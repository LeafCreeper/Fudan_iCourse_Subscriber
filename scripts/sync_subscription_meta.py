#!/usr/bin/env python3
"""Record the subscription secret snapshot in the encrypted database."""

import datetime
import os
import sqlite3
import sys


def sync(db_path: str) -> None:
    captured_at = os.environ.get("SUBSCRIPTION_SNAPSHOT_AT") or (
        datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [
                ("course_ids", os.environ["COURSE_IDS"]),
                ("course_ids_synced_at", captured_at),
            ],
        )


if __name__ == "__main__":
    sync(sys.argv[1])
