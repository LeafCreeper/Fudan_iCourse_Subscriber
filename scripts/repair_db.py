#!/usr/bin/env python3
"""Diagnose and repair lecture processing/email state in the iCourse DB.

Designed to run inside GitHub Actions (see
``.github/workflows/repair.yml``) after the encrypted data branch has
been fetched and reassembled to a plaintext SQLite file — no manual
decryption key handling needed; CI uses its stored STUID/UISPSW secrets.

Why this script exists
======================
Two code paths can leave a lecture "processed but never summarised /
never emailed", and the scheduler then skips it forever:

  * Empty transcript or video-only (no audio stream) marks the lecture
    processed without ever producing a summary.
  * A summary exists but emailed_at stays NULL — the next run should
    auto-resend it, but it's worth being able to inspect/force that.

Actions (selected via the REPAIR_ACTION env var)
================================================
  diagnose         Read-only.  Prints the state of every lecture under
                   the target courses/sub_ids and flags stuck rows
                   (processed but missing summary; summary present but
                   not emailed; abandoned after >=3 errors).
  reset_processed  Sets processed_at = emailed_at = NULL and clears the
                   error counters for the target lectures.  The next
                   scheduled run treats them as new: a lecture that
                   already has a summary is short-circuited and that
                   summary goes into the email batch; a lecture without
                   a summary re-enters the full transcribe/summarize
                   pipeline.
  reset_emailed    Sets emailed_at = NULL only.  The next run resends
                   the existing summary without re-processing.

Safety
======
* Targets come from REPAIR_TARGET ("course" | "sub_id") and
  REPAIR_IDS (comma-separated); values are validated to contain only
  digits so the repair can only touch known-shaped identifiers.
* Everything is a dry run unless REPAIR_APPLY=1 is set explicitly.
  In dry-run mode the script opens a transaction, prints the exact
  rows it would change and rolls back.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

_ID_RE = re.compile(r"^[0-9]+$")

# A lecture is abandoned by the scheduler after this many errors — mirror
# of Database.get_unprocessed_lectures(max_errors=3).
MAX_ERRORS = 3


def _parse_ids(raw: str) -> list[str]:
    ids = [part.strip() for part in (raw or "").split(",") if part.strip()]
    bad = [x for x in ids if not _ID_RE.match(x)]
    if bad:
        print(f"::error::Invalid id(s) (digits only): {bad}", file=sys.stderr)
        sys.exit(2)
    if not ids:
        print("::error::REPAIR_IDS is empty", file=sys.stderr)
        sys.exit(2)
    return ids


def _target_where(target: str, ids: list[str]) -> tuple[str, list]:
    if target == "course":
        return "course_id IN (%s)" % ",".join("?" * len(ids)), ids
    if target == "sub_id":
        return "sub_id IN (%s)" % ",".join("?" * len(ids)), ids
    print(f"::error::Unknown REPAIR_TARGET: {target!r}", file=sys.stderr)
    sys.exit(2)


def _print_row(row: sqlite3.Row) -> None:
    summary_len = row["summary_len"]
    transcript_len = row["transcript_len"]
    flags = []
    if row["processed_at"]:
        flags.append("processed")
    if row["emailed_at"]:
        flags.append("emailed")
    if row["error_count"] and row["error_count"] >= MAX_ERRORS:
        flags.append(f"abandoned({row['error_count']}错)")
    print(
        f"  [{','.join(flags) or '未处理'}] "
        f"{row['sub_title'] or row['sub_id']}  sub_id={row['sub_id']}  "
        f"转录={transcript_len if transcript_len is not None else 0}字 "
        f"总结={summary_len if summary_len is not None else 0}字"
    )
    if row["error_stage"] or row["error_msg"]:
        print(
            f"      错误: 阶段={row['error_stage']} "
            f"次数={row['error_count']} "
            f"信息={(row['error_msg'] or '')[:100]}"
        )


def _fetch(db: sqlite3.Connection, where: str, params: list) -> list[sqlite3.Row]:
    return db.execute(
        f"""
        SELECT l.sub_id, l.course_id, l.sub_title, l.date,
               l.processed_at, l.emailed_at,
               length(l.transcript) AS transcript_len,
               length(l.summary)    AS summary_len,
               l.error_count, l.error_stage, l.error_msg, l.summary
        FROM lectures l
        WHERE {where}
        ORDER BY l.course_id, l.sub_title
        """,
        params,
    ).fetchall()


def cmd_diagnose(db: sqlite3.Connection, where: str, params: list) -> None:
    rows = _fetch(db, where, params)
    if not rows:
        print("没有匹配的讲座记录。")
        return

    print(f"共 {len(rows)} 条讲座记录：")
    current_course = None
    stuck_no_summary = []
    pending_resend = []
    for row in rows:
        if row["course_id"] != current_course:
            current_course = row["course_id"]
            title = db.execute(
                "SELECT title FROM courses WHERE course_id = ?",
                (current_course,),
            ).fetchone()
            print(f"\n课程 {current_course} "
                  f"({title[0] if title else '未知课名'})")
        _print_row(row)
        if row["processed_at"] and not row["summary"]:
            stuck_no_summary.append(row)
        elif row["processed_at"] and row["summary"] and not row["emailed_at"]:
            pending_resend.append(row)

    print("\n" + "=" * 60)
    if stuck_no_summary:
        print(f"[卡死] 已处理但从未生成总结 ({len(stuck_no_summary)} 节):")
        for r in stuck_no_summary:
            print(f"  - sub_id={r['sub_id']} {r['sub_title']}")
        print("  → 对这些讲座执行 reset_processed，下次 check 会重新处理并发邮件")
    else:
        print("[OK] 没有「已处理但无总结」的卡死讲座")
    if pending_resend:
        print(f"[待补发] 已有总结但未发信 ({len(pending_resend)} 节):")
        for r in pending_resend:
            print(f"  - sub_id={r['sub_id']} {r['sub_title']}")
        print("  → 下次 check 本应自动补发；若仍收不到，执行 reset_emailed "
              "并检查 SMTP 授权码")
    else:
        print("[OK] 没有「有总结但未发信」的讲座")


def _reset(db: sqlite3.Connection, where: str, params: list,
           columns: list[str], label: str, apply: bool) -> None:
    rows = _fetch(db, where, params)
    if not rows:
        print("没有匹配的讲座记录，未做任何修改。")
        return

    print(f"将对以下 {len(rows)} 节讲座执行 {label}:")
    for row in rows:
        print(f"  sub_id={row['sub_id']} {row['sub_title']}")

    set_clause = ", ".join(f"{col} = NULL" for col in columns)
    # Resetting processed state also clears the error counters so an
    # abandoned lecture is retried instead of staying over the limit.
    params_out = list(params)
    if "processed_at" in columns:
        set_clause += ", error_count = 0, error_stage = NULL, error_msg = NULL"

    sql = f"UPDATE lectures SET {set_clause} WHERE {where}"
    if not apply:
        db.execute(sql, params_out)
        print("\n[DRY RUN] 以上为将要修改的内容，未实际写入。"
              "在工作流表单勾选 Apply 后才会真正改库并推送。")
        db.rollback()
        return

    cur = db.execute(sql, params_out)
    db.commit()
    print(f"\n[APPLIED] 已更新 {cur.rowcount} 行。"
          "下次 iCourse Check 将重新处理/补发邮件。")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: repair_db.py <db_path>", file=sys.stderr)
        sys.exit(2)

    action = os.environ.get("REPAIR_ACTION", "diagnose").strip()
    target = os.environ.get("REPAIR_TARGET", "course").strip()
    ids = _parse_ids(os.environ.get("REPAIR_IDS", ""))
    apply = os.environ.get("REPAIR_APPLY", "").strip() == "1"

    where, params = _target_where(target, ids)

    # isolation_level=None → we own the transaction boundaries explicitly
    # (dry-run rolls back, apply commits); no implicit-transaction surprises.
    db = sqlite3.connect(sys.argv[1], isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        if action == "diagnose":
            cmd_diagnose(db, where, params)
            db.rollback()  # read-only
        elif action == "reset_processed":
            _reset(db, where, params,
                   ["processed_at", "emailed_at"],
                   "reset_processed（清空处理/发信标志，重跑并发信）",
                   apply)
        elif action == "reset_emailed":
            _reset(db, where, params, ["emailed_at"],
                   "reset_emailed（仅清空发信标志，用已有总结重发）",
                   apply)
        else:
            print(f"::error::Unknown REPAIR_ACTION: {action!r}",
                  file=sys.stderr)
            sys.exit(2)
    finally:
        db.close()


if __name__ == "__main__":
    main()
