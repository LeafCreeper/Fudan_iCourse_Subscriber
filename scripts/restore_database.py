#!/usr/bin/env python3
"""Restore the data branch atomically; existing corrupt data must fail closed."""

import argparse
import gzip
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data import crypto_box
from src.data.sharder import load_index, reassemble_database


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE).stdout


def restore_database(*, allow_empty: bool = False) -> bool:
    try:
        git("ls-remote", "--exit-code", "origin", "refs/heads/data")
    except subprocess.CalledProcessError as error:
        if error.returncode == 2 and allow_empty:
            print("No data branch; starting with an empty database.")
            return False
        raise

    git("fetch", "--depth=1", "origin", "+refs/heads/data:refs/remotes/origin/data")
    files = set(git("ls-tree", "-r", "--name-only", "origin/data", "--", "data/").decode().splitlines())
    stuid, uispsw = os.environ.get("STUID", ""), os.environ.get("UISPSW", "")
    if not stuid or not uispsw:
        raise ValueError("STUID and UISPSW are required to restore an existing database")

    destination = Path("data/icourse.db")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        temporary = Path(directory)
        restored = temporary / "restored.db"
        if "data/icourse-index.enc" in files:
            index_path = temporary / "icourse-index.enc"
            index_path.write_bytes(git("show", "origin/data:data/icourse-index.enc"))
            password = crypto_box.derive_new_password(stuid, uispsw)
            index = load_index(str(index_path), password)
            shards = temporary / "shards"
            shards.mkdir()
            if not isinstance(index.get("shards"), list) or not index["shards"]:
                raise ValueError("The existing database index has no shards")
            for shard in index["shards"]:
                name = shard["name"]
                if not re.fullmatch(r"[A-Za-z0-9_-]+\.db\.gz\.enc", name):
                    raise ValueError("Invalid shard filename in database index")
                path = f"data/shards/{name}"
                if path not in files:
                    raise ValueError("The existing database is missing a required shard")
                (shards / name).write_bytes(git("show", f"origin/data:{path}"))
            reassemble_database(index, str(shards), str(restored), password)
        else:
            source = next((path for path in ("data/icourse.db.gz.enc", "data/icourse.db.enc") if path in files), None)
            if source is None:
                raise ValueError("The data branch contains no supported database; refusing to overwrite it")
            compressed = source.endswith(".gz.enc")
            plaintext, _ = crypto_box.decrypt_with_fallback(
                git("show", f"origin/data:{source}"), stuid=stuid, uispsw=uispsw,
                dashscope=os.environ.get("DASHSCOPE_API_KEY", ""),
                smtp=os.environ.get("SMTP_PASSWORD", ""),
                validate=crypto_box.is_gzip if compressed else crypto_box.is_sqlite,
            )
            restored.write_bytes(gzip.decompress(plaintext) if compressed else plaintext)

        connection = sqlite3.connect(restored.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("Restored database failed its integrity check")
            for table in ("courses", "lectures"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                    raise ValueError("Restored database is missing a required table")
        finally:
            connection.close()
        os.replace(restored, destination)
    print("Database restored and validated.")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-empty", action="store_true")
    options = parser.parse_args()
    restore_database(allow_empty=options.allow_empty)
