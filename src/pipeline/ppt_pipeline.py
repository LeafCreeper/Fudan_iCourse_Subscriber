"""Unified PPT fetch → dHash dedup → OCR → classify pipeline.

Two consumption patterns share the same four stages:

  PPTPipeline.submit(...)        → returns a ``PPTAsyncHandle`` that holds
                                   in-flight OCR ``Future`` s.  Caller (the
                                   LectureRunner) does ASR transcription in
                                   parallel, then calls ``handle.drain()`` to
                                   block for stats.
  PPTPipeline.run_blocking(...)  → ``submit`` + immediate ``drain``.  Used by
                                   the resummarize path where there is no
                                   accompanying ASR to overlap with.

The four stages are identical in both paths:

  1. Fetch the PPT list from iCourse and ``INSERT OR IGNORE`` each row into
     ``ppt_pages`` with ``ocr_status='pending'``.  Idempotent — safe to call
     across resumed runs.  Items typically come from the prefetch cache the
     previous lecture warmed up; if absent, ``submit`` re-schedules.
  2. Collect image bytes for every still-pending page.  Pulls from
     ``Scheduler.image_cache`` (prefetch) first, then falls back to a sync
     ``fetch_ppt_image`` call for any pending row missing from the cache
     (typically a stale row from a prior interrupted run, or a download
     that failed in the prefetch pool).
  3. Sliding-window dHash dedup over the chronologically ordered pending
     pages — losers are stamped ``dedup_dropped`` and removed before OCR.
  4. For each survivor: submit an OCR job to ``Scheduler.submit_ocr`` (which
     gates on the dynamic semaphore, so live concurrency tracks
     ResourceMonitor's target).  Workers classify the OCR'd text as
     ``invalid`` (matches one of the classroom-noise screens) or ``done``,
     and write the row in place.

``get_done_ppt_pages`` only surfaces rows with status='done', so dropped /
invalid / failed pages naturally drop out of the LLM prompt.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.api import icourse
from src.ai.ocr import ocr_image_text
from src.ai.ppt_dedup import compute_dhash, dedup_dhash, is_invalid_page

if TYPE_CHECKING:
    from src.data.database import Database
    from src.api.icourse import ICourseClient
    from src.runtime.reporter import Reporter
    from src.runtime.scheduler import Scheduler


@dataclass
class PPTStats:
    """Final accounting of one lecture's PPT pipeline run.

    ``done`` is the only count that feeds the LLM prompt; the other buckets
    are diagnostic only.
    """

    total: int       # total ``ppt_pages`` rows after registration
    inserted: int    # newly registered this run
    done: int        # OCR succeeded, text kept
    invalid: int     # matched a classroom-noise pattern
    dedupped: int    # dropped by dHash sliding-window dedup
    failed: int      # download or OCR error


class PPTAsyncHandle:
    """Caller-owned handle returned by ``PPTPipeline.submit``.

    Holds the list of in-flight OCR ``Future`` s plus the counts already
    known at submit-time (dedup losers, sync-fallback download failures).
    ``drain()`` blocks until every OCR future resolves and returns the
    aggregated ``PPTStats``.

    The handle is one-shot — calling ``drain()`` twice returns cached stats.
    """

    def __init__(self, pipeline: "PPTPipeline", sub_id: str,
                 *, total: int, inserted: int,
                 futures: list[Future], dedupped: int, presubmit_failed: int,
                 images: dict[int, bytes] | None = None):
        self._pipeline = pipeline
        self._sub_id = sub_id
        self._total = total
        self._inserted = inserted
        self._futures = futures
        self._dedupped = dedupped
        self._presubmit_failed = presubmit_failed
        self._images = images  # non-None when OCR was deferred
        self._ocr_submitted = False
        self._drained: PPTStats | None = None

    def get_done_images(self) -> dict[int, bytes]:
        """Return {page_num: image_bytes} for OCR-successful pages.

        Pulls from the owning pipeline's per-sub_id image cache, which is
        populated by ``_ocr_worker`` across every OCR path (deferred drain,
        look-ahead prefetch, blocking).  Caller (the LectureRunner / email
        path) uses this to embed PPT images without re-downloading from the
        ephemeral pptimgurl.  Best called after ``drain()`` so every OCR
        future for this lecture has resolved.
        """
        return self._pipeline.get_cached_images(self._sub_id)

    def drain(self) -> PPTStats:
        """Block until every OCR future resolves; return aggregate stats."""
        if self._drained is not None:
            return self._drained
        # If OCR was deferred (submit with defer_ocr=True), submit it now
        # so that drain blocks for the actual OCR work, not an empty list.
        if self._images and not self._ocr_submitted:
            self._ocr_submitted = True
            s = self._pipeline._scheduler
            if self._pipeline._reporter and self._images:
                self._pipeline._reporter.ocr_progress_start(
                    self._sub_id, len(self._images),
                )
            for page_num, img in self._images.items():
                self._futures.append(
                    s.submit_ocr(
                        self._pipeline._ocr_worker,
                        self._sub_id, page_num, img,
                    )
                )
            self._images = None  # release memory
        done = invalid = 0
        failed = self._presubmit_failed
        for fut in as_completed(self._futures):
            try:
                _page_num, status = fut.result()
            except Exception as e:
                print(f"      OCR worker exception: {type(e).__name__}: {e}",
                      flush=True)
                failed += 1
                continue
            if status == "done":
                done += 1
            elif status == "invalid":
                invalid += 1
            elif status == "failed":
                failed += 1
        stats = PPTStats(
            total=self._total, inserted=self._inserted,
            done=done, invalid=invalid,
            dedupped=self._dedupped, failed=failed,
        )
        self._drained = stats
        reporter = self._pipeline._reporter
        if reporter and (done or invalid or self._dedupped or failed):
            reporter.ppt_pipeline_summary(done, self._dedupped, invalid, failed)
        return stats


class PPTPipeline:
    """Drives the PPT pipeline against a ``Scheduler`` and a ``Database``."""

    def __init__(self, db: "Database", scheduler: "Scheduler",
                 reporter: "Reporter | None" = None):
        self._db = db
        self._scheduler = scheduler
        self._reporter = reporter
        # OCR futures submitted by prefetch_and_ocr (runs during LLM wait).
        # Keyed by sub_id; submit() drains them before starting ASR if the
        # pre-submitted OCR hasn't completed yet.
        self._prefetched_ocr: dict[str, list[Future]] = {}
        # Raw image bytes for pages that OCR'd to status='done', keyed by
        # sub_id → {page_num: bytes}.  Populated by ``_ocr_worker`` from
        # whichever pool thread ran the OCR, so access is mutex-guarded.
        # Consumed by the email path (CID-embedded inline PPT images) which
        # can't re-download from pptimgurl (internal/ephemeral URL).  The
        # owning LectureRunner discards the entry after building email_items.
        self._image_cache: dict[str, dict[int, bytes]] = {}
        self._image_cache_lock = threading.Lock()

    # ── PPT image byte cache (for email embedding) ──────────────────────

    def _cache_image(self, sub_id: str, page_num: int, img: bytes) -> None:
        """Stash one OCR-successful image's bytes for later embedding."""
        with self._image_cache_lock:
            self._image_cache.setdefault(sub_id, {})[page_num] = img

    def get_cached_images(self, sub_id: str) -> dict[int, bytes]:
        """Return a snapshot copy of {page_num: bytes} cached for ``sub_id``."""
        with self._image_cache_lock:
            return dict(self._image_cache.get(sub_id, {}))

    def discard_cached_images(self, sub_id: str) -> None:
        """Drop the cached image bytes for ``sub_id`` to release memory."""
        with self._image_cache_lock:
            self._image_cache.pop(sub_id, None)

    # ── Public entry points ─────────────────────────────────────────────

    def submit(self, client: "ICourseClient", course_id: str,
               sub_id: str, *, defer_ocr: bool = False) -> PPTAsyncHandle:
        """Stages 1-3 run inline; stage 4 (OCR) is submitted to the pool.

        Returns immediately with a handle so the caller can do ASR (or any
        other long parallel work) before draining.  After this call returns
        the prefetch cache entry has been ``discard``-ed; the OCR workers
        hold the image bytes they need via closure capture.
        """
        sub_id = str(sub_id)

        # Stage 1 — register pending rows from the current PPT list.
        # Prefetch may already have been scheduled by the previous lecture;
        # ``schedule`` is idempotent so this is safe either way.
        self._scheduler.image_cache.schedule(client, course_id, sub_id)
        ppt_items, images = self._scheduler.image_cache.wait(sub_id)

        inserted = 0
        if ppt_items:
            inserted = self._db.insert_ppt_pages_pending(sub_id, ppt_items)
        total = self._db.count_total_ppt_pages(sub_id)
        if self._reporter and (inserted or total):
            self._reporter.ppt_pages_registered(total, inserted)

        # Stage 2 — assemble images for every still-pending row.
        # The DB may include stale pending rows from a prior interrupted
        # run that aren't in the current prefetch.  Re-download those in
        # the main thread (rare path, kept simple).
        pending = self._db.get_pending_ppt_pages(sub_id)
        # If prefetch_and_ocr submitted OCR in the previous lecture's LLM
        # phase, drain those futures before proceeding (OCR may still be
        # running if the LLM returned early).  After draining, re-query
        # pending so any pages the API exposed *after* prefetch ran (rare,
        # but possible if the lecturer adds slides mid-recording) are still
        # processed instead of silently dropped.
        pre_futs = self._prefetched_ocr.pop(sub_id, None)
        if pre_futs:
            for fut in as_completed(pre_futs):
                try:
                    fut.result()
                except Exception:
                    pass
            pending = self._db.get_pending_ppt_pages(sub_id)
        presubmit_failed = 0
        for p in pending:
            page_num = p["page_num"]
            if page_num in images:
                continue
            img = icourse.fetch_ppt_image(client, p)
            if img is None:
                self._db.update_ppt_page(sub_id, page_num, None, "failed")
                presubmit_failed += 1
            else:
                images[page_num] = img

        # Stage 3 — dHash + sliding-window dedup over chronologically
        # ordered pending rows.  Dhash is also persisted for later
        # diagnostics (e.g. inspecting dedup decisions across runs).
        dhashes_in_order: list[str | None] = []
        page_at_index: list[int] = []
        for p in pending:
            page_num = p["page_num"]
            img = images.get(page_num)
            if img is None:
                continue
            dh = compute_dhash(img)
            self._db.update_ppt_page_dhash(sub_id, page_num, dh)
            dhashes_in_order.append(dh)
            page_at_index.append(page_num)

        dropped_idx = dedup_dhash(dhashes_in_order)
        dropped_pages = {page_at_index[i] for i in dropped_idx}
        for page_num in dropped_pages:
            self._db.update_ppt_page(sub_id, page_num, None, "dedup_dropped")
            images.pop(page_num, None)

        # Stage 4 — optionally submit OCR.  When defer_ocr=True, OCR is
        # skipped now and submitted lazily in handle.drain() to avoid
        # CPU contention with ASR (the caller runs ASR between submit
        # and drain).
        keep_images: dict[int, bytes] = {
            pn: img for pn, img in images.items() if pn not in dropped_pages
        }
        futures: list[Future] = []
        if not defer_ocr:
            if self._reporter and keep_images:
                self._reporter.ocr_progress_start(sub_id, len(keep_images))
            for page_num, img in keep_images.items():
                futures.append(
                    self._scheduler.submit_ocr(
                        self._ocr_worker, sub_id, page_num, img,
                    )
                )
            keep_images = {}  # release memory; workers hold closures

        self._scheduler.image_cache.discard(sub_id)

        return PPTAsyncHandle(
            self, sub_id,
            total=total, inserted=inserted, futures=futures,
            dedupped=len(dropped_pages),
            presubmit_failed=presubmit_failed,
            images=keep_images or None,
        )

    def prefetch_and_ocr(self, client: "ICourseClient", course_id: str,
                          sub_id: str) -> None:
        """Download images + dedup + submit OCR for a lecture, but DON'T
        drain or discard the prefetch cache.

        Designed to be called during the LLM wait of the *previous* lecture.
        OCR runs in the background pool while the API call is in flight.
        The subsequent ``submit()`` call for this lecture will find the
        already-OCR'd pages in the DB and skip redundant work.

        The prefetch cache is intentionally NOT discarded here — the
        real ``submit()`` call in Phase B of the next lecture handles that.
        """
        sub_id = str(sub_id)
        self._scheduler.image_cache.schedule(client, course_id, sub_id)
        ppt_items, images = self._scheduler.image_cache.wait(sub_id)

        if ppt_items:
            self._db.insert_ppt_pages_pending(sub_id, ppt_items)

        pending = self._db.get_pending_ppt_pages(sub_id)
        if not pending:
            return

        # Re-fetch any images not in the prefetch cache
        for p in pending:
            pn = p["page_num"]
            if pn in images:
                continue
            img = icourse.fetch_ppt_image(client, p)
            if img:
                images[pn] = img

        # Dedup
        dhashes: list[str | None] = []
        indices: list[int] = []
        for p in pending:
            pn = p["page_num"]
            img = images.get(pn)
            if img is None:
                continue
            dh = compute_dhash(img)
            self._db.update_ppt_page_dhash(sub_id, pn, dh)
            dhashes.append(dh)
            indices.append(pn)

        dropped = {indices[i] for i in dedup_dhash(dhashes)}
        for pn in dropped:
            self._db.update_ppt_page(sub_id, pn, None, "dedup_dropped")
            images.pop(pn, None)

        # Submit OCR — store futures so submit() can drain them if they
        # haven't completed by the time the next lecture starts.
        ocr_pages = {pn: img for pn, img in images.items() if pn not in dropped}
        if self._reporter and ocr_pages:
            self._reporter.ocr_progress_start(sub_id, len(ocr_pages))
        futures = [
            self._scheduler.submit_ocr(self._ocr_worker, sub_id, pn, img)
            for pn, img in ocr_pages.items()
        ]
        if futures:
            self._prefetched_ocr[sub_id] = futures

    def run_blocking(self, client: "ICourseClient", course_id: str,
                     sub_id: str) -> PPTStats:
        """submit() then drain(); convenient for callers with no parallel work."""
        return self.submit(client, course_id, sub_id).drain()

    # ── Worker ──────────────────────────────────────────────────────────

    def _ocr_worker(self, sub_id: str, page_num: int,
                    image_bytes: bytes) -> tuple[int, str]:
        """OCR one image, classify, persist. Returns (page_num, status).

        Runs in the OCR pool (gated by the dynamic semaphore).  Database
        writes go through ``Database._lock`` so concurrent workers don't
        race on the same row.

        On a ``done`` classification the raw ``image_bytes`` are stashed in
        the pipeline-level image cache (``_cache_image``) so the email /
        frontend embed path can reuse them without re-downloading from the
        ephemeral ``pptimgurl`` (which is only reachable while the WebVPN
        session is live).  This is done here — rather than in ``drain()`` —
        so it covers every OCR path uniformly: the deferred ``drain()``
        path, the ``prefetch_and_ocr`` look-ahead path (whose futures are
        drained-and-discarded by the next ``submit``), and ``run_blocking``.

        The reporter tick fires for every outcome (done/invalid/failed) so
        the printed page/s reflects total OCR throughput, not just
        successful pages — otherwise a run with many "invalid" classroom
        screens would look artificially slow.
        """
        try:
            text = ocr_image_text(image_bytes)
        except Exception as e:
            print(f"      page {page_num}: OCR error "
                  f"{type(e).__name__}: {e}", flush=True)
            self._db.update_ppt_page(sub_id, page_num, None, "failed")
            if self._reporter:
                self._reporter.ocr_progress_tick(sub_id)
            return page_num, "failed"
        status = "invalid" if is_invalid_page(text) else "done"
        self._db.update_ppt_page(sub_id, page_num, text, status)
        if status == "done":
            self._cache_image(sub_id, page_num, image_bytes)
        if self._reporter:
            self._reporter.ocr_progress_tick(sub_id)
        return page_num, status
