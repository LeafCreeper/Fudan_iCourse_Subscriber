"""Concurrency primitives: thread pools, prefetch caches, audio downloader.

Owns three resource pools and coordinates work across them so the
LectureRunner can focus on per-lecture business logic.

  image_pool       20 workers   IO bound  (per-image HTTP)
  ocr_pool          8 workers   CPU bound (RapidOCR), gated by BoundedSemaphore(2)
  audio_downloader  2 slots     IO bound  (ffmpeg URL → audio.raw to disk)

The audio downloader is special: each "slot" hosts a running ffmpeg process
that writes f32le mono 16 kHz audio to a per-sub_id scratch file.  Transcriber
reads that file with tail-f semantics while ffmpeg is still writing — so the
network download isn't bottlenecked by ASR speed and the ASR isn't blocked
on download completion.  See ``AudioDownloader`` below.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from src.runtime import config
from src.api import icourse
from src.ai.transcriber import AudioDownloadError


# ── PrefetchCache (per-sub_id image bytes) ─────────────────────────────────

class PrefetchCache:
    """Per-lecture image-bytes pre-fetcher driven by the global image pool.

    schedule(client, course_id, sub_id) — fetch the PPT list (synchronous,
                                          a few paginated GETs), fire all
                                          image downloads into the pool,
                                          then return.  Idempotent.
    wait(sub_id) -> (items, images)   — block until every download for sub_id
                                          resolves.
    discard(sub_id)                    — drop the cached entry (release bytes).
    in_flight(sub_id)                  — number of unfinished futures.

    The reporter (passed at __init__) is called for per-image ticks so
    progress logging is throttled in one place.  ``reporter`` may be ``None``
    in tests that don't care about output.
    """

    def __init__(self, image_pool: ThreadPoolExecutor, reporter=None):
        self._image_pool = image_pool
        self._reporter = reporter
        self._lock = threading.Lock()
        # sub_id -> {"items": list[dict]|None, "futures": dict[int, Future]}
        self._cache: dict[str, dict] = {}

    def schedule(self, client, course_id: str, sub_id: str):
        sub_id = str(sub_id)
        with self._lock:
            if sub_id in self._cache:
                return
            self._cache[sub_id] = {"items": None, "futures": {}}

        try:
            ppt_items = client.get_ppt_list(course_id, sub_id)
        except Exception as e:
            if self._reporter:
                self._reporter.ppt_list_failed(type(e).__name__, str(e))
            ppt_items = []
        for idx, item in enumerate(ppt_items, start=1):
            item["page_num"] = idx

        if self._reporter and ppt_items:
            self._reporter.image_progress_start(sub_id, len(ppt_items))

        futures: dict[int, Future] = {}
        for item in ppt_items:
            futures[item["page_num"]] = self._image_pool.submit(
                self._download_one, client, item, sub_id,
            )

        with self._lock:
            self._cache[sub_id]["items"] = ppt_items
            self._cache[sub_id]["futures"] = futures

    def _download_one(self, client, item: dict, sub_id: str) -> bytes | None:
        """Image-pool worker body. Goes through the module-level
        ``icourse.fetch_ppt_image`` so tests can monkey-patch it."""
        try:
            return icourse.fetch_ppt_image(client, item)
        finally:
            if self._reporter:
                self._reporter.image_progress_tick(sub_id)

    def wait(self, sub_id: str) -> tuple[list[dict], dict[int, bytes]]:
        sub_id = str(sub_id)
        with self._lock:
            entry = self._cache.get(sub_id)
        if entry is None:
            return [], {}
        items = entry.get("items") or []
        images: dict[int, bytes] = {}
        for page_num, fut in entry.get("futures", {}).items():
            try:
                img = fut.result()
            except Exception as e:
                print(
                    f"    [Prefetch {sub_id}] page {page_num} download "
                    f"failed: {type(e).__name__}: {e}"
                )
                img = None
            if img is not None:
                images[page_num] = img
        return items, images

    def discard(self, sub_id: str) -> None:
        sub_id = str(sub_id)
        with self._lock:
            self._cache.pop(sub_id, None)
        if self._reporter:
            self._reporter.image_progress_abort(sub_id)


# ── AudioDownloader (per-sub_id ffmpeg → disk audio file) ──────────────────

@dataclass
class AudioHandle:
    """Reference to an audio-extraction job."""

    sub_id: str
    path: str          # disk file ffmpeg writes f32le mono 16 kHz to
    process: subprocess.Popen
    stderr_chunks: list[bytes]


class _PendingSpawn:
    """Per-schedule placeholder stored in ``_active`` while the background
    spawn is still working.  A unique instance per ``schedule()`` call lets
    the spawn thread detect that its entry was ``release()``-d (or replaced)
    in the meantime and abort instead of resurrecting a zombie entry."""

    def __init__(self):
        self.error: AudioDownloadError | None = None


class AudioDownloader:
    """Spawn-and-track concurrent ``ffmpeg`` audio extractions.

    For each sub_id we spawn one ``ffmpeg -i <signed URL> -vn -ar 16000 -ac 1
    -f f32le <path>`` process.  ``ffmpeg`` writes the decoded mono float32
    audio straight to disk at network speed — no Python pipe in the loop, so
    download is NOT bottlenecked by ASR consumption.  Transcriber reads that
    file with tail-f semantics, processing chunks as they arrive.

    Concurrency is bounded by ``max_concurrent`` (default 2: current lecture
    being transcribed + one pre-decoded for the next lecture).  ``schedule()``
    returns immediately; if all slots are taken the background spawn waits.
    """

    def __init__(self, audio_dir: str, max_concurrent: int = None,
                 reporter=None):
        self._dir = audio_dir
        self.max_concurrent = max_concurrent or config.VIDEO_DOWNLOAD_CONCURRENCY
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        # sub_id -> AudioHandle (ready) | _PendingSpawn (spawn in flight)
        self._active: dict[str, "AudioHandle | _PendingSpawn"] = {}
        self._lock = threading.Lock()
        self._reporter = reporter
        os.makedirs(self._dir, exist_ok=True)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for h in self._active.values()
                if isinstance(h, AudioHandle)
            )

    def schedule(self, client, course_id: str, sub_id: str) -> None:
        """Reserve a slot for sub_id and spawn ffmpeg in the background.

        Returns immediately. If all slots are taken the spawn blocks in its
        background thread until a slot frees.  Idempotent — second call for
        the same sub_id is a no-op.
        """
        sub_id = str(sub_id)
        pending = _PendingSpawn()
        with self._lock:
            if sub_id in self._active:
                return
            self._active[sub_id] = pending

        threading.Thread(
            target=self._spawn_when_ready,
            args=(client, course_id, sub_id, pending),
            name=f"audio-spawn-{sub_id}",
            daemon=True,
        ).start()

    def _pop_if_mine(self, sub_id: str, pending: _PendingSpawn) -> None:
        """Remove our pending entry — but only if it is still ours."""
        with self._lock:
            if self._active.get(sub_id) is pending:
                self._active.pop(sub_id, None)

    def _spawn_when_ready(self, client, course_id: str, sub_id: str,
                          pending: _PendingSpawn):
        try:
            self._sem.acquire()
            try:
                with self._lock:
                    if self._active.get(sub_id) is not pending:
                        self._sem.release()
                        return
                url = client.get_video_url(course_id, sub_id)
                if not url:
                    self._pop_if_mine(sub_id, pending)
                    self._sem.release()
                    return
                vpn_url, headers = client.get_stream_params(url)
                # A cancelled spawn may return after a replacement starts.
                # Its cleanup must never touch the replacement's PCM file.
                path = os.path.join(self._dir, f"{sub_id}-{uuid.uuid4().hex}.raw")

                cmd = [
                    "ffmpeg", "-y",
                    "-headers", headers,
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",
                    "-i", vpn_url,
                    "-vn",
                    "-ar", "16000",
                    "-ac", "1",
                    "-f", "f32le",
                    path,
                ]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )

                # Drain stderr so the pipe never deadlocks.  Keep last few KB
                # for diagnostics if ffmpeg dies.
                stderr_chunks: list[bytes] = []

                def _drain():
                    try:
                        for chunk in proc.stderr:
                            stderr_chunks.append(chunk)
                            if len(stderr_chunks) > 2048:
                                # keep only the tail to bound memory
                                del stderr_chunks[: -1024]
                    except Exception:
                        pass

                threading.Thread(
                    target=_drain, name=f"audio-stderr-{sub_id}",
                    daemon=True,
                ).start()

                handle = AudioHandle(
                    sub_id=sub_id, path=path,
                    process=proc, stderr_chunks=stderr_chunks,
                )

                # Install the handle — unless release() already removed our
                # pending entry (e.g. get() timed out and the caller gave
                # up).  In that case nobody will ever release this handle,
                # so kill the orphan ffmpeg here instead of letting it hold
                # a download slot for the rest of the run.
                with self._lock:
                    installed = self._active.get(sub_id) is pending
                    if installed:
                        self._active[sub_id] = handle
                if not installed:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    self._sem.release()
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    return

                if self._reporter:
                    self._reporter.audio_prefetch_start(sub_id)

                # Background monitor: release the semaphore slot when
                # ffmpeg exits.  We do NOT pop from _active here — that's
                # the caller's job (via release()).
                threading.Thread(
                    target=self._monitor, args=(handle,),
                    name=f"audio-monitor-{sub_id}", daemon=True,
                ).start()
            except Exception as e:
                # None means no playable URL, not a network/login error.
                # Preserve the failure for get(), without signed URLs.
                error = AudioDownloadError(
                    f"audio download for {sub_id} could not start ({type(e).__name__})"
                )
                with self._lock:
                    if self._active.get(sub_id) is pending:
                        pending.error = error
                self._sem.release()
                raise error from None
        except AudioDownloadError as e:
            if self._reporter:
                self._reporter.audio_prefetch_failed(sub_id, e)

    def _monitor(self, handle: AudioHandle):
        handle.process.wait()
        self._sem.release()

    def get(self, sub_id: str, timeout: float = 120.0) -> AudioHandle | None:
        """Block until ffmpeg has been spawned for sub_id; return its handle.

        Returns None if sub_id was never scheduled (or already released).
        Raises TimeoutError if the spawn never happens within ``timeout``.
        Raises AudioDownloadError if acquiring the URL or spawning failed.
        """
        sub_id = str(sub_id)
        deadline = time.time() + timeout
        while True:
            with self._lock:
                entry = self._active.get(sub_id)
                if entry is None:
                    return None
                if isinstance(entry, AudioHandle):
                    return entry
                if entry.error is not None:
                    raise entry.error
            if time.time() > deadline:
                raise TimeoutError(
                    f"audio download for {sub_id} did not start within "
                    f"{timeout}s — likely WebVPN session expired or "
                    f"get_video_url failed"
                )
            time.sleep(0.05)

    def release(self, sub_id: str) -> None:
        """Kill ffmpeg (if still alive) and delete the audio file.

        Called from LectureRunner Phase H once the lecture has been
        transcribed and summarized.  If the spawn is still pending, the
        entry is removed and the spawn thread aborts itself when it
        notices its token is gone.
        """
        sub_id = str(sub_id)
        with self._lock:
            handle = self._active.pop(sub_id, None)
        if not isinstance(handle, AudioHandle):
            return
        proc = handle.process
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # The monitor thread releases the semaphore on its own.
        if os.path.exists(handle.path):
            try:
                os.remove(handle.path)
            except OSError:
                pass

    def shutdown(self) -> None:
        """Kill every in-flight ffmpeg and wipe the scratch directory."""
        with self._lock:
            sub_ids = list(self._active.keys())
        for sub_id in sub_ids:
            self.release(sub_id)


# ── Scheduler — single façade ──────────────────────────────────────────────

@dataclass
class ResourceSnapshot:
    cpu_pct: float
    ocr_busy: int
    ocr_target: int
    image_busy: int
    audio_busy: int


class Scheduler:
    """Single façade owning every concurrency primitive.

    LectureRunner gets one Scheduler.  Through it, every other layer talks
    to pools and prefetch caches by name — no module holds its own
    ThreadPoolExecutor.

    OCR concurrency is capped by a fixed BoundedSemaphore (2 permits).
    RapidOCR is single-threaded CPU-bound; more than 2 concurrent workers
    don't increase throughput on a 4-core runner and waste cycles on
    contention.  There is no dynamic adjustment — the simple fixed cap is
    both sufficient and easier to reason about.

    Lifecycle:
        scheduler = Scheduler(reporter=...)
        ... LectureRunner uses it ...
        scheduler.shutdown()        # drains pools, kills ffmpegs
    """

    def __init__(self, reporter):
        self._reporter = reporter
        self.image_pool = ThreadPoolExecutor(
            max_workers=config.IMAGE_WORKERS, thread_name_prefix="img",
        )
        self.ocr_pool = ThreadPoolExecutor(
            max_workers=config.OCR_MAX_WORKERS, thread_name_prefix="ocr",
        )
        self._ocr_sem = threading.BoundedSemaphore(
            config.OCR_MAX_TARGET
        )
        self.image_cache = PrefetchCache(self.image_pool, reporter=reporter)
        self.audio_downloader = AudioDownloader(
            audio_dir=config.AUDIO_DIR,
            max_concurrent=config.VIDEO_DOWNLOAD_CONCURRENCY,
            reporter=reporter,
        )

    def prefetch_lecture(self, client, course_id: str, sub_id: str,
                         *, audio: bool = True) -> None:
        """Schedule image (always) + audio (optional) prefetch for a future
        lecture.  Pass ``audio=False`` when the caller already knows
        transcription won't read the stream (cached or official
        transcript) — the ffmpeg download is a full pull of the lecture
        and would otherwise hold one of the two slots for nothing."""
        self.image_cache.schedule(client, course_id, sub_id)
        if audio:
            self.audio_downloader.schedule(client, course_id, sub_id)

    def submit_ocr(self, fn: Callable, *args, **kwargs) -> Future:
        """Submit an OCR job.  Live concurrency is capped at
        OCR_MAX_TARGET (2) by a fixed BoundedSemaphore — no dynamic
        CPU-based adjustment since RapidOCR is single-threaded and
        never benefits from more than 2 concurrent workers on a 4-core
        runner."""
        def _wrapped():
            with self._ocr_sem:
                return fn(*args, **kwargs)
        return self.ocr_pool.submit(_wrapped)

    def shutdown(self) -> None:
        self.audio_downloader.shutdown()
        self.image_pool.shutdown(wait=True)
        self.ocr_pool.shutdown(wait=True)
