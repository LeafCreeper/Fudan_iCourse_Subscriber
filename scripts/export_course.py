"""Export all summaries for one or more courses as emails or PDF attachments.

Usage:
    python scripts/export_course.py --course-id 30004
    python scripts/export_course.py --course-id 30004,30005
    python scripts/export_course.py --course-id 30004,30005 --pdf
    python scripts/export_course.py --course-id 30004 --sub-ids 1,2,5

Options:
    --course-id   Comma-separated course IDs to export (required).
    --sub-ids     Optional comma-separated lecture sub_ids.  When set, only
                  lectures with matching sub_id are exported.  Used by the
                  frontend "导出" dialog to honour per-lecture selection.
    --pdf         Convert summaries to PDF and send as attachment.
                  Without this flag the summaries are sent as HTML emails.
    --db          Database path (default: data/icourse.db).

When multiple course IDs are given:
  - PDF mode:  each course becomes a separate PDF; all PDFs are sent in
               a single email as attachments.
  - Email mode: each course is sent as a separate HTML email.
"""

import argparse
import os
import smtplib
import sys
import tempfile
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

# Allow importing from the project root when run as `python scripts/export_course.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runtime import config
from src.data.database import Database  # noqa: E402
from src.data.crypto_box import derive_new_password  # noqa: E402
from src.data.sharder import (  # noqa: E402
    INDEX_FILENAME,
    SHARDS_DIR,
    load_index,
    reassemble_database,
)
from src.api.emailer import _EMAIL_CSS, _PYGMENTS_CSS, _md_to_html  # noqa: E402

# Override hardcoded pixel dimensions for PDF rendering.
# WeasyPrint maps CSS px to physical size at 96 DPI, which makes the
# pre-scaled latex images appear too small. This CSS lets the renderer
# size them naturally based on the image's intrinsic dimensions instead.
_PDF_LATEX_CSS = (
    "img { max-width: 100% !important; height: auto !important; }\n"
    'body { font-family: "Microsoft YaHei", sans-serif; }\n'
)


def _build_html(course_title: str, teacher: str, lectures: list[dict],
                pdf: bool = False, cid_images: dict | None = None) -> str:
    """Build a complete styled HTML document from course summaries.

    Args:
        cid_images: When provided (dict), LaTeX images are downloaded and
                    embedded via CID references.  The dict is populated with
                    ``{cid_name: png_bytes}`` entries for the caller to attach
                    to the MIME message.
    """
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
        body_parts.append(_md_to_html(lec["summary"], cid_images=cid_images))
        body_parts.append("<hr>")

    extra_css = f"\n{_PDF_LATEX_CSS}" if pdf else ""
    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset='utf-8'>"
        f"<style>{_EMAIL_CSS}\n{_PYGMENTS_CSS}{extra_css}</style>"
        "</head><body>"
        + "\n".join(body_parts)
        + "</body></html>"
    )


def _build_plain(course_title: str, teacher: str, lectures: list[dict]) -> str:
    """Build a plain-text version of the summaries."""
    parts = [
        f"# 课程：{course_title}",
        f"任课教师：{teacher}",
        # "=" * 40,
    ]
    for lec in lectures:
        # parts.append(f"\n{'─' * 40}")
        parts.append(f"## {lec['sub_title']} ({lec['date']})")
        # parts.append("─" * 40)
        parts.append(lec["summary"])
    return "\n".join(parts)


def _smtp_connect():
    """Return an authenticated SMTP_SSL connection."""
    server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT)
    server.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
    return server


def _send_html_email(subject: str, html: str, plain: str,
                     cid_images: dict[str, bytes] | None = None) -> None:
    """Send a multipart HTML email with CID-embedded LaTeX images.

    Uses ``multipart/related`` wrapping ``multipart/alternative`` so that
    CID image references in the HTML resolve correctly, matching the MIME
    structure used in the main programme (``src/emailer.py``).
    """
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = formataddr(("iCourse Subscriber", config.SMTP_EMAIL))
    msg["To"] = config.RECEIVER_EMAIL

    msg_alt = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(plain, "plain", "utf-8"))
    msg_alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(msg_alt)

    if cid_images:
        for cid, png_data in cid_images.items():
            img_part = MIMEImage(png_data, "png")
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline",
                                filename=f"{cid}.png")
            msg.attach(img_part)

    with _smtp_connect() as server:
        server.sendmail(config.SMTP_EMAIL, config.RECEIVER_EMAIL, msg.as_string())


def _send_pdf_email(subject: str,
                    attachments: list[tuple[bytes, str]]) -> None:
    """Send an email with one or more PDF files attached.

    Args:
        attachments: List of ``(pdf_bytes, filename)`` tuples.
    """
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr(("iCourse Subscriber", config.SMTP_EMAIL))
    msg["To"] = config.RECEIVER_EMAIL

    for pdf_bytes, filename in attachments:
        part = MIMEBase("application", "pdf", name=filename)
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    with _smtp_connect() as server:
        server.sendmail(config.SMTP_EMAIL, config.RECEIVER_EMAIL, msg.as_string())

def _send_md_email(subject: str, md_content: list[tuple[bytes, str]]) -> None:
    """Send an email with Markdown content.

    Args:
        md_content: List of ``(md_bytes, filename)`` tuples.
    """
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr(("iCourse Subscriber", config.SMTP_EMAIL))
    msg["To"] = config.RECEIVER_EMAIL

    for md_bytes, filename in md_content:
        part = MIMEBase("text", "markdown", name=filename)
        part.set_payload(md_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    with _smtp_connect() as server:
        server.sendmail(config.SMTP_EMAIL, config.RECEIVER_EMAIL, msg.as_string())

def _safe_filename(title: str) -> str:
    """Sanitise a course title for use as a filename."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in title)


def _resolve_db_path(db_arg: str) -> tuple[str, str | None]:
    """Resolve ``--db`` to a usable sqlite path.

    Returns:
        (db_path, tmp_path)
        - db_path: SQLite file path to open.
        - tmp_path: Temp file to clean up afterwards, or None.

    Supported inputs:
      1) Plain sqlite path (legacy and current workflow output): data/icourse.db
      2) V2 sharded directory: <dir>/icourse-index.enc + <dir>/shards/*.db.gz.enc
      3) Direct V2 index file path: <dir>/icourse-index.enc
    """
    default_db = "data/icourse.db"

    # Backward-compatible fast path: direct sqlite file.
    if os.path.isfile(db_arg) and os.path.basename(db_arg) != INDEX_FILENAME:
        return db_arg, None

    # Auto-fallback for common V2 local layout when caller keeps default --db.
    if db_arg == default_db and not os.path.isfile(db_arg):
        maybe_sharded = os.path.join("data", "sharded")
        if os.path.isdir(maybe_sharded):
            db_arg = maybe_sharded

    # Accept either a sharded directory or direct index file.
    if os.path.isdir(db_arg):
        base_dir = db_arg
        index_path = os.path.join(base_dir, INDEX_FILENAME)
    elif os.path.isfile(db_arg) and os.path.basename(db_arg) == INDEX_FILENAME:
        index_path = db_arg
        base_dir = os.path.dirname(index_path)
    else:
        raise FileNotFoundError(f"Database not found: {db_arg}")

    shards_dir = os.path.join(base_dir, SHARDS_DIR)
    if not os.path.isfile(index_path):
        raise FileNotFoundError(f"Sharded index not found: {index_path}")
    if not os.path.isdir(shards_dir):
        raise FileNotFoundError(f"Shards directory not found: {shards_dir}")

    stuid = os.environ.get("STUID") or os.environ.get("StuId", "")
    uispsw = os.environ.get("UISPSW") or os.environ.get("UISPsw", "")
    if not stuid or not uispsw:
        raise ValueError(
            "Detected V2 sharded database, but STUID and UISPSW env vars are required "
            "to decrypt and reassemble shards."
        )
    password = derive_new_password(stuid, uispsw)

    index = load_index(index_path, password)
    with tempfile.NamedTemporaryFile(
        suffix=".db",
        prefix="icourse-export-",
        delete=False,
    ) as tmp:
        tmp_db = tmp.name

    reassemble_database(index, shards_dir, tmp_db, password)
    print(f"Reassembled V2 sharded DB to temporary file: {tmp_db}")
    return tmp_db, tmp_db


def _query_course(db: Database, course_id: str,
                  sub_ids: list[str] | None = None) -> tuple[str, str, list[dict]] | None:
    """Return ``(course_title, teacher, lectures)`` for *course_id*.

    Returns ``None`` if the course is missing or has no summaries.

    Args:
        sub_ids: When provided, restrict the result to lectures whose
                 ``sub_id`` is in the list.  String comparison — pass the
                 same form the database stores (the schema treats sub_id
                 as TEXT/INTEGER interchangeably).
    """
    course = db.conn.execute(
        "SELECT * FROM courses WHERE course_id = ?", (course_id,)
    ).fetchone()
    if not course:
        print(f"Course {course_id} not found in database – skipping.")
        return None

    course_title = course["title"]
    teacher = course["teacher"]

    rows = db.conn.execute(
        """SELECT sub_id, sub_title, date, summary
           FROM lectures
           WHERE course_id = ? AND summary IS NOT NULL
           ORDER BY CAST(sub_id AS INTEGER) ASC""",
        (course_id,),
    ).fetchall()
    lectures = [dict(row) for row in rows]

    if sub_ids:
        wanted = {str(s) for s in sub_ids}
        lectures = [lec for lec in lectures if str(lec["sub_id"]) in wanted]

    if not lectures:
        print(f"No summaries found for course {course_id} ({course_title}) – skipping.")
        return None

    print(f"Found {len(lectures)} summarized lecture(s) for {course_title}.")
    return course_title, teacher, lectures


def main():
    parser = argparse.ArgumentParser(description="Export course summaries.")
    parser.add_argument(
        "--course-id", required=True,
        help="Comma-separated course IDs to export (e.g. 30004 or 30004,30005)",
    )
    parser.add_argument(
        "--sub-ids", default="",
        help="Optional comma-separated sub_ids; when set, only those "
             "lectures are exported (used by the frontend's per-lecture "
             "selection in the export dialog)",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Export as PDF attachment instead of inline HTML email",
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Export as Markdown attachment instead of HTML email (experimental)",
    )
    parser.add_argument(
        "--db", default="data/icourse.db",
        help="Database path (default: data/icourse.db)",
    )
    args = parser.parse_args()

    try:
        db_path, temp_db_path = _resolve_db_path(args.db)
    except Exception as e:
        print(str(e))
        sys.exit(1)

    db = Database(db_path)

    # Parse comma-separated course IDs
    course_ids = [cid.strip() for cid in args.course_id.split(",") if cid.strip()]
    if not course_ids:
        print("No valid course IDs provided.")
        sys.exit(1)

    # Parse optional sub_ids filter
    sub_ids = [s.strip() for s in args.sub_ids.split(",") if s.strip()] or None
    if sub_ids:
        print(f"Filtering to {len(sub_ids)} sub_id(s): {', '.join(sub_ids)}")

    if not config.SMTP_EMAIL or not config.SMTP_PASSWORD or not config.RECEIVER_EMAIL:
        print("Email configuration incomplete. Set SMTP_EMAIL, SMTP_PASSWORD, RECEIVER_EMAIL.")
        sys.exit(1)

    try:
        if args.pdf:
            try:
                import weasyprint  # noqa: PLC0415
            except ImportError:
                print("weasyprint is required for PDF export. Install it with: pip install weasyprint")
                sys.exit(1)

            # PDF mode: one PDF per course, all PDFs in one email
            attachments: list[tuple[bytes, str]] = []
            titles: list[str] = []
            for cid in course_ids:
                result = _query_course(db, cid, sub_ids=sub_ids)
                if result is None:
                    continue
                course_title, teacher, lectures = result
                titles.append(course_title)

                html = _build_html(course_title, teacher, lectures, pdf=True)
                print(f"Generating PDF for {course_title}...")
                pdf_bytes = weasyprint.HTML(string=html).write_pdf()
                filename = f"{_safe_filename(course_title)}_summaries.pdf"
                attachments.append((pdf_bytes, filename))
                print(f"  PDF ready ({len(pdf_bytes)} bytes): {filename}")

            if not attachments:
                print("No courses with summaries found – nothing to send.")
                sys.exit(0)

            subject = "[iCourse 课程摘要导出] " + ", ".join(titles)
            total_bytes = sum(len(b) for b, _ in attachments)
            print(f"Sending email with {len(attachments)} PDF(s) ({total_bytes} bytes)...")
            _send_pdf_email(subject, attachments)
            print(f"[OK] Sent: {subject}")

        elif args.md:
            # Markdown mode: one MD file per course, all files in one email
            attachments: list[tuple[bytes, str]] = []
            titles: list[str] = []
            for cid in course_ids:
                result = _query_course(db, cid, sub_ids=sub_ids)
                if result is None:
                    continue
                course_title, teacher, lectures = result
                titles.append(course_title)

                markdown:str = _build_plain(course_title, teacher, lectures)
                markdown_bytes = markdown.encode("utf-8")
                filename = f"{_safe_filename(course_title)}_summaries.md"
                attachments.append((markdown_bytes, filename))
                print(f"  Markdown ready ({len(markdown_bytes)} bytes): {filename}")

            if not attachments:
                print("No courses with summaries found – nothing to send.")
                sys.exit(0)

            subject = "[iCourse 课程摘要导出] " + ", ".join(titles)
            total_bytes = sum(len(b) for b, _ in attachments)
            print(f"Sending email with {len(attachments)} MD(s) ({total_bytes} bytes)...")
            _send_md_email(subject, attachments)
            print(f"[OK] Sent: {subject}")

        else:
            # Email mode: one CID-embedded HTML email per course
            sent = 0
            for cid in course_ids:
                result = _query_course(db, cid, sub_ids=sub_ids)
                if result is None:
                    continue
                course_title, teacher, lectures = result

                cid_images: dict[str, bytes] = {}
                html = _build_html(course_title, teacher, lectures,
                                   cid_images=cid_images)
                plain = _build_plain(course_title, teacher, lectures)
                subject = f"[iCourse 课程摘要导出] {course_title}"

                print(f"Sending HTML email for {course_title}...")
                if cid_images:
                    print(f"  Embedded {len(cid_images)} LaTeX image(s) as CID")
                _send_html_email(subject, html, plain, cid_images=cid_images)
                print(f"[OK] Sent: {subject}")
                sent += 1

            if sent == 0:
                print("No courses with summaries found – nothing to send.")
                sys.exit(0)
    finally:
        try:
            db.conn.close()
        except Exception:
            pass
        if temp_db_path:
            try:
                os.remove(temp_db_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
