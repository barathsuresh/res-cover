"""
ATS-friendly resume PDF generator using reportlab.
Usage (standalone):  python resume_builder.py
Usage (pipeline):    build_resume(data_dict, output_path)

Edit data.json to update resume content — it is the source of truth.
"""

import json
import os
import re
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable
)
from reportlab.lib import colors

# ─────────────────────────────────────────────
#  BASE RESUME DATA  ← loaded from data.json
# ─────────────────────────────────────────────

_DATA_JSON = os.path.join(os.path.dirname(__file__), "data.json")
_CONTACT_JSON = os.path.join(os.path.dirname(__file__), "contact.json")

with open(_DATA_JSON, "r", encoding="utf-8") as _f:
    BASE_RESUME_DATA = json.load(_f)

# contact.json is gitignored — real phone/email live there so data.json (tracked
# in git) only ever holds a placeholder. Falls back to data.json's contact field
# if contact.json is absent (e.g. a fresh clone before the user creates it).
if os.path.exists(_CONTACT_JSON):
    with open(_CONTACT_JSON, "r", encoding="utf-8") as _f:
        BASE_RESUME_DATA["contact"] = json.load(_f)["contact"]

# ─────────────────────────────────────────────
#  LAYOUT CONSTANTS
# ─────────────────────────────────────────────

W, H = letter
# 0.5in on every side — the "narrow margins" figure recommended for ATS-parsed
# resumes. Tighter than this risks clipping in print and in some parsers.
LM = RM = 0.5 * inch
TM = BM = 0.5 * inch


# ─────────────────────────────────────────────
#  STYLES
# ─────────────────────────────────────────────

DEFAULT_LEADING = 11.25

def build_styles(leading: float = DEFAULT_LEADING):
    base = dict(fontName="Helvetica", fontSize=10, leading=leading)
    bold = dict(fontName="Helvetica-Bold",  fontSize=10, leading=leading)

    return {
        "name": ParagraphStyle("name",
            fontName="Helvetica-Bold", fontSize=14, leading=16.5,
            alignment=TA_CENTER, spaceAfter=1),

        "contact": ParagraphStyle("contact",
            fontName="Helvetica", fontSize=8.8, leading=10.5,
            alignment=TA_CENTER, spaceAfter=1),

        "section": ParagraphStyle("section",
            fontName="Helvetica-Bold", fontSize=10, leading=12,
            spaceBefore=1, spaceAfter=0, textTransform="uppercase"),

        "org": ParagraphStyle("org", **bold, spaceBefore=0, spaceAfter=0),

        "role": ParagraphStyle("role",
            fontName="Helvetica-Oblique", fontSize=9.8, leading=12.0),

        "body": ParagraphStyle("body", **base),

        "bullet": ParagraphStyle("bullet",
            **base, leftIndent=13, firstLineIndent=-9, spaceAfter=0),

        "summary": ParagraphStyle("summary",
            **base, spaceAfter=2),

        "extra": ParagraphStyle("extra", **base, spaceAfter=0),
    }


# ─────────────────────────────────────────────
#  MARKUP SAFETY
# ─────────────────────────────────────────────
#  Paragraph text is parsed as mini-HTML by reportlab, so a bare "&" (e.g.
#  "Q&A") or "<" from data.json / the LLM either renders wrong or raises.
#  Escape those without touching the intentional tags the resume uses.

_ALLOWED_TAGS = "b|i|u|br|sub|super|font|a|para"

_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-fA-F]+);)")
_BARE_LT = re.compile(rf"<(?!/?(?:{_ALLOWED_TAGS})\b[^<>]*>)")


def esc(text) -> str:
    """Escape stray & and < while preserving supported inline markup."""
    if text is None:
        return ""
    return _BARE_LT.sub("&lt;", _BARE_AMP.sub("&amp;", str(text)))


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def hr():
    return HRFlowable(width="100%", thickness=0.8, color=colors.black,
                      spaceAfter=0, spaceBefore=0)

def spacer(h=3):
    return Spacer(1, h)

def bullet_item(text, styles):
    # ASCII hyphen, not "•": the standard-14 fonts carry no ToUnicode map
    # for the bullet glyph, so ATS text extraction reads it as "(cid:127)".
    return Paragraph(f"-&nbsp;&nbsp;{esc(text)}", styles["bullet"])

def _project_title(entry, styles):
    url = entry.get("link_url", "")
    link_label = esc(entry.get("link", "GitHub"))
    link_part = (
        f'<a href="{esc(url)}" color="black"><u>{link_label}</u></a>'
        if url else link_label
    )
    return Paragraph(
        f"<b>{esc(entry['title'])}</b> | {link_part} | <i>{esc(entry['tech'])}</i>",
        styles["body"],
    )


# ─────────────────────────────────────────────
#  BUILD
# ─────────────────────────────────────────────

def build_resume(data: dict, output_path: str, leading: float = DEFAULT_LEADING):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=LM, rightMargin=RM,
        topMargin=TM, bottomMargin=BM,
        title=data["name"],
        author=data["name"],
        subject="Resume",
        creator="",
        producer="",
    )

    S = build_styles(leading)
    story = []

    # ── Header ──
    story.append(Paragraph(esc(data["name"]), S["name"]))
    story.append(Paragraph(esc(data["contact"]), S["contact"]))

    # ── Summary (optional, for keyword coverage) ──
    if data.get("summary"):
        story.append(spacer(1))
        story.append(Paragraph("Summary", S["section"]))
        story.append(hr())
        story.append(Paragraph(esc(data["summary"]), S["summary"]))

    # ── Education ──
    story.append(spacer(1))
    story.append(Paragraph("Education", S["section"]))
    story.append(hr())

    for i, edu in enumerate(data["education"]):
        if i > 0:
            story.append(spacer(1))
        # Single line, same reasoning as Work Experience below — no table for a
        # parser to transpose.
        #   [School], [Location] | [Degree] | [Dates]
        story.append(Paragraph(
            f"<b>{esc(edu['school'])}</b>, {esc(edu['location'])} | "
            f"<i>{esc(edu['degree'])}</i> | {esc(edu['dates'])}",
            S["body"],
        ))

        for ex in edu.get("extra", []):
            story.append(Paragraph(esc(ex), S["extra"]))

    # ── Skills ──
    story.append(spacer(1))
    story.append(Paragraph("Skills", S["section"]))
    story.append(hr())

    for item in data["skills"]:
        label, value = item[0], item[1]
        story.append(Paragraph(f"<b>{esc(label)}:</b> {esc(value)}", S["body"]))

    # ── Experience ──
    story.append(spacer(1))
    story.append(Paragraph("Work Experience", S["section"]))
    story.append(hr())

    for i, exp in enumerate(data["experience"]):
        if i > 0:
            story.append(spacer(1))
        # One flowing line rather than a two-row table: ATS parsers read a
        # single Paragraph in reading order with no risk of a table transposing
        # company/date or role/location.
        #   [Company], [Location] | [Title] | [Dates]
        story.append(Paragraph(
            f"<b>{esc(exp['company'])}</b>, {esc(exp['location'])} | "
            f"<i>{esc(exp['role'])}</i> | {esc(exp['dates'])}",
            S["body"],
        ))

        for b in exp["bullets"]:
            story.append(bullet_item(b, S))

    # ── Projects ──
    story.append(spacer(1))
    story.append(Paragraph("Projects", S["section"]))
    story.append(hr())

    for i, proj in enumerate(data["projects"]):
        if i > 0:
            story.append(spacer(1))
        story.append(_project_title(proj, S))

        for b in proj["bullets"]:
            story.append(bullet_item(b, S))

    # ── Open Source Contributions (optional) ──
    if data.get("open_source"):
        story.append(spacer(1))
        story.append(Paragraph("Open Source Contributions", S["section"]))
        story.append(hr())

        for i, item in enumerate(data["open_source"]):
            if i > 0:
                story.append(spacer(1))
            story.append(_project_title(item, S))

            for b in item["bullets"]:
                story.append(bullet_item(b, S))

    doc.build(story)
    print(f"Resume saved: {output_path}")


if __name__ == "__main__":
    build_resume(BASE_RESUME_DATA, "Barath_Suresh_Resume.pdf")
