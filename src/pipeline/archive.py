"""Mirror a run's summaries and slide decks into a browsable ``archive/`` tree.

The CI runner is ephemeral and ``Emailer`` only ever sees the lectures that are
new *in this run*, so nothing survives the run unless it is written somewhere
durable.  This module materialises the two artefacts users actually want to
keep around:

* ``archive/md/<课程>_全讲次.md`` — every summarised lecture of the course in
  lecture order, rebuilt from the database on each run.  Rebuilding (rather
  than appending to the previous file) is what makes re-summarised lectures,
  re-sent batches and late-arriving replays idempotent: a lecture is keyed by
  ``sub_id``, so its newest summary simply replaces the old one instead of
  showing up twice.
* ``archive/ppt/<课程>_<日期>_<sub_id>_slides.pptx`` — one deck per lecture,
  copied out of ``config.PPT_EXPORT_DIR`` *before* ``main._cleanup_ppt_exports``
  deletes them.  Decks are archived even when the email's attachment budget
  dropped them, so this folder is the complete deck set for the run.

Everything is plain Markdown / plain .pptx on purpose: the workflow commits this
tree to the ``archive`` branch, which is meant to be read directly after a
``git pull``.  No network access, no LLM calls — a rebuild is one SQLite query
per course plus a file copy.
"""

from __future__ import annotations

import os
import shutil

from src.runtime import config

ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "archive")
MD_DIR = os.path.join(ARCHIVE_DIR, "md")
PPT_DIR = os.path.join(ARCHIVE_DIR, "ppt")

# GitHub rejects blobs over 100 MB; decks are capped by PPT_EXPORT_MAX_MB (15
# by default), so this only guards against a mis-set budget.
MAX_DECK_BYTES = 90 * 1024 * 1024


def _safe_filename(title: str) -> str:
    """Sanitise a course title for use inside a file name."""
    return "".join(
        c if c.isalnum() or c in " _-" else "_" for c in (title or "")
    )[:80]


def _build_course_markdown(course_title: str, teacher: str,
                           lectures: list[dict]) -> str:
    """Render one course's lectures into a single Markdown document.

    Layout mirrors ``scripts/export_course.py::_build_plain`` so an archived
    file reads the same as a manual "Export Course Summaries" run.
    """
    lines = [f"# 课程：{course_title}"]
    if teacher:
        lines.append(f"任课教师：{teacher}")
    lines.append("")
    for lec in lectures:
        title = lec.get("sub_title") or lec["sub_id"]
        lines.append(f"## {title} ({lec.get('date') or ''})")
        lines.append("")
        lines.append((lec.get("summary") or "").strip())
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _reset_markdown_dir() -> None:
    """Drop yesterday's course files before rebuilding them.

    Deleting first (instead of overwriting in place) also removes a course that
    no longer has any summary, so the folder never keeps stale documents.
    """
    os.makedirs(MD_DIR, exist_ok=True)
    for name in os.listdir(MD_DIR):
        if name.endswith(".md"):
            os.remove(os.path.join(MD_DIR, name))


def _export_course_markdown(db) -> list[dict]:
    """Write one merged Markdown file per course; return per-course stats."""
    courses = db.conn.execute(
        """SELECT c.course_id, c.title, c.teacher,
                  (SELECT COUNT(*) FROM lectures l
                    WHERE l.course_id = c.course_id
                      AND l.summary IS NOT NULL) AS lecture_count
             FROM courses c
            ORDER BY c.title"""
    ).fetchall()

    exported: list[dict] = []
    for course in courses:
        if not course["lecture_count"]:
            continue
        rows = db.conn.execute(
            """SELECT sub_id, sub_title, date, summary FROM lectures
                WHERE course_id = ? AND summary IS NOT NULL
                ORDER BY CAST(sub_id AS INTEGER) ASC""",
            (course["course_id"],),
        ).fetchall()
        title = course["title"] or course["course_id"]
        filename = f"{_safe_filename(title)}_全讲次.md"
        body = _build_course_markdown(
            title, course["teacher"] or "", [dict(r) for r in rows],
        )
        with open(os.path.join(MD_DIR, filename), "w", encoding="utf-8") as fh:
            fh.write(body)
        exported.append({
            "title": title,
            "filename": filename,
            "lecture_count": len(rows),
            "deck_count": 0,
        })
    return exported


def _deck_filename(course_title: str, date: str, sub_id: str) -> str:
    parts = [_safe_filename(course_title)]
    if date:
        parts.append(_safe_filename(date))
    parts.append(str(sub_id))
    return "_".join(parts) + "_slides.pptx"


def _copy_decks(db) -> tuple[dict[str, int], int]:
    """Copy this run's decks into ``archive/ppt``; return (counts, skipped).

    ``ppt_exporter`` names decks ``<sub_id>.pptx``, so the course title and
    lecture date are resolved from the database to make the archived file
    self-describing.  Including ``sub_id`` keeps the name unique when a course
    has two lectures on the same day.
    """
    src_dir = config.PPT_EXPORT_DIR
    if not os.path.isdir(src_dir):
        return {}, 0

    os.makedirs(PPT_DIR, exist_ok=True)
    counts: dict[str, int] = {}
    skipped = 0
    for name in sorted(os.listdir(src_dir)):
        if not name.lower().endswith(".pptx"):
            continue
        src_path = os.path.join(src_dir, name)
        sub_id = os.path.splitext(name)[0]
        row = db.conn.execute(
            """SELECT l.date, c.title
                 FROM lectures l
                 LEFT JOIN courses c ON c.course_id = l.course_id
                WHERE l.sub_id = ?""",
            (sub_id,),
        ).fetchone()
        if not row:
            print(f"[Archive] Deck {name} has no lecture row, skipping")
            skipped += 1
            continue
        if os.path.getsize(src_path) > MAX_DECK_BYTES:
            print(f"[Archive] Deck {name} exceeds {MAX_DECK_BYTES // 1048576} MB,"
                  " skipping")
            skipped += 1
            continue
        course_title = row["title"] or ""
        dest_name = _deck_filename(course_title, row["date"] or "", sub_id)
        shutil.copyfile(src_path, os.path.join(PPT_DIR, dest_name))
        counts[course_title] = counts.get(course_title, 0) + 1
    return counts, skipped


def _write_index(courses: list[dict]) -> None:
    """Write a small table of contents so the branch is browsable."""
    lines = [
        "# 课程归档",
        "",
        "每门课一份汇总 Markdown（全部讲次，按讲次顺序），所有课件集中放在 "
        "`ppt/`，文件名含课程名与日期。",
        "",
    ]
    for course in courses:
        lines.append(f"## {course['title']}")
        lines.append("")
        lines.append(
            f"- 汇总：[{course['filename']}](md/{course['filename']})"
            f"（{course['lecture_count']} 讲）"
        )
        if course["deck_count"]:
            lines.append(f"- 课件：{course['deck_count']} 份（[ppt/](ppt/)）")
        lines.append("")
    with open(os.path.join(ARCHIVE_DIR, "index.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def archive_run(db) -> dict:
    """Rebuild ``archive/`` from the database plus this run's exported decks.

    Must be called after the summaries are committed and before
    ``main._cleanup_ppt_exports`` removes the generated decks.

    Returns a stats dict for the reporter.
    """
    _reset_markdown_dir()
    courses = _export_course_markdown(db)
    decks_by_course, skipped = _copy_decks(db)
    for course in courses:
        course["deck_count"] = decks_by_course.get(course["title"], 0)
    _write_index(courses)
    return {
        "dir": ARCHIVE_DIR,
        "courses": len(courses),
        "lectures": sum(c["lecture_count"] for c in courses),
        "decks": sum(c["deck_count"] for c in courses),
        "skipped_decks": skipped,
    }
