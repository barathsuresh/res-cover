"""
Cover letter PDF builder using reportlab.
Usage: build_cover_letter(text, company, role, output_path)

text: full letter body starting with "Dear Hiring Team," through final paragraph.
      Header, date, closing ("Sincerely,"), and signature are appended automatically.
"""

import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib import colors

LM = RM = 0.75 * inch
TM = BM = 0.65 * inch

_BASE  = dict(fontName="Times-Roman", fontSize=10.5, leading=15)
_BOLD  = dict(fontName="Times-Bold",  fontSize=10.5, leading=15)
_SMALL = dict(fontName="Times-Roman", fontSize=9.5,  leading=13)

STYLES = {
    "name":    ParagraphStyle("cl_name",    fontName="Times-Bold",   fontSize=16, leading=20, alignment=TA_LEFT, spaceAfter=2),
    "contact": ParagraphStyle("cl_contact", fontName="Times-Roman",  fontSize=9,  leading=12, alignment=TA_LEFT, spaceAfter=0, textColor=colors.HexColor("#444444")),
    "date":    ParagraphStyle("cl_date",    **_BASE, spaceAfter=0),
    "to":      ParagraphStyle("cl_to",      **_BASE, spaceAfter=0),
    "body":    ParagraphStyle("cl_body",    **_BASE, spaceAfter=10),
    "closing": ParagraphStyle("cl_closing", **_BASE, spaceAfter=0),
    "sig":     ParagraphStyle("cl_sig",     **_BOLD, spaceAfter=0),
}


def _para(text: str, style: str = "body") -> Paragraph:
    from resume_builder import esc
    return Paragraph(esc(text), STYLES[style])

def _spacer(h: int = 6) -> Spacer:
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

    # ── Header ──────────────────────────────────
    story.append(_para(name, "name"))
    story.append(_para(contact, "contact"))
    story.append(_spacer(6))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#888888"), spaceAfter=8))

    # ── Date ────────────────────────────────────
    date_str = datetime.date.today().strftime("%B %d, %Y").replace(" 0", " ")
    story.append(_para(date_str, "date"))
    story.append(_spacer(14))

    # ── To block ────────────────────────────────
    story.append(_para("Hiring Manager", "to"))
    if company:
        story.append(_para(company, "to"))
    story.append(_spacer(14))

    # ── Body ────────────────────────────────────
    raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for block in raw_paragraphs:
        story.append(_para(block.replace("\n", " ")))

    # ── Closing ─────────────────────────────────
    story.append(_spacer(6))
    story.append(_para("Sincerely,", "closing"))
    story.append(_spacer(20))
    story.append(_para(name, "sig"))

    doc.build(story)
    print(f"Cover letter saved: {output_path}")
