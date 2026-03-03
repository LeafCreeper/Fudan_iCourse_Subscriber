"""Export all summaries for a course as an email or PDF attachment.

Usage:
    python scripts/export_course.py --course-id 30004
    python scripts/export_course.py --course-id 30004 --pdf

Options:
    --course-id   Course ID to export (required).
    --pdf         Convert summaries to PDF and send as attachment.
                  Without this flag the summaries are sent as an HTML email.
    --db          Database path (default: data/icourse.db).
"""

import argparse
import os
import smtplib
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

# Allow importing from the project root when run as `python scripts/export_course.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config  # noqa: E402
from src.database import Database  # noqa: E402
from src.emailer import _EMAIL_CSS, _md_to_html  # noqa: E402


def _build_html(course_title: str, teacher: str, lectures: list[dict]) -> str:
    """Build a complete styled HTML document from course summaries."""
    body_parts = [
        f"<h1>{escape(course_title)}</h1>",
        f"<p>任课教师：{escape(teacher)}</p>",
        "<hr>",
    ]
    for lec in lectures:
        body_parts.append(
            f"<h2>{escape(lec['sub_title'])} "
            f"<small>({escape(lec['date'])})</small></h2>"
        )
        body_parts.append(_md_to_html(lec["summary"]))
        body_parts.append("<hr>")

    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset='utf-8'>"
        f"<style>{_EMAIL_CSS}</style>"
        "</head><body>"
        + "\n".join(body_parts)
        + "</body></html>"
    )


def _build_plain(course_title: str, teacher: str, lectures: list[dict]) -> str:
    """Build a plain-text version of the summaries."""
    parts = [
        f"课程：{course_title}",
        f"任课教师：{teacher}",
        "=" * 40,
    ]
    for lec in lectures:
        parts.append(f"\n{'─' * 40}")
        parts.append(f"{lec['sub_title']} ({lec['date']})")
        parts.append("─" * 40)
        parts.append(lec["summary"])
    return "\n".join(parts)


def _smtp_connect():
    """Return an authenticated SMTP_SSL connection."""
    server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT)
    server.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
    return server


def _send_html_email(subject: str, html: str, plain: str) -> None:
    """Send a multipart HTML email."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("iCourse Subscriber", config.SMTP_EMAIL))
    msg["To"] = config.RECEIVER_EMAIL
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with _smtp_connect() as server:
        server.sendmail(config.SMTP_EMAIL, config.RECEIVER_EMAIL, msg.as_string())


def _send_pdf_email(subject: str, pdf_bytes: bytes, filename: str) -> None:
    """Send an email with a PDF file attached."""
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr(("iCourse Subscriber", config.SMTP_EMAIL))
    msg["To"] = config.RECEIVER_EMAIL

    part = MIMEBase("application", "pdf", name=filename)
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)

    with _smtp_connect() as server:
        server.sendmail(config.SMTP_EMAIL, config.RECEIVER_EMAIL, msg.as_string())


def main():
    parser = argparse.ArgumentParser(description="Export course summaries.")
    parser.add_argument("--course-id", required=True, help="Course ID to export")
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Export as PDF attachment instead of inline HTML email",
    )
    parser.add_argument(
        "--db", default="data/icourse.db", help="Database path (default: data/icourse.db)"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    db = Database(args.db)

    course = db.conn.execute(
        "SELECT * FROM courses WHERE course_id = ?", (args.course_id,)
    ).fetchone()
    if not course:
        print(f"Course {args.course_id} not found in database.")
        sys.exit(1)

    course_title = course["title"]
    teacher = course["teacher"]

    rows = db.conn.execute(
        """SELECT sub_id, sub_title, date, summary
           FROM lectures
           WHERE course_id = ? AND summary IS NOT NULL
           ORDER BY date ASC, sub_id ASC""",
        (args.course_id,),
    ).fetchall()
    lectures = [dict(row) for row in rows]

    if not lectures:
        print(f"No summaries found for course {args.course_id} ({course_title}).")
        sys.exit(0)

    print(f"Found {len(lectures)} summarized lecture(s) for {course_title}.")

    if not config.SMTP_EMAIL or not config.SMTP_PASSWORD or not config.RECEIVER_EMAIL:
        print("Email configuration incomplete. Set SMTP_EMAIL, SMTP_PASSWORD, RECEIVER_EMAIL.")
        sys.exit(1)

    html = _build_html(course_title, teacher, lectures)
    subject = f"[iCourse 课程摘要导出] {course_title}"

    if args.pdf:
        try:
            import weasyprint  # noqa: PLC0415
        except ImportError:
            print("weasyprint is required for PDF export. Install it with: pip install weasyprint")
            sys.exit(1)

        print("Generating PDF...")
        pdf_bytes = weasyprint.HTML(string=html).write_pdf()
        safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in course_title)
        filename = f"{safe_title}_summaries.pdf"
        print(f"Sending PDF email ({len(pdf_bytes)} bytes)...")
        _send_pdf_email(subject, pdf_bytes, filename)
    else:
        plain = _build_plain(course_title, teacher, lectures)
        print("Sending HTML email...")
        _send_html_email(subject, html, plain)

    print(f"[OK] Sent: {subject}")


if __name__ == "__main__":
    main()
