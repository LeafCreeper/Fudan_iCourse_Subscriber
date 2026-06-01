#!/usr/bin/env python3
"""Dump every course + lecture record in the DB to a single Markdown document.

Produces a human-readable catalog of what the database knows: subscribed
courses, their lectures, processing/email state, and PPT-OCR coverage.  The
``course_id`` and lecture ``sub_id`` are always included — they're the
primary keys you need to drive the other workflows (export, reset, single
run, etc.).

Usage:
    python scripts/dump_course_catalog.py
    python scripts/dump_course_catalog.py --db data/icourse.db --out catalog.md
    python scripts/dump_course_catalog.py --course-id 30004,30005

Options:
    --db          Database path (default: data/icourse.db).
    --out         Output Markdown path (default: course_catalog.md).
    --course-id   Optional comma-separated course IDs to restrict the dump.
                  When omitted, every course in the DB is included.

The document has three sections:
  1. Meta — key/value rows from the ``meta`` table plus generation time.
  2. Subscribed courses — one block per course, with a lecture table that
     lists sub_id, title, date, processing state, summary model and PPT
     page status counts.
  3. Catalog (all_courses) — the full term catalog the subscription editor
     uses, grouped by term, if present.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from collections import defaultdict


def _md_escape(s: object) -> str:
    """Escape pipe characters so values don't break Markdown tables."""
    text = "" if s is None else str(s)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _ppt_status_counts(db: sqlite3.Connection, sub_id: str) -> dict[str, int]:
    rows = db.execute(
        "SELECT ocr_status, COUNT(*) AS n FROM ppt_pages "
        "WHERE sub_id = ? GROUP BY ocr_status",
        (sub_id,),
    ).fetchall()
    return {r["ocr_status"] or "?": r["n"] for r in rows}


def _fmt_status_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def dump_courses(db: sqlite3.Connection, out, course_ids: list[str] | None):
    if course_ids:
        placeholders = ",".join("?" for _ in course_ids)
        courses = db.execute(
            f"SELECT course_id, title, teacher FROM courses "
            f"WHERE course_id IN ({placeholders}) ORDER BY course_id",
            course_ids,
        ).fetchall()
    else:
        courses = db.execute(
            "SELECT course_id, title, teacher FROM courses ORDER BY course_id"
        ).fetchall()

    total_lectures = 0
    out.write("## 已订阅课程 (courses)\n\n")
    if not courses:
        out.write("_(数据库中没有匹配的课程)_\n\n")
        return 0

    for c in courses:
        course_id = c["course_id"]
        lectures = db.execute(
            "SELECT sub_id, sub_title, date, processed_at, emailed_at, "
            "summary_model, summary_format_version, "
            "LENGTH(transcript) AS transcript_len, "
            "LENGTH(summary) AS summary_len, "
            "error_stage, error_count "
            "FROM lectures WHERE course_id = ? "
            "ORDER BY date, sub_id",
            (course_id,),
        ).fetchall()
        total_lectures += len(lectures)

        out.write(f"### {_md_escape(c['title']) or '(无标题)'}\n\n")
        out.write(f"- **course_id**: `{course_id}`\n")
        out.write(f"- **teacher**: {_md_escape(c['teacher']) or '—'}\n")
        out.write(f"- **lecture 数量**: {len(lectures)}\n\n")

        if not lectures:
            out.write("_(该课程暂无 lecture 记录)_\n\n")
            continue

        out.write(
            "| sub_id (lecture id) | 标题 | 日期 | 已处理 | 已发邮件 | "
            "模型 (版本) | 转写字数 | 摘要字数 | PPT 页状态 | 错误 |\n"
        )
        out.write(
            "| --- | --- | --- | :---: | :---: | --- | ---: | ---: "
            "| --- | --- |\n"
        )
        for lec in lectures:
            sub_id = lec["sub_id"]
            counts = _ppt_status_counts(db, str(sub_id))
            processed = "✓" if lec["processed_at"] else "✗"
            emailed = "✓" if lec["emailed_at"] else "✗"
            model = lec["summary_model"] or "—"
            version = lec["summary_format_version"]
            model_cell = f"{_md_escape(model)} (v{version})"
            err = "—"
            if lec["error_stage"]:
                err = f"{_md_escape(lec['error_stage'])} ×{lec['error_count']}"
            out.write(
                f"| `{sub_id}` "
                f"| {_md_escape(lec['sub_title']) or '—'} "
                f"| {_md_escape(lec['date']) or '—'} "
                f"| {processed} "
                f"| {emailed} "
                f"| {model_cell} "
                f"| {lec['transcript_len'] or 0} "
                f"| {lec['summary_len'] or 0} "
                f"| {_fmt_status_counts(counts)} "
                f"| {err} |\n"
            )
        out.write("\n")

    return total_lectures


def dump_meta(db: sqlite3.Connection, out):
    out.write("## 元信息 (meta)\n\n")
    out.write(f"- **生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")

    counts = {}
    for tbl in ("courses", "lectures", "ppt_pages", "all_courses"):
        if _table_exists(db, tbl):
            n = db.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
            counts[tbl] = n
    out.write(
        "- **行数**: "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
        + "\n"
    )

    if _table_exists(db, "meta"):
        rows = db.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        if rows:
            out.write("\n| key | value |\n| --- | --- |\n")
            for r in rows:
                out.write(
                    f"| {_md_escape(r['key'])} | {_md_escape(r['value'])} |\n"
                )
    out.write("\n")


def dump_catalog(db: sqlite3.Connection, out, course_ids: list[str] | None):
    if not _table_exists(db, "all_courses"):
        return
    rows = db.execute(
        "SELECT course_id, term, title, teacher, dept, last_seen_at "
        "FROM all_courses ORDER BY term, course_id"
    ).fetchall()
    if course_ids:
        wanted = set(course_ids)
        rows = [r for r in rows if r["course_id"] in wanted]
    if not rows:
        return

    out.write("## 全校课程目录 (all_courses)\n\n")
    by_term: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_term[r["term"] or "(未知学期)"].append(r)

    for term in sorted(by_term):
        out.write(f"### 学期 {term}\n\n")
        out.write(
            "| course_id | 课程标题 | 教师 | 院系 | 最近出现 |\n"
            "| --- | --- | --- | --- | --- |\n"
        )
        for r in by_term[term]:
            out.write(
                f"| `{r['course_id']}` "
                f"| {_md_escape(r['title']) or '—'} "
                f"| {_md_escape(r['teacher']) or '—'} "
                f"| {_md_escape(r['dept']) or '—'} "
                f"| {_md_escape(r['last_seen_at']) or '—'} |\n"
            )
        out.write("\n")


def main():
    parser = argparse.ArgumentParser(
        description="Dump all course/lecture info from the DB to Markdown."
    )
    parser.add_argument(
        "--db", default="data/icourse.db",
        help="Database path (default: data/icourse.db)",
    )
    parser.add_argument(
        "--out", default="course_catalog.md",
        help="Output Markdown path (default: course_catalog.md)",
    )
    parser.add_argument(
        "--course-id", default="",
        help="Optional comma-separated course IDs to restrict the dump.",
    )
    args = parser.parse_args()

    course_ids = [c.strip() for c in args.course_id.split(",") if c.strip()]
    course_ids = course_ids or None

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row

    with open(args.out, "w", encoding="utf-8") as out:
        out.write("# 课程信息导出\n\n")
        dump_meta(db, out)
        total = dump_courses(db, out, course_ids)
        dump_catalog(db, out, course_ids)

    print(f"Wrote {args.out} (total {total} lecture(s)).")


if __name__ == "__main__":
    main()
