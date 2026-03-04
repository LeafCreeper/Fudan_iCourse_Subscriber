import re
import smtplib
import time
import uuid
import requests
from io import BytesIO
from PIL import Image
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from html import escape
from email.utils import formataddr
from urllib.parse import quote

import markdown

from . import config

_MD_EXTENSIONS = ["tables", "fenced_code", "nl2br", "sane_lists"]

_EMAIL_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
}
h3 {
    color: #34495e;
    margin-top: 24px;
}
h3 small {
    color: #7f8c8d;
    font-weight: normal;
}
h4 { color: #555; margin-top: 18px; }
hr {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 28px 0;
}
strong { color: #c0392b; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background: #f5f6fa;
    font-weight: 600;
}
tr:nth-child(even) { background: #fafafa; }
pre {
    background: #f4f4f4;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 13px;
}
p code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #3498db;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f8f9fa;
    color: #555;
}
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
"""

_PDF_LATEX_SCALE = 2.5
_MIN_INLINE_HEIGHT = 13  # minimum logical height for inline formulas (px)

_IMAGE_CACHE: dict[str, tuple] = {}


def _fetch_latex_image(url: str, dpi: int = 300) -> tuple:
    """Fetch rendered LaTeX image, return (width, height, png_bytes).

    Width/height are logical display pixels (DPI-adjusted).
    Returns (None, None, None) on failure.
    """
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]

    try:
        scale_factor = dpi / 96.0
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        img = Image.open(BytesIO(response.content))
        logical_width = max(1, int(img.width / scale_factor))
        logical_height = max(1, int(img.height / scale_factor))

        result = (logical_width, logical_height, response.content)
        _IMAGE_CACHE[url] = result
        return result
    except Exception as e:
        print(f"[LaTeX Render] Image fetch failed: {e}")
        return None, None, None
def _md_to_html(md_text: str, pdf_mode: bool = False,
                cid_images: dict | None = None) -> str:
    """Convert Markdown to styled HTML, rendering LaTeX math as images.

    Processing order: extract LaTeX → markdown convert → restore as <img>.
    This prevents the markdown engine from corrupting backslash escapes.

    Args:
        md_text: Markdown source text.
        pdf_mode: Scale LaTeX dimensions for PDF output.
        cid_images: When provided (dict), download images and embed via CID
                    references instead of external URLs.  The dict is populated
                    with {cid_name: png_bytes} entries for the caller to attach
                    to the MIME message.
    """
    latex_map: dict[str, str] = {}
    counter = 0

    def _stash(match):
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = match.group(0)
        return key

    def _stash_block(match):
        """Stash \\[...\\] as $$...$$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$$" + match.group(1) + "$$"
        return key

    def _stash_inline(match):
        """Stash \\(...\\) as $...$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$" + match.group(1) + "$"
        return key

    # Extract LaTeX in order: block first, then inline
    # 1) $$...$$ block formulas
    text = re.sub(r"\$\$(.+?)\$\$", _stash, md_text, flags=re.DOTALL)
    # 2) \[...\] block formulas (normalize to $$...$$)
    text = re.sub(r"\\\[(.+?)\\\]", _stash_block, text, flags=re.DOTALL)
    # 3) $...$ inline formulas (not $$)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash, text)
    # 4) \(...\) inline formulas (normalize to $...$)
    text = re.sub(r"\\\((.+?)\\\)", _stash_inline, text)

    html = markdown.markdown(text, extensions=_MD_EXTENSIONS)

    for key, original in latex_map.items():
        if original.startswith("$$"):
            latex_content = original[2:-2]
            url = (
                "https://latex.codecogs.com/png.latex?"
                r"\dpi{300}\bg{white}"
                f"%20{quote(latex_content)}"
            )
            w, h, img_data = _fetch_latex_image(url)

            if w and h:
                if pdf_mode:
                    w = max(1, int(w / _PDF_LATEX_SCALE))
                    h = max(1, int(h / _PDF_LATEX_SCALE))

                src = _resolve_src(url, img_data, cid_images)
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:middle;border:none;display:inline-block;">'
                    f'</div>'
                )
            else:
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<code>{escape(latex_content)}</code></div>'
                )
        else:
            latex_content = original[1:-1]
            url = (
                "https://latex.codecogs.com/png.latex?"
                r"\dpi{300}\bg{white}\inline"
                f"%20{quote(latex_content)}"
            )
            w, h, img_data = _fetch_latex_image(url)

            if w and h:
                if pdf_mode:
                    w = max(1, int(w / _PDF_LATEX_SCALE))
                    h = max(1, int(h / _PDF_LATEX_SCALE))
                # Enforce minimum height so formulas aren't smaller than text
                if not pdf_mode and h < _MIN_INLINE_HEIGHT:
                    scale = _MIN_INLINE_HEIGHT / h
                    w = max(1, int(w * scale))
                    h = _MIN_INLINE_HEIGHT

                src = _resolve_src(url, img_data, cid_images)
                img_tag = (
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:-3px;border:none;margin:0 2px;">'
                )
            else:
                img_tag = (
                    f'<code>{escape(latex_content)}</code>'
                )

        html = html.replace(key, img_tag)

    return html


def _resolve_src(url: str, img_data: bytes | None,
                 cid_images: dict | None) -> str:
    """Return a CID reference if embedding, otherwise the original URL."""
    if cid_images is not None and img_data:
        cid = f"latex-{uuid.uuid4().hex[:12]}"
        cid_images[cid] = img_data
        return f"cid:{cid}"
    return url

class Emailer:
    """Send course summary emails via QQ SMTP SSL."""

    def __init__(self):
        self.host = config.SMTP_HOST
        self.port = config.SMTP_PORT
        self.sender = config.SMTP_EMAIL
        self.password = config.SMTP_PASSWORD
        self.receiver = config.RECEIVER_EMAIL

    def send(self, items: list[dict]) -> bool:
        """Send a single email containing all lecture summaries.

        LaTeX formulas are rendered as PNG images and embedded directly into
        the email via CID attachments, so they display on all clients
        (including mobile) without loading external images.

        Args:
            items: List of dicts, each with keys:
                   course_title, sub_title, date, summary

        Returns:
            True if email was sent successfully, False otherwise.
        """
        if not items:
            return True

        # Group by course (preserve insertion order)
        courses: OrderedDict[str, list[dict]] = OrderedDict()
        for item in items:
            courses.setdefault(item["course_title"], []).append(item)

        # Subject
        parts = [f"{ct} ({len(lecs)})" for ct, lecs in courses.items()]
        subject = f"[iCourse 课程内容更新] {', '.join(parts)}"

        # Plain text (Markdown as-is, readable without rendering)
        plain_sections = []
        for course_title, lectures in courses.items():
            plain_sections.append(f"{'=' * 40}")
            plain_sections.append(f"课程：{course_title}")
            plain_sections.append(f"{'=' * 40}")
            for lec in lectures:
                plain_sections.append(
                    f"\n--- {lec['sub_title']} ({lec['date']}) ---\n"
                )
                plain_sections.append(lec["summary"])
        plain = "\n".join(plain_sections)

        # HTML (Markdown → styled HTML with CID-embedded LaTeX images)
        cid_images: dict[str, bytes] = {}
        body_parts = []
        for course_title, lectures in courses.items():
            body_parts.append(f"<h2>{escape(course_title)}</h2>")
            for lec in lectures:
                body_parts.append(
                    f"<h3>{escape(lec['sub_title'])} "
                    f"<small>({escape(lec['date'])})</small></h3>"
                )
                body_parts.append(
                    _md_to_html(lec["summary"], cid_images=cid_images)
                )
                body_parts.append("<hr>")

        html = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'>"
            f"<style>{_EMAIL_CSS}</style>"
            "</head><body>"
            + "\n".join(body_parts)
            + "</body></html>"
        )

        # Build MIME: related > alternative > (plain, html) + image attachments
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        msg["To"] = self.receiver

        msg_alt = MIMEMultipart("alternative")
        msg_alt.attach(MIMEText(plain, "plain", "utf-8"))
        msg_alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(msg_alt)

        # Attach CID images
        for cid, png_data in cid_images.items():
            img_part = MIMEImage(png_data, "png")
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline",
                                filename=f"{cid}.png")
            msg.attach(img_part)

        if cid_images:
            print(f"[Emailer] Embedded {len(cid_images)} LaTeX images as CID")

        # Retry with exponential backoff
        for attempt in range(3):
            try:
                with smtplib.SMTP_SSL(self.host, self.port) as server:
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, self.receiver, msg.as_string())
                print(f"[Emailer] Sent: {subject}")
                return True
            except Exception as e:
                print(f"[Emailer] Attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)

        print("[Emailer] All send attempts failed.")
        return False
