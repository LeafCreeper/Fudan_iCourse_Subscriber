#!/usr/bin/env python3
"""Delete requested records using bound parameters and course-scoped queries."""

import os
import re
import sqlite3
import sys
from pathlib import Path


def parse_ids(value: str, *, required: bool = False) -> list[str]:
    ids = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if required and not ids:
        raise ValueError("At least one course ID is required")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item) for item in ids):
        raise ValueError("IDs must contain only letters, digits, underscores or hyphens")
    return ids


def delete_records(db_path: str, course_ids: str, sub_ids: str = "") -> int:
    courses = parse_ids(course_ids, required=True)
    lectures = parse_ids(sub_ids)
    if sub_ids.strip() and not lectures:
        raise ValueError("A supplied lecture filter must contain at least one ID")
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True)
    deleted = 0
    try:
        with connection:
            for course_id in courses:
                if lectures:
                    for sub_id in lectures:
                        connection.execute(
                            "DELETE FROM ppt_pages WHERE sub_id IN "
                            "(SELECT sub_id FROM lectures WHERE course_id=? AND sub_id=?)",
                            (course_id, sub_id),
                        )
                        deleted += connection.execute(
                            "DELETE FROM lectures WHERE course_id=? AND sub_id=?",
                            (course_id, sub_id),
                        ).rowcount
                else:
                    connection.execute(
                        "DELETE FROM ppt_pages WHERE sub_id IN "
                        "(SELECT sub_id FROM lectures WHERE course_id=?)", (course_id,),
                    )
                    deleted += connection.execute(
                        "DELETE FROM lectures WHERE course_id=?", (course_id,),
                    ).rowcount
                connection.execute(
                    "DELETE FROM courses WHERE course_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM lectures WHERE course_id=?)", (course_id, course_id),
                )
        return deleted
    finally:
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: delete_course_records.py DATABASE")
    count = delete_records(
        sys.argv[1], os.environ.get("DELETE_COURSE_IDS", ""),
        os.environ.get("DELETE_SUB_IDS", ""),
    )
    print(f"Deleted {count} lecture(s)")
