#!/usr/bin/env python3
"""Merge local DB into remote DB (additive-only).

Used at deploy time to safely combine results from concurrent workflow runs.
For each lecture row, fields only progress forward (null -> non-null).
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.schema import (
    LECTURES_MIGRATION_COLUMNS,
    PPT_PAGES_MIGRATION_COLUMNS,
    SCHEMA_SQL,
    requeue_incomplete_lectures,
)


def _ensure_schema(conn: sqlite3.Connection):
    """Create tables and migration columns if missing in remote DB."""
    conn.executescript(SCHEMA_SQL)
    existing_lectures = {r[1] for r in conn.execute("PRAGMA table_info(lectures)")}
    for col, typedef in LECTURES_MIGRATION_COLUMNS:
        if col not in existing_lectures:
            conn.execute(f"ALTER TABLE lectures ADD COLUMN {col} {typedef}")

    existing_ppt = {r[1] for r in conn.execute("PRAGMA table_info(ppt_pages)")}
    for col, typedef in PPT_PAGES_MIGRATION_COLUMNS:
        if col not in existing_ppt:
            conn.execute(f"ALTER TABLE ppt_pages ADD COLUMN {col} {typedef}")


def _migrate_attached(conn: sqlite3.Connection, schema: str):
    """Bring an ATTACHed database up to the current column set.

    The merge below SELECTs migration columns (e.g. ``dhash``) from the
    local side; a local DB written by an older code version would make
    those statements crash with "no such column".  ALTER TABLE works on
    attached schemas, so pad the missing columns with NULLs first.
    """
    for table, cols in (
        ("lectures", LECTURES_MIGRATION_COLUMNS),
        ("ppt_pages", PPT_PAGES_MIGRATION_COLUMNS),
    ):
        existing = {
            r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")
        }
        if not existing:
            continue  # table absent entirely; nothing to pad
        for col, typedef in cols:
            if col not in existing:
                conn.execute(
                    f"ALTER TABLE {schema}.{table} ADD COLUMN {col} {typedef}"
                )


def merge(local_path: str, remote_path: str):
    """Merge local changes into remote DB.  Only adds/progresses, never deletes."""
    conn = sqlite3.connect(remote_path)
    _ensure_schema(conn)
    conn.execute("ATTACH DATABASE ? AS local", (local_path,))
    _migrate_attached(conn, "local")

    try:
        with conn:
            # Normalize both inputs before COALESCE merges timestamps and
            # clears errors. This preserves retries repaired by Database.
            requeue_incomplete_lectures(conn)
            requeue_incomplete_lectures(conn, "local")
            # 1) Courses: upsert
            conn.execute("""
                INSERT OR REPLACE INTO main.courses (course_id, title, teacher)
                SELECT course_id, title, teacher FROM local.courses
            """)

            # 2) Lectures: insert rows that only exist in local
            conn.execute("""
                INSERT OR IGNORE INTO main.lectures
                    (sub_id, course_id, sub_title, date, transcript, summary,
                     processed_at, emailed_at, error_msg, error_count, error_stage,
                     summary_model)
                SELECT sub_id, course_id, sub_title, date, transcript, summary,
                       processed_at, emailed_at, error_msg, error_count, error_stage,
                       summary_model
                FROM local.lectures
            """)

            # 3) Lectures: merge existing rows (progress forward only)
            #    - Progress fields: COALESCE(local, remote) — prefer non-null
            #    - Error fields: clear if processed, otherwise keep the most info
            conn.execute("""
                UPDATE main.lectures SET
                    transcript    = COALESCE(l.transcript,    main.lectures.transcript),
                    summary       = COALESCE(l.summary,       main.lectures.summary),
                    summary_model = COALESCE(l.summary_model, main.lectures.summary_model),
                    processed_at  = COALESCE(l.processed_at,  main.lectures.processed_at),
                    emailed_at    = COALESCE(l.emailed_at,    main.lectures.emailed_at),
                    error_msg = CASE
                        WHEN COALESCE(l.processed_at, main.lectures.processed_at) IS NOT NULL
                        THEN NULL
                        ELSE COALESCE(l.error_msg, main.lectures.error_msg)
                    END,
                    error_count = CASE
                        WHEN COALESCE(l.processed_at, main.lectures.processed_at) IS NOT NULL
                        THEN 0
                        ELSE MAX(COALESCE(l.error_count, 0), COALESCE(main.lectures.error_count, 0))
                    END,
                    error_stage = CASE
                        WHEN COALESCE(l.processed_at, main.lectures.processed_at) IS NOT NULL
                        THEN NULL
                        ELSE COALESCE(l.error_stage, main.lectures.error_stage)
                    END
                FROM local.lectures l
                WHERE main.lectures.sub_id = l.sub_id
            """)

            # 4) PPT pages: insert local-only rows.  Existing rows are left
            # untouched — as-is by design: a pre-existing bug left many rows
            # stuck at 'pending' that are really invalid/dedup results, and
            # "progressing" them here would resurrect that garbage.  If a
            # row is already in the remote DB, whatever wrote it owns it.
            conn.execute("""
                INSERT OR IGNORE INTO main.ppt_pages
                    (sub_id, page_num, created_sec, pptimgurl, text, ocr_status, ocr_at, dhash)
                SELECT sub_id, page_num, created_sec, pptimgurl, text, ocr_status, ocr_at, dhash
                FROM local.ppt_pages
            """)

            # 6) all_courses (catalog): upsert local rows into remote.  We take
            #    the side with the newer ``last_seen_at`` so a stale local crawl
            #    can't overwrite a fresher remote one.  We deliberately don't
            #    delete from remote — local's upsert_all_courses_for_term may
            #    have hard-deleted dropped courses for the term it crawled, but
            #    we can't tell here which terms were "intentionally crawled"
            #    vs. "stale snapshot".  Frontend filters on last_seen_at for
            #    freshness instead.
            #
            #    Guarded: workflows running against a pre-catalog local DB will
            #    lack the table entirely; in that case there's nothing to merge.
            has_all_courses = conn.execute(
                "SELECT 1 FROM local.sqlite_master "
                "WHERE type='table' AND name='all_courses'"
            ).fetchone()
            if has_all_courses:
                conn.execute("""
                    INSERT INTO main.all_courses
                        (course_id, term, title, teacher, dept, last_seen_at)
                    SELECT course_id, term, title, teacher, dept, last_seen_at
                    FROM local.all_courses
                    WHERE true
                    ON CONFLICT(course_id, term) DO UPDATE SET
                        title       = excluded.title,
                        teacher     = excluded.teacher,
                        dept        = excluded.dept,
                        last_seen_at = excluded.last_seen_at
                    WHERE excluded.last_seen_at > all_courses.last_seen_at
                """)

    finally:
        # Persist COURSE_IDS from the CI secret into the meta table so
        # the frontend can read the current subscription list from the
        # metadata shard without relying on localStorage alone.
        course_ids_env = os.environ.get("COURSE_IDS", "")
        if course_ids_env:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('course_ids', ?)", (course_ids_env,),
            )
            conn.commit()

        try:
            conn.execute("DETACH DATABASE local")
        except sqlite3.Error:
            pass
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} LOCAL_DB REMOTE_DB")
        print("Merges LOCAL_DB into REMOTE_DB (additive-only).")
        sys.exit(1)
    merge(sys.argv[1], sys.argv[2])
    print("Merge complete.")
