"""Centralised progress / debug reporter.

All multi-line orchestration output funnels through one object so format
changes happen in exactly one file.  Two reasons:
  1. The main run is long (hours).  Inconsistent log formats make it hard
     to grep / read after the fact.
  2. The image-progress and CPU-snapshot lines have to be *throttled*
     (every 30 pics; every 60 s) — putting the throttle policy in callers
     leaks state everywhere.

Thread safety: all emission methods take ``self._lock`` so concurrent
workers (image downloads, OCR, audio downloader) interleave cleanly.
"""

from __future__ import annotations

import os
import threading
import time
from html import escape


# ── Resource state for _resource_meter (kept across calls) ────────────────
_net_last: tuple[float, int, int] | None = None  # (time, sent, recv)


def _resource_meter(bound: str = "") -> str:
    """Return CPU + memory + network suffix for progress lines.

    ``bound`` indicates the limiting resource: "cpu" (ASR/OCR) or "io"
    (download).  Network throughput is computed as a delta since the
    previous call using module-level state.
    """
    global _net_last
    try:
        import psutil
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        now = time.time()

        net = psutil.net_io_counters()
        if _net_last is not None:
            dt = now - _net_last[0]
            up = (net.bytes_sent - _net_last[1]) / max(dt, 0.1) / 1024
            down = (net.bytes_recv - _net_last[2]) / max(dt, 0.1) / 1024
            net_str = f" down={down:.0f}KB/s"
        else:
            net_str = ""
        _net_last = (now, net.bytes_sent, net.bytes_recv)

        tag = f"[{bound.upper()}] " if bound else ""
        return f"  {tag}(cpu={cpu:.0f}% mem={mem:.0f}%{net_str})"
    except Exception:
        return ""


class Reporter:
    """Single sink for orchestration logs.

    Caller pattern: every place that used to ``print()`` orchestration
    text now calls ``reporter.<method>(...)``.  The reporter holds tiny
    throttling state (last-emit timestamps per kind/sub_id) so callers
    can fire freely without thinking about cadence.
    """

    # ── Throttling cadences ──
    IMAGE_PROGRESS_EVERY_PICS = 30  # emit a line every 30 finished images
    OCR_PROGRESS_EVERY_PAGES = 20   # emit a line every 20 OCR'd pages

    def __init__(self):
        self._lock = threading.Lock()
        self._failures: set[tuple[str, str]] = set()
        self._waiting_playback: set[str] = set()
        # sub_id -> (last_done_emitted_at_count, t0, last_print_t)
        self._image_progress_state: dict[str, dict] = {}
        self._ocr_progress_state: dict[str, dict] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────

    def run_header(self):
        with self._lock:
            bar = "=" * 60
            print(bar)
            print("iCourse Subscriber — starting run")
            print(bar, flush=True)

    def run_footer(self):
        with self._lock:
            status = (f"Run failed: {len(self._failures)} unresolved item(s)."
                      if self._failures else "Run complete.")
            if self._waiting_playback:
                status += f" Waiting for playback: {len(self._waiting_playback)} lecture(s)."
            print(f"\n{'=' * 60}")
            print(status, flush=True)
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as summary:
                    summary.write(f"## iCourse Check\n\n{status}\n\n")
                    for stage, item in sorted(self._failures):
                        summary.write(f"- {escape(stage)}: <code>{escape(item)}</code>\n")
                    for item in sorted(self._waiting_playback):
                        summary.write(f"- waiting for playback: <code>{escape(item)}</code>\n")

    def raise_if_failed(self):
        """Fail the job only after successful work and email state are saved."""
        with self._lock:
            if self._failures:
                raise RuntimeError(
                    f"Run has {len(self._failures)} unresolved failure(s); "
                    "see the run summary and logs."
                )

    # ── Course-level ─────────────────────────────────────────────────────

    def course_header(self, course_id: str, title: str, teacher: str,
                      total: int, playback: int):
        with self._lock:
            print(f"\n{'─' * 50}")
            print(f"[Course] {course_id}")
            print(f"  Title: {title} (Teacher: {teacher})")
            print(f"  Total lectures: {total} ({playback} with playback)",
                  flush=True)

    def course_dedup_skip(self, sub_title: str, sub_id):
        with self._lock:
            print(f"  [Dedup] Skipping duplicate: {sub_title} "
                  f"(sub_id={sub_id})", flush=True)

    def course_new_count(self, n: int):
        with self._lock:
            print(f"  New/retry lectures: {n}", flush=True)
            if n == 0:
                print("  No new lectures, skipping.", flush=True)

    def course_enumeration_error(self, course_id: str):
        with self._lock:
            self._failures.add(("course enumeration", str(course_id)))
            print(f"  ERROR enumerating course {course_id}:", flush=True)

    # ── Lecture-level ────────────────────────────────────────────────────

    def lecture_start(self, course_title: str, sub_title: str, date: str):
        """Lecture header — explicitly includes course_title so users can
        correlate a Phase-2 'Processing' line back to its course even after
        the Phase-1 enumeration scrolled away."""
        with self._lock:
            print(f"\n  -- [{course_title}] {sub_title} ({date})")
            print(f"    [Time] Start: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                  flush=True)

    def lecture_skip_v2_done(self, sub_title: str, summary_chars: int):
        with self._lock:
            print(f"    v2 summary exists ({summary_chars} chars) and PPT "
                  f"pages present — skipping.", flush=True)

    def lecture_skip_no_video(self, sub_title: str):
        with self._lock:
            self._waiting_playback.add(str(sub_title))
            print("    No video URL — waiting for playback on a later run.", flush=True)

    def lecture_retry_exhausted(self, sub_id: str):
        with self._lock:
            self._failures.add(("retry_exhausted", str(sub_id)))
            print(f"    Retry limit reached for {sub_id}; still missing a summary.", flush=True)

    def lecture_done(self, course_title: str, sub_title: str, elapsed: float):
        with self._lock:
            print(f"    [Time] Done at {time.strftime('%H:%M:%S')}: "
                  f"[{course_title}] {sub_title} (total {elapsed:.0f}s)",
                  flush=True)

    def lecture_error(self, sub_id: str):
        with self._lock:
            self._failures.add(("lecture", str(sub_id)))
            print(f"    ERROR processing {sub_id}:", flush=True)

    # ── PPT pipeline ─────────────────────────────────────────────────────

    def ppt_pages_registered(self, total: int, inserted: int):
        with self._lock:
            print(f"    PPT pages: {total} total ({inserted} newly "
                  f"registered)", flush=True)

    def ppt_list_failed(self, exc_type: str, msg: str):
        with self._lock:
            print(f"    [WARN] PPT list fetch failed: {exc_type}: {msg}",
                  flush=True)

    def ppt_pipeline_summary(self, done: int, dedupped: int, invalid: int,
                             failed: int):
        with self._lock:
            print(f"    PPT pipeline: {done} done, {dedupped} dedup'd, "
                  f"{invalid} invalid, {failed} failed", flush=True)

    # ── Image-download progress (throttled every IMAGE_PROGRESS_EVERY_PICS) ──

    def image_progress_start(self, sub_id: str, total: int):
        """Record t0 for a sub_id so per-30-pic rate can be computed."""
        with self._lock:
            self._image_progress_state[sub_id] = {
                "total": total,
                "done": 0,
                "t0": time.time(),
                "last_emit_at_count": 0,
            }

    def image_progress_tick(self, sub_id: str):
        """Call once per finished image. Emits a line every 30 (and at end).

        Hot path — OCR/image workers hit this from many threads.  Keep the
        critical section tiny: only mutate the per-sub_id counter inside
        the lock, then drop the lock before doing psutil sampling +
        ``print`` (both can stall hundreds of ms under high CPU/IO load).
        """
        emit_payload = None
        with self._lock:
            st = self._image_progress_state.get(sub_id)
            if st is None:
                return
            st["done"] += 1
            done = st["done"]
            total = st["total"]
            is_final = (done == total)
            cross = (done - st["last_emit_at_count"]) >= self.IMAGE_PROGRESS_EVERY_PICS
            if cross or is_final:
                st["last_emit_at_count"] = done
                elapsed = max(time.time() - st["t0"], 0.001)
                emit_payload = (done, total, done / elapsed, is_final)
            if is_final:
                self._image_progress_state.pop(sub_id, None)
        if emit_payload is None:
            return
        done, total, rate, _is_final = emit_payload
        bar = self._bar(done, total)
        _rm = _resource_meter("io")
        print(f"    [Images {sub_id}] {bar} {done}/{total} "
              f"({rate:.1f} pic/s){_rm}", flush=True)

    def image_progress_abort(self, sub_id: str):
        with self._lock:
            self._image_progress_state.pop(sub_id, None)

    # ── OCR-completion progress (throttled every OCR_PROGRESS_EVERY_PAGES) ──
    #
    # Lifecycle mirrors image_progress_*: caller registers a total at start,
    # ticks once per finished page, and the line is emitted every N pages
    # plus once at completion. We track per-sub_id because the prefetch
    # pipeline may submit the next lecture's OCR work before this lecture's
    # OCR drains, so two streams can overlap.
    #
    # Distinction from pool-occupancy metrics (which show how many OCR
    # slots are in flight): this one is *throughput* (pages OCR'd per
    # second).  Both useful, neither substitutes for the other.

    def ocr_progress_start(self, sub_id: str, total: int):
        """Record t0 for a sub_id's OCR phase so page/s can be computed."""
        with self._lock:
            self._ocr_progress_state[sub_id] = {
                "total": total,
                "done": 0,
                "t0": time.time(),
                "last_emit_at_count": 0,
            }

    def ocr_progress_tick(self, sub_id: str):
        """Call once per finished OCR page.  Emits every N pages and at end.

        Cheap and lock-free for the not-tracked case so OCR workers that
        run from contexts without a registered start don't pay any cost.

        Like ``image_progress_tick``, the psutil snapshot and ``print``
        run outside the lock so the hot path serialises only on a tiny
        counter update.
        """
        emit_payload = None
        with self._lock:
            st = self._ocr_progress_state.get(sub_id)
            if st is None:
                return
            st["done"] += 1
            done = st["done"]
            total = st["total"]
            is_final = (done >= total)
            cross = (done - st["last_emit_at_count"]) >= self.OCR_PROGRESS_EVERY_PAGES
            if cross or is_final:
                st["last_emit_at_count"] = done
                elapsed = max(time.time() - st["t0"], 0.001)
                emit_payload = (done, total, done / elapsed, is_final)
            if is_final:
                self._ocr_progress_state.pop(sub_id, None)
        if emit_payload is None:
            return
        done, total, rate, _is_final = emit_payload
        bar = self._bar(done, total)
        _rm = _resource_meter("cpu")
        print(f"    [OCR {sub_id}] {bar} {done}/{total} "
              f"({rate:.2f} page/s){_rm}", flush=True)

    def ocr_progress_abort(self, sub_id: str):
        with self._lock:
            self._ocr_progress_state.pop(sub_id, None)

    @staticmethod
    def _bar(done: int, total: int, width: int = 20) -> str:
        if total <= 0:
            return "[" + " " * width + "]"
        filled = int(width * done / total)
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    # ── Prefetch / audio download ────────────────────────────────────────

    def audio_prefetch_start(self, sub_id: str):
        with self._lock:
            print(f"    [Prefetch] audio for {sub_id} starting...",
                  flush=True)

    def audio_prefetch_done(self, sub_id: str, elapsed: float, size_mb: float):
        with self._lock:
            rate = size_mb / max(elapsed, 0.001)
            print(f"    [Prefetch] audio for {sub_id}: {size_mb:.1f} MB "
                  f"in {elapsed:.1f}s ({rate:.1f} MB/s)", flush=True)

    def audio_prefetch_failed(self, sub_id: str, exc: BaseException):
        with self._lock:
            print(f"    [Prefetch] audio for {sub_id} failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    # ── Email / generic ──────────────────────────────────────────────────

    def email_summary(self, n: int):
        with self._lock:
            print(f"\n[Email] Sending summary for {n} lecture(s)...",
                  flush=True)

    def email_failed(self, sub_id: str = "batch"):
        with self._lock:
            self._failures.add(("email", str(sub_id)))
            print("[Email] Send failed, lectures will be retried next run.",
                  flush=True)

    def email_recovered_unsent(self, n: int):
        with self._lock:
            print(f"[Email] Including {n} previously unsent lecture(s).",
                  flush=True)

    def info(self, msg: str):
        """Generic info line — escape hatch for one-off messages."""
        with self._lock:
            print(msg, flush=True)

    # ── Semester course crawl ────────────────────────────────────────────

    def crawl_courses_start(self, term: str):
        with self._lock:
            print(f"\n[Crawl] Fetching semester {term} course catalog...",
                  flush=True)

    def crawl_courses_done(self, term: str, fetched: int,
                           deleted: int, upserted: int, elapsed: float):
        with self._lock:
            print(
                f"[Crawl] Term {term}: {fetched} courses fetched, "
                f"{upserted} upserted, {deleted} removed in {elapsed:.1f}s",
                flush=True,
            )

    def crawl_courses_failed(self, term: str, exc: BaseException):
        with self._lock:
            print(
                f"[Crawl] Term {term} failed: "
                f"{type(exc).__name__}: {exc}", flush=True,
            )
