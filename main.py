"""iCourse Subscriber — top-level orchestration.

The runtime is split across cooperating components — Scheduler (pools +
resource monitor), Reporter (centralised logging), PPTPipeline, LectureRunner,
AudioDownloader.  This file does only orchestration:

  1. Build all components.
  2. Login + enumerate.
  3. Drive LectureRunner across the queued lectures.
  4. Email + bookkeeping.
  5. Shutdown.

Anything more interesting belongs in one of ``src/*`` modules.
"""

import time
import traceback

from src.runtime import config
from src.data.database import Database
from src.api.emailer import Emailer
from src.api.icourse import ICourseClient
from src.pipeline.lecture_runner import LectureRunner
from src.runtime.reporter import Reporter
from src.runtime.scheduler import Scheduler
from src.ai.summarizer import Summarizer
from src.ai.transcriber import Transcriber
from src.api.webvpn import WebVPNSession


def login_with_retry(max_attempts: int = 10) -> WebVPNSession:
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
            # When a sub_title appears more than once, keep the first one
            # that has playback; if none have playback, keep the first.
            seen_sub_titles: dict[str, dict] = {}
            deduped = []
            for lec in lectures:
                title = lec.get("sub_title", "")
                if not title:
                    deduped.append(lec)
                    continue
                existing = seen_sub_titles.get(title)
                if existing is None:
                    seen_sub_titles[title] = lec
                    deduped.append(lec)
                elif not existing.get("has_playback") and lec.get("has_playback"):
                    # replace the earlier no-playback entry in place so the
                    # processing order stays chronological
                    deduped[deduped.index(existing)] = lec
                    seen_sub_titles[title] = lec
                    reporter.course_dedup_skip(title, existing["sub_id"])
                else:
                    reporter.course_dedup_skip(title, lec["sub_id"])
            lectures = deduped

            ineligible = db.get_ineligible_sub_ids(course_id)
            for sub_id in sorted(ineligible - db.get_processed_sub_ids(course_id)):
                reporter.lecture_retry_exhausted(sub_id)
            new_lectures = [
                lec for lec in lectures
                if lec.get("has_playback")
                and str(lec["sub_id"]) not in ineligible
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
                    email_items: list) -> None:
    """Phase 2: run each lecture through LectureRunner.

    Pre-schedules the first lecture's prefetch (audio + images) before
    entering the loop; subsequent prefetches are kicked off from inside
    each LectureRunner.run via ``next_info``.
    """
    if not all_lectures:
        return

    runner = LectureRunner(
        client, db, scheduler, transcriber, summarizer, reporter,
        ensure_session=lambda: _check_session(client),
    )

    first_course, _, first_lec = all_lectures[0]
    runner.prefetch_first(first_course, str(first_lec["sub_id"]))

    for i, (course_id, course_title, lecture) in enumerate(all_lectures):
        sub_id = str(lecture["sub_id"])
        next_info: tuple[str, str] | None = None
        if i + 1 < len(all_lectures):
            next_course, _, next_lec = all_lectures[i + 1]
            next_info = (next_course, str(next_lec["sub_id"]))

        try:
            _check_session(client)
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
            else:
                row = db.get_lecture(sub_id)
                if row and row.get("error_stage") not in (None, "", "no_video"):
                    reporter.lecture_error(sub_id)
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
        elif emailer.last_error_code == 550 and len(email_items) > 1:
            reporter.info("[Email] Digest rejected; isolating individual lectures.")
            for item in email_items:
                try:
                    delivered = emailer.send([item])
                except Exception:
                    delivered = False
                    traceback.print_exc()
                if delivered:
                    db.mark_emailed_batch([item["sub_id"]])
                else:
                    reporter.email_failed(item["sub_id"])
        else:
            reporter.email_failed()
    except Exception:
        reporter.email_failed()
        reporter.info("[Email] Failed to send:")
        traceback.print_exc()


def _crawl_semester_catalog(client: ICourseClient, db: Database,
                            reporter: Reporter) -> None:
    """Discover semesters on every run and fetch catalogs for new terms.

    Compare API term names with the names stored in ``all_courses``.
    Only terms with a successfully fetched catalog are considered known,
    so empty or failed fetches are retried on the next run.
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

    known_terms = db.list_catalog_terms()
    new_terms = [term for term in terms if term["name"] not in known_terms]
    if not new_terms:
        reporter.info("Skipping catalog crawl (no new semesters).")
        return

    for term_info in new_terms:
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
            if len(rows) < expected:
                reporter.info(
                    f"  Term {name}: fetched {len(rows)} of {expected} courses, "
                    "deferring catalog update until the next run."
                )
                continue
            # Pass the human-readable term name (not the API code) to the
            # DB so the frontend displays "2025-20262" instead of "25".
            deleted, upserted = db.upsert_all_courses_for_term(name, rows)
            reporter.crawl_courses_done(
                name, len(rows), deleted, upserted, time.time() - t0,
            )
        except Exception as e:
            reporter.crawl_courses_failed(name, e)
            continue
        reporter.info(f"  ({code}) → {expected} API courses, "
                      f"{len(rows)} fetched")

    reporter.info("Semester catalog crawl complete.")


def run():
    """Single execution of the full pipeline."""
    reporter = Reporter()
    reporter.run_header()

    if not config.COURSE_IDS and not config.CRAWL_TERM:
        reporter.info(
            "No COURSE_IDS configured. Set COURSE_IDS to process lectures "
            "or leave empty for crawl-only mode."
        )
        # Fall through — crawl-only mode is valid.

    db = Database()
    corrected = db.sync_dates_from_sub()
    if corrected:
        print(f"  [Date] Synced {corrected} lecture date(s) from sub_title", flush=True)
    transcriber = Transcriber()
    summarizer = Summarizer() if config.COURSE_IDS else None
    emailer = Emailer() if (
        config.SMTP_EMAIL and config.SMTP_PASSWORD
    ) else None

    vpn = login_with_retry()
    client = ICourseClient(vpn)
    email_items: list = []

    # Discover new semesters every run; only fetch catalogs not yet stored.
    _crawl_semester_catalog(client, db, reporter)

    if not config.COURSE_IDS:
        # Crawl-only mode: nothing to process, just persist + exit.
        reporter.info("\n[Crawl-only mode] No COURSE_IDS — skipping lectures.")
        reporter.run_footer()
        reporter.raise_if_failed()
        return

    scheduler = Scheduler(reporter=reporter)

    try:
        all_lectures = _enumerate_lectures(client, db, reporter)
        _drive_lectures(
            client, db, scheduler, transcriber, summarizer, reporter,
            all_lectures, email_items,
        )

    finally:
        scheduler.shutdown()

    _send_email(emailer, db, reporter, email_items)
    reporter.run_footer()
    reporter.raise_if_failed()


if __name__ == "__main__":
    run()
