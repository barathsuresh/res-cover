"""
Cover letter PDF builder using reportlab.
Usage: build_cover_letter(text, company, role, output_path)

text: full letter body starting with "Dear Hiring Manager," through final
      paragraph. Date, recipient block, closing ("Sincerely,") and signature
      are appended automatically.

Layout is a classic block-format business letter — everything flush left,
no letterhead banner, no rule, no color — the way a formal letter is
actually laid out, not a resume header repeated on a second page.
"""

import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

LM = RM = 1.0 * inch
TM = BM = 1.0 * inch

_BASE = dict(fontName="Arial", fontSize=11, leading=16)

STYLES = {
    "sender":  ParagraphStyle("cl_sender",  **_BASE, alignment=TA_LEFT, spaceAfter=0),
    "date":    ParagraphStyle("cl_date",    **_BASE, alignment=TA_LEFT, spaceAfter=0),
    "to":      ParagraphStyle("cl_to",      **_BASE, alignment=TA_LEFT, spaceAfter=0),
    "body":    ParagraphStyle("cl_body",    **_BASE, alignment=TA_LEFT, spaceAfter=12),
    "closing": ParagraphStyle("cl_closing", **_BASE, alignment=TA_LEFT, spaceAfter=0),
    "sig":     ParagraphStyle("cl_sig",     **_BASE, alignment=TA_LEFT, spaceAfter=0),
}


def _para(text: str, style: str = "body") -> Paragraph:
    from resume_builder import esc
    return Paragraph(esc(text), STYLES[style])

def _spacer(h: int = 12) -> Spacer:
    return Spacer(1, h)


def build_cover_letter(text: str, company: str, role: str, output_path: str):
    from resume_builder import BASE_RESUME_DATA
    name    = BASE_RESUME_DATA["name"]
    contact = BASE_RESUME_DATA["contact"]

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=LM, rightMargin=RM,
        topMargin=TM, bottomMargin=BM,
        title=f"Cover Letter – {company} – {role}",
        author=name,
        subject="Cover Letter",
        creator="",
        producer="",
    )

    story = []

    # ── Sender block ────────────────────────────
    # Two lines, not one per field — six one-line fields reads sparse and
    # un-letter-like. Location/phone first, then the online-presence fields.
    parts = contact.split(" | ")
    story.append(_para(name, "sender"))
    story.append(_para(" | ".join(parts[:2]), "sender"))
    if len(parts) > 2:
        story.append(_para(" | ".join(parts[2:]), "sender"))
    story.append(_spacer())

    # ── Date ────────────────────────────────────
    date_str = datetime.date.today().strftime("%B %d, %Y").replace(" 0", " ")
    story.append(_para(date_str, "date"))
    story.append(_spacer())

    # ── Recipient block ─────────────────────────
    story.append(_para("Hiring Manager", "to"))
    if company:
        story.append(_para(company, "to"))
    story.append(_spacer())

    # ── Body (salutation is the model's first paragraph) ──
    raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for block in raw_paragraphs:
        story.append(_para(block.replace("\n", " ")))

    # ── Closing ─────────────────────────────────
    story.append(_para("Sincerely,", "closing"))
    story.append(_spacer(28))
    story.append(_para(name, "sig"))

    doc.build(story)
    print(f"Cover letter saved: {output_path}")
