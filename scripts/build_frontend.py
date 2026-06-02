#!/usr/bin/env python3
"""Build the frontend dist/ with encrypted JSON shards for lazy-loading.

Usage:
    python scripts/build_frontend.py --data-dir enc_data --output dist

Reads STUID/UISPSW from environment (or .env.test for local dev).
Decrypts the data-branch shards, splits into per-lecture JSON files,
encrypts each with HKDF-derived keys, and assembles the final dist/.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_crypto import (
    ITERATIONS,
    derive_master_key,
    encrypt_file,
)
from src.data.crypto_box import (
    decrypt,
    derive_new_password,
    is_gzip,
    is_json_obj,
)

PBKDF2_SALT = b"ICSv3-pages-salt"  # fixed, stored in meta.json


def _load_env():
    """Load STUID/UISPSW from env or .env.test."""
    stuid = os.environ.get("STUID") or os.environ.get("StuId", "")
    uispsw = os.environ.get("UISPSW") or os.environ.get("UISPsw", "")
    if not stuid or not uispsw:
        env_test = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".env.test",
        )
        if os.path.exists(env_test):
            for line in open(env_test):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            stuid = os.environ.get("stuid") or os.environ.get("STUID", "")
            uispsw = os.environ.get("uispsw") or os.environ.get("UISPSW", "")
    if not stuid or not uispsw:
        print("error: STUID and UISPSW required", file=sys.stderr)
        sys.exit(1)
    return stuid, uispsw


def _reassemble_to_memory(data_dir: str, v2_password: str) -> sqlite3.Connection:
    """Decrypt shards and reassemble into an in-memory SQLite DB."""
    index_path = os.path.join(data_dir, "data", "icourse-index.enc")
    if not os.path.exists(index_path):
        # Try flat layout
        index_path = os.path.join(data_dir, "icourse-index.enc")
    with open(index_path, "rb") as f:
        index_enc = f.read()
    index_json = json.loads(decrypt(index_enc, v2_password))

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS courses (course_id TEXT PRIMARY KEY, title TEXT, teacher TEXT);
        CREATE TABLE IF NOT EXISTS lectures (
            sub_id TEXT PRIMARY KEY, course_id TEXT, sub_title TEXT, date TEXT,
            transcript TEXT, summary TEXT, processed_at TEXT, emailed_at TEXT,
            error_msg TEXT, error_count INTEGER, error_stage TEXT,
            summary_model TEXT, summary_format_version TEXT, old_summary TEXT
        );
        CREATE TABLE IF NOT EXISTS ppt_pages (
            sub_id TEXT, page_num INTEGER, created_sec INTEGER, pptimgurl TEXT,
            text TEXT, ocr_status TEXT, ocr_at TEXT, dhash TEXT,
            PRIMARY KEY (sub_id, page_num)
        );
        CREATE TABLE IF NOT EXISTS all_courses (
            course_id TEXT, term TEXT, title TEXT, teacher TEXT, dept TEXT, last_seen_at TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)

    shards_dir = os.path.join(os.path.dirname(index_path), "shards")
    for shard in index_json.get("shards", []):
        shard_path = os.path.join(shards_dir, shard["name"])
        with open(shard_path, "rb") as f:
            encrypted = f.read()
        gzipped = decrypt(encrypted, v2_password)
        if not is_gzip(gzipped):
            raise ValueError(f"shard {shard['name']} not gzip after decrypt")
        raw = gzip.decompress(gzipped)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            conn.execute("ATTACH DATABASE ? AS shard", (tmp_path,))
            # Copy rows from each table that exists in the shard
            for table in ("courses", "lectures", "ppt_pages", "all_courses", "meta"):
                try:
                    conn.execute(
                        f"INSERT OR IGNORE INTO main.{table} "
                        f"SELECT * FROM shard.{table}"
                    )
                except sqlite3.OperationalError:
                    # Table might not exist in shard or column mismatch - try column-matched insert
                    try:
                        main_cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]
                        shard_cols = [r[1] for r in conn.execute(f"PRAGMA shard.table_info('{table}')").fetchall()]
                        common = [c for c in main_cols if c in shard_cols]
                        if common:
                            cols = ",".join(common)
                            conn.execute(f"INSERT OR IGNORE INTO main.{table} ({cols}) SELECT {cols} FROM shard.{table}")
                    except sqlite3.OperationalError:
                        pass
            conn.commit()
            conn.execute("DETACH DATABASE shard")
        finally:
            os.unlink(tmp_path)

    return conn


def _derive_state(row):
    if row["error_stage"]:
        return "failed"
    if row["summary"] and row["processed_at"]:
        return "ready"
    if row["transcript"] and not row["summary"]:
        return "processing"
    return "waiting"


def _build_index(conn: sqlite3.Connection) -> dict:
    """Build the index JSON with course list + lecture skeletons."""
    courses = []
    for r in conn.execute(
        "SELECT c.course_id, c.title, c.teacher, "
        "COUNT(CASE WHEN l.summary IS NOT NULL THEN 1 END) AS summary_count, "
        "COUNT(l.sub_id) AS total_count, "
        "MAX(l.processed_at) AS last_updated "
        "FROM courses c LEFT JOIN lectures l ON c.course_id = l.course_id "
        "GROUP BY c.course_id ORDER BY last_updated DESC"
    ):
        courses.append({
            "course_id": r["course_id"], "title": r["title"],
            "teacher": r["teacher"], "summary_count": r["summary_count"],
            "total_count": r["total_count"], "last_updated": r["last_updated"],
        })

    lectures = []
    for r in conn.execute(
        "SELECT sub_id, course_id, sub_title, date, processed_at, "
        "error_stage, summary_model, "
        "CASE WHEN summary IS NOT NULL THEN 1 ELSE 0 END AS has_summary, "
        "CASE WHEN transcript IS NOT NULL THEN 1 ELSE 0 END AS has_transcript "
        "FROM lectures ORDER BY sub_id ASC"
    ):
        state = "waiting"
        if r["error_stage"]:
            state = "failed"
        elif r["has_summary"] and r["processed_at"]:
            state = "ready"
        elif r["has_transcript"] and not r["has_summary"]:
            state = "processing"
        lectures.append({
            "sub_id": r["sub_id"], "course_id": r["course_id"],
            "sub_title": r["sub_title"], "date": r["date"],
            "processed_at": r["processed_at"], "state": state,
            "summary_model": r["summary_model"],
        })

    return {"courses": courses, "lectures": lectures}


def _build_search_index(conn: sqlite3.Connection) -> list:
    """Build a simple search index: list of {sub_id, sub_title, course_id, snippets}."""
    entries = []
    for r in conn.execute(
        "SELECT l.sub_id, l.sub_title, l.course_id, l.summary, l.transcript, "
        "c.title AS course_title "
        "FROM lectures l JOIN courses c ON l.course_id = c.course_id "
        "WHERE l.summary IS NOT NULL"
    ):
        # Store first 200 chars of summary as searchable snippet
        entry = {
            "sub_id": r["sub_id"],
            "sub_title": r["sub_title"],
            "course_id": r["course_id"],
            "course_title": r["course_title"],
            "summary_snippet": (r["summary"] or "")[:200],
            "transcript_snippet": (r["transcript"] or "")[:200],
        }
        entries.append(entry)
    return entries


def _encrypt_and_write(data: bytes, master_key: bytes, file_id: str, path: str):
    """Gzip + HKDF-encrypt and write to path."""
    compressed = gzip.compress(data, compresslevel=9)
    encrypted = encrypt_file(compressed, master_key, file_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(encrypted)


def build(data_dir: str, output_dir: str):
    stuid, uispsw = _load_env()
    v2_password = derive_new_password(stuid, uispsw)

    print("Reassembling database from shards...")
    conn = _reassemble_to_memory(data_dir, v2_password)

    # Derive V3 master key
    password_v3 = hashlib.sha256(f"ICSv3:{stuid}:{uispsw}".encode()).hexdigest()
    master_key = derive_master_key(password_v3, PBKDF2_SALT)

    # Prepare output
    dist_data = os.path.join(output_dir, "data")
    os.makedirs(dist_data, exist_ok=True)
    os.makedirs(os.path.join(dist_data, "lectures"), exist_ok=True)
    os.makedirs(os.path.join(dist_data, "ppt"), exist_ok=True)

    # 1) meta.json (plaintext)
    meta = {
        "version": 5,
        "pbkdf2_salt": PBKDF2_SALT.hex(),
        "iterations": ITERATIONS,
        "hkdf_hash": "SHA-256",
        "format": "ICSv3",
    }
    meta_path = os.path.join(dist_data, "meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    print(f"  meta.json ({os.path.getsize(meta_path)} bytes)")

    # 2) index.enc
    index_data = _build_index(conn)
    index_json = json.dumps(index_data, ensure_ascii=False).encode("utf-8")
    index_path = os.path.join(dist_data, "index.enc")
    _encrypt_and_write(index_json, master_key, "index", index_path)
    print(f"  index.enc ({os.path.getsize(index_path)} bytes) - "
          f"{len(index_data['courses'])} courses, {len(index_data['lectures'])} lectures")

    # 3) Per-lecture files
    lecture_count = 0
    for r in conn.execute(
        "SELECT sub_id, transcript, summary, summary_model FROM lectures "
        "WHERE summary IS NOT NULL OR transcript IS NOT NULL"
    ):
        lecture_json = json.dumps({
            "transcript": r["transcript"],
            "summary": r["summary"],
            "summary_model": r["summary_model"],
        }, ensure_ascii=False).encode("utf-8")
        file_id = f"lecture-{r['sub_id']}"
        path = os.path.join(dist_data, "lectures", f"{r['sub_id']}.enc")
        _encrypt_and_write(lecture_json, master_key, file_id, path)
        lecture_count += 1
    print(f"  lectures/ ({lecture_count} files)")

    # 4) Per-course PPT files
    ppt_count = 0
    course_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT course_id FROM ppt_pages pp "
        "JOIN lectures l ON pp.sub_id = l.sub_id "
        "WHERE pp.ocr_status = 'done' AND pp.text IS NOT NULL AND pp.text != ''"
    ).fetchall()]
    for cid in course_ids:
        pages = []
        for r in conn.execute(
            "SELECT pp.sub_id, pp.page_num, pp.text, pp.created_sec "
            "FROM ppt_pages pp JOIN lectures l ON pp.sub_id = l.sub_id "
            "WHERE l.course_id = ? AND pp.ocr_status = 'done' "
            "AND pp.text IS NOT NULL AND pp.text != '' "
            "ORDER BY pp.created_sec ASC",
            (cid,),
        ):
            pages.append({
                "sub_id": r["sub_id"], "page_num": r["page_num"],
                "text": r["text"], "created_sec": r["created_sec"],
            })
        if pages:
            ppt_json = json.dumps(pages, ensure_ascii=False).encode("utf-8")
            file_id = f"ppt-{cid}"
            path = os.path.join(dist_data, "ppt", f"{cid}.enc")
            _encrypt_and_write(ppt_json, master_key, file_id, path)
            ppt_count += 1
    print(f"  ppt/ ({ppt_count} files)")

    # 5) Catalog (all_courses for subscription editor)
    catalog_rows = []
    for r in conn.execute(
        "SELECT course_id, term, title, teacher, dept FROM all_courses ORDER BY term DESC, title"
    ):
        catalog_rows.append({
            "course_id": r["course_id"], "term": r["term"],
            "title": r["title"], "teacher": r["teacher"], "dept": r["dept"],
        })
    if catalog_rows:
        catalog_json = json.dumps(catalog_rows, ensure_ascii=False).encode("utf-8")
        catalog_path = os.path.join(dist_data, "catalog.enc")
        _encrypt_and_write(catalog_json, master_key, "catalog", catalog_path)
        print(f"  catalog.enc ({os.path.getsize(catalog_path)} bytes) - {len(catalog_rows)} courses")

    # 6) Search index
    search_data = _build_search_index(conn)
    search_json = json.dumps(search_data, ensure_ascii=False).encode("utf-8")
    search_path = os.path.join(dist_data, "search-index.enc")
    _encrypt_and_write(search_json, master_key, "search-index", search_path)
    print(f"  search-index.enc ({os.path.getsize(search_path)} bytes)")

    # 7) Copy frontend static files
    frontend_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
    )
    for item in os.listdir(frontend_dir):
        src = os.path.join(frontend_dir, item)
        dst = os.path.join(output_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    print(f"  Copied frontend/ static files to dist/")

    conn.close()
    print(f"\nBuild complete: {output_dir}/")


def main():
    parser = argparse.ArgumentParser(description="Build frontend dist with encrypted shards")
    parser.add_argument("--data-dir", required=True, help="Path to cloned data branch")
    parser.add_argument("--output", required=True, help="Output directory (dist/)")
    args = parser.parse_args()

    if os.path.exists(args.output):
        shutil.rmtree(args.output)

    build(args.data_dir, args.output)


if __name__ == "__main__":
    main()
