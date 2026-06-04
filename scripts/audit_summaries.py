#!/usr/bin/env python3
"""Audit rendered lecture summaries for Markdown/LaTeX display risks.

This is a lightweight review layer inspired by the review-report loop in
Luolingli/ocr-md-to-pdf.  It does not compile PDFs and does not need OCR JSON;
instead it scans stored lecture summaries for issues that commonly make the
frontend or exports render badly: broken math delimiters, KaTeX-hostile OCR
math, raw HTML styling, and PPT/OCR presentation markup leaking into notes.

The default output is privacy-preserving: it reports counts and identifiers but
not summary text.  Pass --include-snippets when running locally if you need the
exact surrounding text in the JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from typing import Iterable


MATH_DISPLAY_DOLLAR_RE = re.compile(r"\$\$([\s\S]*?)\$\$")
MATH_DISPLAY_BRACKET_RE = re.compile(r"\\\[([\s\S]*?)\\\]")
MATH_INLINE_DOLLAR_RE = re.compile(r"(?<!\$)\$([^\n$]+?)\$(?!\$)")
MATH_INLINE_PAREN_RE = re.compile(r"\\\(([\s\S]*?)\\\)")

HTML_STYLE_RE = re.compile(
    r"<\s*(?:font|style)\b|\b(?:style|class|align|color|size|face|bgcolor)\s*=",
    re.I,
)
HTML_TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9:-]*)\b[^>]*>")
ALLOWED_HTML_TAGS = {
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "strong", "sub",
    "sup", "table", "tbody", "td", "th", "thead", "tr", "ul",
}

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{4,}")
RAW_LATEX_RE = re.compile(
    r"\\(?:int|sum|prod|lim|frac|sqrt|mu|nu|lambda|chi|mathcal|sqcup|ge|le|to|infty|begin|end)\b"
)

ARR_RE = re.compile(r"(\\begin\{array\}\{)([^{}]*)(\})(.*?)(\\end\{array\})", re.S)


@dataclass
class Issue:
    severity: str
    kind: str
    detail: str
    position: int | None = None
    snippet: str | None = None


def _snippet(text: str, pos: int | None, radius: int = 90) -> str | None:
    if pos is None:
        return None
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _balance_braces_delta(text: str) -> int:
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth


def _array_column_issue(math: str) -> bool:
    for match in ARR_RE.finditer(math):
        body = match.group(4)
        max_cols = 1
        for row in re.split(r"\\\\", body):
            if row.strip():
                max_cols = max(max_cols, len(row.split("&")))
        declared = len(re.findall(r"[lcr]", match.group(2)))
        if declared < max_cols:
            return True
    return False


def _single_dollar_count(text: str) -> int:
    return sum(1 for _ in re.finditer(r"(^|[^$])\$(?!$)", text))


def _math_spans(text: str) -> Iterable[tuple[str, str, int]]:
    for regex, kind in (
        (MATH_DISPLAY_DOLLAR_RE, "display_dollar"),
        (MATH_DISPLAY_BRACKET_RE, "display_bracket"),
        (MATH_INLINE_DOLLAR_RE, "inline_dollar"),
        (MATH_INLINE_PAREN_RE, "inline_paren"),
    ):
        for match in regex.finditer(text):
            yield kind, match.group(1), match.start()


def _mask_math(text: str) -> str:
    masked = text
    for regex in (
        MATH_DISPLAY_DOLLAR_RE,
        MATH_DISPLAY_BRACKET_RE,
        MATH_INLINE_DOLLAR_RE,
        MATH_INLINE_PAREN_RE,
    ):
        masked = regex.sub(" ", masked)
    return masked


def audit_text(text: str, include_snippets: bool = False) -> list[Issue]:
    issues: list[Issue] = []

    def add(severity: str, kind: str, detail: str, pos: int | None = None) -> None:
        issues.append(Issue(
            severity=severity,
            kind=kind,
            detail=detail,
            position=pos,
            snippet=_snippet(text, pos) if include_snippets else None,
        ))

    if HTML_STYLE_RE.search(text):
        add("high", "html_style", "raw HTML styling or presentation attributes")

    for match in HTML_TAG_RE.finditer(text):
        tag = match.group(1).lower()
        if tag not in ALLOWED_HTML_TAGS:
            add("medium", "unexpected_html", f"unexpected raw HTML tag <{tag}>", match.start())
            break

    if text.count("$$") % 2:
        add("high", "unbalanced_display_dollar", "odd number of $$ delimiters")
    if _single_dollar_count(text) % 2:
        add("high", "unbalanced_inline_dollar", "odd number of single $ delimiters")
    if text.count("\\[") != text.count("\\]"):
        add("high", "unbalanced_display_bracket", "mismatched \\[ / \\] delimiters")
    if text.count("\\(") != text.count("\\)"):
        add("high", "unbalanced_inline_paren", "mismatched \\( / \\) delimiters")

    for kind, math, pos in _math_spans(text):
        compact = math.strip()
        if not compact:
            continue
        brace_delta = _balance_braces_delta(compact)
        if brace_delta:
            add("high", "math_brace_imbalance", f"brace balance delta {brace_delta}", pos)
        if re.search(r"_[A-Za-z0-9]_[A-Za-z0-9]", compact):
            add("medium", "double_subscript", "likely OCR double subscript", pos)
        if re.search(r"\^[A-Za-z0-9]\^[A-Za-z0-9]", compact):
            add("medium", "double_superscript", "likely OCR double superscript", pos)
        if re.search(r"\\left(?![A-Za-z\s(\[\]{}|./<>)\\])", compact) or \
           re.search(r"\\right(?![A-Za-z\s(\[\]{}|./<>)\\])", compact):
            add("medium", "left_right_delim", "bare or malformed \\left/\\right delimiter", pos)
        if _array_column_issue(compact):
            add("medium", "array_columns", "array colspec has fewer columns than rows", pos)
        cjk_runs = CJK_RUN_RE.findall(compact)
        if cjk_runs:
            add("medium", "cjk_in_math", "long CJK text inside math span", pos)
        if kind.startswith("inline") and len(compact) > 160:
            add("medium", "long_inline_math", "inline math span is unusually long", pos)
        if "\n" in compact and kind.startswith("inline"):
            add("medium", "multiline_inline_math", "inline math contains newline", pos)

    outside_math = _mask_math(text)
    raw_match = RAW_LATEX_RE.search(outside_math)
    if raw_match:
        add("medium", "raw_latex_outside_math", "LaTeX command appears outside math delimiters", raw_match.start())

    if re.search(r"(^|\n)#{1,2}\s+", text):
        add("low", "oversized_heading", "summary contains # or ## heading")

    return issues


def _load_rows(db_path: str, course_ids: set[str] | None, sub_ids: set[str] | None,
               limit: int | None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = ["l.summary IS NOT NULL", "TRIM(l.summary) != ''"]
    params: list[str] = []
    if course_ids:
        where.append("l.course_id IN (%s)" % ",".join("?" for _ in course_ids))
        params.extend(sorted(course_ids))
    if sub_ids:
        where.append("l.sub_id IN (%s)" % ",".join("?" for _ in sub_ids))
        params.extend(sorted(sub_ids))
    sql = f"""
        SELECT l.sub_id, l.course_id, l.sub_title, l.date, l.summary,
               l.summary_model, l.summary_format_version, c.title AS course_title
          FROM lectures l
          LEFT JOIN courses c ON c.course_id = l.course_id
         WHERE {' AND '.join(where)}
         ORDER BY l.processed_at DESC, l.date DESC, l.sub_id
    """
    if limit:
        sql += " LIMIT ?"
        params.append(str(limit))
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def _csv_set(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit lecture summaries for display risks.")
    parser.add_argument("--db", default="data/icourse.db", help="SQLite DB path")
    parser.add_argument("--course-ids", help="comma-separated course IDs to scan")
    parser.add_argument("--sub-ids", help="comma-separated lecture sub_ids to scan")
    parser.add_argument("--limit", type=int, help="scan at most N summaries")
    parser.add_argument("--json", dest="json_path", help="write machine-readable report")
    parser.add_argument("--include-snippets", action="store_true",
                        help="include summary snippets in JSON; avoid in CI logs")
    parser.add_argument("--fail-on", choices=["none", "high", "medium", "low"],
                        default="none", help="exit non-zero at or above severity")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"Database not found: {args.db}")

    rows = _load_rows(args.db, _csv_set(args.course_ids), _csv_set(args.sub_ids), args.limit)
    report_rows = []
    severity_rank = {"low": 1, "medium": 2, "high": 3}
    fail_rank = 0 if args.fail_on == "none" else severity_rank[args.fail_on]
    issue_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    affected_courses: defaultdict[str, int] = defaultdict(int)
    fail = False

    for row in rows:
        issues = audit_text(row["summary"] or "", include_snippets=args.include_snippets)
        if not issues:
            continue
        for issue in issues:
            issue_counts[issue.kind] += 1
            severity_counts[issue.severity] += 1
            if fail_rank and severity_rank[issue.severity] >= fail_rank:
                fail = True
        affected_courses[row["course_id"]] += 1
        report_rows.append({
            "sub_id": row["sub_id"],
            "course_id": row["course_id"],
            "course_title": row["course_title"],
            "sub_title": row["sub_title"],
            "date": row["date"],
            "summary_model": row["summary_model"],
            "summary_format_version": row["summary_format_version"],
            "issues": [asdict(issue) for issue in issues],
        })

    summary = {
        "db": args.db,
        "summaries_scanned": len(rows),
        "summaries_with_issues": len(report_rows),
        "issue_counts": dict(issue_counts),
        "severity_counts": dict(severity_counts),
        "affected_courses": dict(sorted(affected_courses.items())),
    }
    report = {"summary": summary, "lectures": report_rows}

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    print("[SummaryAudit] scanned={summaries_scanned} with_issues={summaries_with_issues} "
          "high={high} medium={medium} low={low}".format(
              high=severity_counts.get("high", 0),
              medium=severity_counts.get("medium", 0),
              low=severity_counts.get("low", 0),
              **summary,
          ))
    if issue_counts:
        print("[SummaryAudit] issue_counts=" + json.dumps(dict(issue_counts), ensure_ascii=False,
                                                        sort_keys=True))
    if args.json_path:
        print(f"[SummaryAudit] report={args.json_path}")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n### Summary display audit\n\n")
            f.write(f"- Summaries scanned: {len(rows)}\n")
            f.write(f"- Summaries with issues: {len(report_rows)}\n")
            f.write(f"- Severity counts: {dict(severity_counts)}\n")
            if issue_counts:
                f.write(f"- Issue counts: `{json.dumps(dict(issue_counts), ensure_ascii=False, sort_keys=True)}`\n")

    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
