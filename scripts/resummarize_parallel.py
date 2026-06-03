#!/usr/bin/env python3
"""Parallel resummarize — dual pipeline: OCR saturation + concurrent LLM.

Architecture:

  Phase 1 — Catalog
    Scan every target lecture, fetch its PPT list, register all pending
    rows.  After this phase we know the full workload.

  Phase 2a — OCR saturation (CPU-bound, ~6 threads)
    Each thread takes a batch of lectures and runs the full PPT pipeline
    (download → dedup → OCR → drain) for each.  As a lecture's OCR
    completes, its (transcript, pages) is pushed to the LLM queue.

  Phase 2b — LLM concurrent (IO-bound, ~4 workers)
    Workers pull from the queue, assemble the prompt, call the API.
    N concurrent API calls cost no extra CPU.

  The two pipelines run simultaneously, so wall-clock time is bounded
  by max(ΣOCR, ΣLLM) rather than ΣOCR + ΣLLM.

Usage:
    python scripts/resummarize_parallel.py                      # all
    python scripts/resummarize_parallel.py --limit 100          # first 100
    python scripts/resummarize_parallel.py --llm-workers 8      # more LLM concurrency
    python scripts/resummarize_parallel.py --all                # force every summary
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
from queue import Queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.database import Database
from src.ai.summarizer import Summarizer
from src.ai.bucketer import assemble
from src.pipeline.ppt_pipeline import PPTPipeline
from src.runtime.scheduler import Scheduler
from src.runtime.reporter import Reporter
from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession

# ── Defaults ───────────────────────────────────────────────────────────────
OCR_THREADS = 6
LLM_WORKERS = 4
SESSION_LOCK = threading.Lock()
# Global LLM queue populated during Phase 2
llm_queue: Queue | None = None


def check_session(client: ICourseClient) -> None:
    """Thread-safe session refresh — one re-login at a time."""
    if client.check_alive():
        return
    with SESSION_LOCK:
        if client.check_alive():
            return
        print("[Session] WebVPN session expired, re-logging in...")
        vpn = WebVPNSession()
        vpn.login()
        vpn.authenticate_icourse()
        client.vpn = vpn
        client._userinfo = None


def ocr_worker(
    rows: list[dict],
    client: ICourseClient,
    ppt_pipeline: PPTPipeline,
    db: Database,
    reporter: Reporter,
) -> None:
    """Run PPT pipeline for each lecture in batch, push results to LLM queue."""
    for row in rows:
        sub_id = str(row["sub_id"])
        course_id = row["course_id"]
        course_title = row.get("course_title", "Unknown")
        sub_title = row.get("sub_title", sub_id)
        transcript = row.get("transcript") or ""

        try:
            reporter.resummarize_one(course_title, sub_title)
            check_session(client)

            if not transcript.strip():
                reporter.info(f"    Empty transcript, skipping.")
                continue

            ppt_pipeline.run_blocking(client, course_id, sub_id)

            kept_pages = db.get_done_ppt_pages(sub_id)
            llm_queue.put((sub_id, course_id, course_title, transcript, kept_pages))

        except Exception:
            reporter.info(f"    [FAIL] OCR {sub_id}:")
            traceback.print_exc()


def llm_worker(
    db: Database,
    summarizer: Summarizer,
    reporter: Reporter,
    total: int,
) -> None:
    """Pull finished-OCR lectures, call LLM, persist results."""
    done = 0
    while True:
        item = llm_queue.get()
        if item is None:
            llm_queue.task_done()
            break
        sub_id, course_id, course_title, transcript, kept_pages = item
        t0 = time.time()
        try:
            prompt_text, mode = assemble(transcript, None, kept_pages)
            reporter.info(
                f"    Prompt [{sub_id}]: mode={mode}, "
                f"{len(prompt_text)} chars, {len(kept_pages)} PPT pages"
            )
            summary, model_used = summarizer.summarize(course_title, prompt_text)
            db.update_summary_v2(sub_id, summary, model_used)
            db.reset_emailed(sub_id)
            done += 1
            reporter.info(
                f"    [OK] {sub_id} v2 by {model_used}: {len(summary)} chars "
                f"({time.time()-t0:.0f}s) [{done}/{total}]"
            )
        except Exception as e:
            reporter.info(f"    [FAIL] LLM {sub_id}: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            llm_queue.task_done()


def main():
    parser = argparse.ArgumentParser(
        description="Dual-pipeline resummarize (OCR ∥ LLM)"
    )
    parser.add_argument("--ocr-threads", type=int, default=OCR_THREADS)
    parser.add_argument("--llm-workers", type=int, default=LLM_WORKERS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--course-ids", type=str, default="")
    parser.add_argument(
        "--all",
        action="store_true",
        help="force every existing summary through the current LLM providers",
    )
    args = parser.parse_args()

    global llm_queue
    llm_queue = Queue(maxsize=args.llm_workers * 2)

    # ── Bootstrap ─────────────────────────────────────────────────────────
    db = Database()
    scheduler = Scheduler(reporter=Reporter())
    ppt_pipeline = PPTPipeline(db, scheduler, reporter=Reporter())

    print("[Login] WebVPN...")
    vpn = WebVPNSession()
    vpn.login()
    vpn.authenticate_icourse()
    client = ICourseClient(vpn)

    # ── Phase 1: Catalog — scan targets + register PPT pages ─────────────
    if args.course_ids:
        cids = [c.strip() for c in args.course_ids.split(",") if c.strip()]
        targets = db.get_lectures_to_resummarize_for_courses(
            cids, force_all=args.all,
        )
    else:
        if args.all:
            print("--all requires --course-ids to keep the rewrite scoped.")
            scheduler.shutdown()
            return
        targets = db.get_lectures_to_resummarize()
    if not targets:
        print("No lectures need resummarize.")
        scheduler.shutdown()
        return
    if args.limit:
        targets = targets[:args.limit]

    print(f"\n[Phase 1] Cataloging {len(targets)} lecture(s)...")
    t_start = time.time()
    total_pages = 0
    for row in targets:
        sub_id = str(row["sub_id"])
        course_id = row["course_id"]
        check_session(client)
        try:
            items = client.get_ppt_list(course_id, sub_id)
            if items:
                registered = db.insert_ppt_pages_pending(sub_id, items)
                total_pages += registered
        except Exception as e:
            print(f"  [WARN] PPT list for {sub_id}: {e}")
    print(f"  Registered {total_pages} pending pages ({time.time()-t_start:.0f}s)")

    # ── Phase 2: Dual pipeline ──────────────────────────────────────────
    print(f"\n[Phase 2] OCR threads={args.ocr_threads}, "
          f"LLM workers={args.llm_workers}")

    batch_size = max(1, len(targets) // args.ocr_threads)
    batches = [targets[i:i + batch_size] for i in
               range(0, len(targets), batch_size)]

    # LLM workers (IO-bound consumers)
    llm_threads = [
        threading.Thread(
            target=llm_worker,
            args=(db, Summarizer(), Reporter(), len(targets)),
            name=f"llm-{i}",
        ) for i in range(args.llm_workers)
    ]
    for t in llm_threads:
        t.start()

    # OCR threads (CPU-bound producers)
    ocr_threads = [
        threading.Thread(
            target=ocr_worker,
            args=(batch, client, ppt_pipeline, db, Reporter()),
            name=f"ocr-{i}",
        ) for i, batch in enumerate(batches)
    ]
    for t in ocr_threads:
        t.start()

    for t in ocr_threads:
        t.join()

    print("\n[Phase 2] OCR complete, draining LLM queue...")
    for _ in llm_threads:
        llm_queue.put(None)
    for t in llm_threads:
        t.join()

    elapsed = time.time() - t_start
    print(f"\nDone in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    scheduler.shutdown()


if __name__ == "__main__":
    main()
