"""iCourse Subscriber — top-level orchestration.

The runtime is split across cooperating components — Scheduler (pools +
resource monitor), Reporter (centralised logging), PPTPipeline, LectureRunner,
AudioDownloader.  This file does only orchestration:

  1. Build all components.
  2. Login + enumerate.
  3. Drive LectureRunner across the queued lectures.
  4. Resummarize old (pre-v2) lectures.
  5. Email + bookkeeping.
  6. Shutdown.

Anything more interesting belongs in one of ``src/*`` modules.
"""

import datetime
import time
import traceback
from typing import Callable

from src.runtime import config
from src.data.database import Database
from src.api.emailer import Emailer
from src.api.icourse import ICourseClient
from src.pipeline.lecture_runner import LectureRunner, resummarize_old_lectures
from src.pipeline.ppt_pipeline import PPTPipeline
from src.runtime.reporter import Reporter
from src.runtime.scheduler import Scheduler
from src.ai.summarizer import Summarizer
from src.ai.transcriber import Transcriber
from src.api.webvpn import WebVPNSession


def login_with_retry(max_attempts: int = 5) -> WebVPNSession:
    """Login to WebVPN + iCourse CAS, retrying on transient failures.

    The iCourse CAS step (authenticate_icourse) has its own inner retry
    loop for transient redirect-chain hiccups; this outer loop only runs
    when those inner retries are exhausted, which generally means the
    WebVPN session itself needs a fresh login.  5 attempts handles the
    long tail of times when CAS rejects multiple fresh sessions in a
    row before letting one through.
    """
    for attempt in range(max_attempts):
        try:
            vpn = WebVPNSession()
            print(f"\n[Login] WebVPN (attempt {attempt + 1}/{max_attempts})...")
            vpn.login()
            print("[Login] iCourse CAS...")
            vpn.authenticate_icourse()
            return vpn
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"  Failed: {type(e).__name__}: {e}; retrying...")
                time.sleep(5)
            else:
                raise


def _check_session(client: ICourseClient) -> None:
    """Verify WebVPN session; re-login in place if expired.

    Mutates ``client`` so background workers holding the same instance
    automatically pick up refreshed cookies.
    """
    if client.check_alive():
        return
    print("[Session] WebVPN session expired, re-logging in...")
    client.vpn = login_with_retry()
    client._userinfo = None


def _enumerate_lectures(client: ICourseClient, db: Database,
                        reporter: Reporter) -> list[tuple[str, str, dict]]:
    """Sync, fast: list every (course_id, course_title, lecture) we'll
    process this run.  Done up-front so the prefetch loop can see across
    course boundaries when picking the "next" lecture."""
    out: list[tuple[str, str, dict]] = []
    for course_id in config.COURSE_IDS:
        try:
            _check_session(client)
            detail = client.get_course_detail(course_id)
            course_title = detail["title"]
            teacher = detail["teacher"]
            lectures = detail["lectures"]
            playback_count = sum(1 for l in lectures if l.get("has_playback"))
            reporter.course_header(
                course_id, course_title, teacher,
                total=len(lectures), playback=playback_count,
            )
            db.upsert_course(course_id, course_title, teacher)

            # School system sometimes lists duplicate lectures; dedup the
            # raw list so the same logic produces the same outcome each run.
            seen_sub_titles: set[str] = set()
            deduped = []
            for lec in lectures:
                title = lec.get("sub_title", "")
                if title and title in seen_sub_titles:
                    reporter.course_dedup_skip(title, lec["sub_id"])
                    continue
                if title:
                    seen_sub_titles.add(title)
                deduped.append(lec)
            lectures = deduped

            known_processed = db.get_processed_sub_ids(course_id)
            new_lectures = [
                lec for lec in lectures
                if lec.get("has_playback")
                and str(lec["sub_id"]) not in known_processed
            ]
            unprocessed = db.get_unprocessed_lectures(course_id)
            new_ids = {str(lec["sub_id"]) for lec in new_lectures}
            retry_only = [
                {"sub_id": u["sub_id"], "sub_title": u["sub_title"],
                 "date": u["date"]}
                for u in unprocessed if u["sub_id"] not in new_ids
            ]
            new_lectures.extend(retry_only)
            reporter.course_new_count(len(new_lectures))
            if not new_lectures:
                continue

            for lecture in new_lectures:
                sub_id = str(lecture["sub_id"])
                db.insert_lecture(
                    sub_id, course_id,
                    lecture.get("sub_title", ""),
                    lecture.get("date", ""),
                )
                out.append((course_id, course_title, lecture))
        except Exception:
            reporter.course_enumeration_error(course_id)
            traceback.print_exc()
    return out


def _drive_lectures(client: ICourseClient, db: Database,
                    scheduler: Scheduler, transcriber: Transcriber,
                    summarizer: Summarizer, reporter: Reporter,
                    all_lectures: list[tuple[str, str, dict]],
                    email_items: list,
                    should_stop: Callable[[], bool] | None = None) -> None:
    """Phase 2: run each lecture through LectureRunner.

    Pre-schedules the first lecture's prefetch (audio + images) before
    entering the loop; subsequent prefetches are kicked off from inside
    each LectureRunner.run via ``next_info``.
    """
    if not all_lectures:
        return

    first_course, _, first_lec = all_lectures[0]
    scheduler.prefetch_lecture(client, first_course, str(first_lec["sub_id"]))

    runner = LectureRunner(
        client, db, scheduler, transcriber, summarizer, reporter,
    )

    for i, (course_id, course_title, lecture) in enumerate(all_lectures):
        if should_stop and should_stop():
            reporter.info(
                "[Runtime] Soft time limit reached; stopping before the "
                "next lecture so progress can be saved."
            )
            return

        sub_id = str(lecture["sub_id"])
        next_info: tuple[str, str] | None = None
        if i + 1 < len(all_lectures):
            next_course, _, next_lec = all_lectures[i + 1]
            next_info = (next_course, str(next_lec["sub_id"]))

        _check_session(client)
        try:
            summary = runner.run(
                course_id, course_title, lecture, next_info=next_info,
            )
            if summary:
                email_items.append({
                    "sub_id": sub_id,
                    "course_title": course_title,
                    "sub_title": lecture.get("sub_title", sub_id),
                    "date": lecture.get("date", ""),
                    "summary": summary,
                })
        except Exception:
            reporter.lecture_error(sub_id)
            traceback.print_exc()
        finally:
            # Belt-and-braces: drop any lingering prefetch entry for this
            # lecture so we don't leak bytes if the runner crashed before
            # PPTPipeline.submit released the cache.
            scheduler.image_cache.discard(sub_id)
            scheduler.audio_downloader.release(sub_id)


def _send_email(emailer: Emailer | None, db: Database, reporter: Reporter,
                email_items: list) -> None:
    """Append any previously-processed-but-unsent lectures, then send."""
    unsent = db.get_unsent_lectures()
    if unsent:
        seen_sub_ids = {item["sub_id"] for item in email_items}
        for row in unsent:
            if row["sub_id"] not in seen_sub_ids:
                email_items.append({
                    "sub_id": row["sub_id"],
                    "course_title": row["course_title"],
                    "sub_title": row["sub_title"],
                    "date": row["date"],
                    "summary": row["summary"],
                })
        reporter.email_recovered_unsent(len(unsent))

    if not (emailer and email_items):
        return
    try:
        reporter.email_summary(len(email_items))
        if emailer.send(email_items):
            db.mark_emailed_batch([item["sub_id"] for item in email_items])
        else:
            reporter.email_failed()
    except Exception:
        reporter.info("[Email] Failed to send:")
        traceback.print_exc()


def _crawl_semester_catalog(client: ICourseClient, db: Database,
                            reporter: Reporter) -> None:
    """Auto-discover every available semester and refresh ``all_courses``.

    Walks every page of get-course-list for each discovered term and
    replaces the term's catalog in one transaction.  No longer requires
    the ``CRAWL_TERM`` secret — the API tells us what terms exist.
    """
    reporter.info("Discovering available semesters from API...")
    try:
        _check_session(client)
        terms = client.discover_terms()
    except Exception as e:
        reporter.crawl_courses_failed("discovery", e)
        return

    if not terms:
        reporter.info("No semesters found via API discovery.")
        return

    reporter.info(f"Found {len(terms)} semester(s): "
                  f"{', '.join(t['name'] for t in terms)}")

    for term_info in terms:
        code = term_info["code"]
        name = term_info["name"]
        expected = term_info["count"]
        reporter.crawl_courses_start(name)
        t0 = time.time()
        try:
            _check_session(client)
            rows = client.list_semester_courses(code)
            if not rows:
                reporter.info(f"  Term {name}: API returned 0 courses, skipping.")
                continue
            # Pass the human-readable term name (not the API code) to the
            # DB so the frontend displays "2025-20262" instead of "25".
            deleted, upserted = db.upsert_all_courses_for_term(name, rows)
            reporter.crawl_courses_done(
                name, len(rows), deleted, upserted, time.time() - t0,
            )
        except Exception as e:
            reporter.crawl_courses_failed(name, e)
        reporter.info(f"  ({code}) → {expected} API courses, "
                      f"{len(rows)} fetched")

    reporter.info("Semester catalog crawl complete.")


def run():
    """Single execution of the full pipeline."""
    reporter = Reporter()
    reporter.run_header()
    run_started_at = time.time()

    def should_stop_for_time() -> bool:
        if config.RUN_SOFT_TIMEOUT_MINUTES <= 0:
            return False
        return (time.time() - run_started_at) >= (
            config.RUN_SOFT_TIMEOUT_MINUTES * 60
        )

    if not config.COURSE_IDS and not config.CRAWL_TERM:
        reporter.info(
            "No COURSE_IDS configured. Set COURSE_IDS to process lectures "
            "or leave empty for crawl-only mode."
        )
        # Fall through — crawl-only mode is valid.

    db = Database()
    transcriber = Transcriber()
    summarizer = Summarizer() if config.COURSE_IDS else None
    emailer = Emailer() if (
        config.SMTP_EMAIL and config.SMTP_PASSWORD
    ) else None

    vpn = login_with_retry()
    client = ICourseClient(vpn)
    email_items: list = []

    # Refresh the semester catalog: run on the 5th and 25th of each month,
    # or immediately if the database has no catalog data yet.
    has_catalog = db.has_all_courses()
    today = datetime.datetime.now().day
    if not has_catalog or today in (5, 25):
        _crawl_semester_catalog(client, db, reporter)
    else:
        reporter.info("Skipping catalog crawl (has data, not the 5th or 25th).")

    if not config.COURSE_IDS:
        # Crawl-only mode: nothing to process, just persist + exit.
        reporter.info("\n[Crawl-only mode] No COURSE_IDS — skipping lectures.")
        reporter.run_footer()
        return

    scheduler = Scheduler(reporter=reporter)

    try:
        all_lectures = _enumerate_lectures(client, db, reporter)
        _drive_lectures(
            client, db, scheduler, transcriber, summarizer, reporter,
            all_lectures, email_items, should_stop=should_stop_for_time,
        )

        # Resummarize old (pre-v2) lectures, scoped to the courses we're
        # actively monitoring this run.  Opt-in via RESUMMARIZE_OLD=1
        # because re-OCR + re-LLM on every stale lecture turns a 5-min
        # nightly run into a 2-hour one; flip on for one-shot manual runs.
        if config.RESUMMARIZE_OLD_ENABLED and should_stop_for_time():
            reporter.info(
                "\n[Resummarize] Skipping — soft time limit reached."
            )
        elif config.RESUMMARIZE_OLD_ENABLED:
            try:
                _check_session(client)
                ppt_pipeline = PPTPipeline(db, scheduler, reporter)
                resummarize_old_lectures(
                    client, db, summarizer, ppt_pipeline, reporter,
                    email_items, config.COURSE_IDS,
                    check_session_fn=_check_session,
                    should_stop=should_stop_for_time,
                    force_all=config.RESUMMARIZE_ALL_ENABLED,
                )
            except Exception:
                reporter.info("[Resummarize] phase errored:")
                traceback.print_exc()
        else:
            reporter.info(
                "\n[Resummarize] Skipping — set RESUMMARIZE_OLD=1 to enable."
            )
    finally:
        scheduler.shutdown()

    _send_email(emailer, db, reporter, email_items)
    reporter.run_footer()


if __name__ == "__main__":
    run()
