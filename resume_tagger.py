"""
Resume version tagging.

Picks the best-fitting *existing* resume version from the resume library for a
given JD and rates the fit out of 10. No PDF is generated — this mode only
tells you which file to send.

Library lives outside the repo (Documents/.../RESUME). Override with the
RESUME_DIR env var.
"""

import os
import re
from pathlib import Path

import jsonschema
import pdfplumber

from llm import call_llm

DEFAULT_RESUME_DIR = "/Users/barathsuresh/Documents/Barath Suresh Docs/RESUME"

RESUME_DIR = Path(os.getenv("RESUME_DIR", DEFAULT_RESUME_DIR))

# Master lives one level down; the tailored variants sit at the library root.
_MASTER_REL = Path("Resume_Docx/master/Barath_Suresh_Master_Resume.pdf")

_MAX_CHARS_PER_RESUME = 6000  # 2 pages of text; keeps the prompt bounded

_TAG_SCHEMA = {
    "type": "object",
    "required": ["best_version", "rating", "reasoning", "rankings"],
    "properties": {
        "best_version": {"type": "string"},
        "rating": {"type": "number"},
        "reasoning": {"type": "string"},
        "gaps": {"type": ["string", "null"]},
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["version", "rating", "why"],
                "properties": {
                    "version": {"type": "string"},
                    "rating": {"type": "number"},
                    "why": {"type": "string"},
                },
            },
        },
    },
}

_SYSTEM = (
    "You are a senior technical recruiter screening resumes against a job description. "
    "You are given several EXISTING resume versions for one candidate and one JD. "
    "You do not rewrite anything. You only judge which version, exactly as written, "
    "would perform best for this JD with both an ATS keyword filter and a human recruiter's "
    "6-second skim. "
    "Rate each version 0-10 on fit for THIS JD: 9-10 near-perfect keyword and seniority match, "
    "7-8 strong with minor gaps, 5-6 usable but generic, below 5 wrong specialization. "
    "Be discriminating — do not give every version the same score. "
    "Return only valid JSON matching the provided schema, no markdown, no explanation."
)


def _label(path: Path) -> str:
    """Barath_Suresh_SRE_Platform_Resume.pdf -> 'SRE Platform'."""
    stem = path.stem
    stem = re.sub(r"^Barath_Suresh_", "", stem)
    stem = re.sub(r"_?Resume$", "", stem)
    return stem.replace("_", " ").strip() or path.stem


_pages_cache: dict[tuple[str, float], int] = {}


def _pdf_pages(path: Path) -> int:
    key = (str(path), path.stat().st_mtime)
    if key not in _pages_cache:
        with pdfplumber.open(path) as pdf:
            _pages_cache[key] = len(pdf.pages)
    return _pages_cache[key]


def list_versions() -> list[dict]:
    """Available resume versions, newest-modified last. [{label, path}]

    Multi-page resumes are excluded: a version that cannot be uploaded cannot be
    the answer, and rating it wastes an LLM call before the caller discards it.
    The master lives here to be scored only if it ever becomes one page."""
    candidates = []
    seen = set()

    for pdf in sorted(RESUME_DIR.glob("*.pdf")):
        if pdf.name.startswith("~$"):
            continue
        candidates.append({"label": _label(pdf), "path": pdf})
        seen.add(pdf.name)

    master = RESUME_DIR / _MASTER_REL
    if master.exists() and master.name not in seen:
        candidates.append({"label": _label(master), "path": master})

    versions, skipped = [], []
    for v in candidates:
        try:
            pages = _pdf_pages(v["path"])
        except Exception:
            pages = 1  # unreadable page count is not a reason to drop a candidate
        (versions if pages <= 1 else skipped).append(v)

    if skipped:
        names = ", ".join(f'{v["label"]} ({_pdf_pages(v["path"])}pg)' for v in skipped)
        print(f"  [tagger] skipping multi-page version(s), not uploadable: {names}")

    # Everything is multi-page — score them anyway rather than failing outright.
    return versions or candidates


_text_cache: dict[tuple[str, float], str] = {}


def _extract_text(path: Path) -> str:
    key = (str(path), path.stat().st_mtime)
    if key not in _text_cache:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        _text_cache[key] = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _text_cache[key]


def extract_resume_text(path: Path) -> str:
    """Plain text of a resume PDF. Used to ground a cover letter in the sent resume."""
    return _extract_text(Path(path))


def _build_prompt(jd: str, company: str, role: str, versions: list[dict]) -> str:
    blocks = []
    for v in versions:
        body = _extract_text(v["path"])[:_MAX_CHARS_PER_RESUME]
        blocks.append(f"=== RESUME VERSION: {v['label']} ===\n{body}")

    labels = ", ".join(f'"{v["label"]}"' for v in versions)

    return (
        f"TARGET ROLE: {role or 'unspecified'} at {company or 'unspecified company'}\n\n"
        "JOB DESCRIPTION:\n"
        f"{jd}\n\n"
        + "\n\n".join(blocks)
        + "\n\n"
        "TASK:\n"
        f"1. Rate EVERY version listed above ({labels}) 0-10 for this JD, with a one-line reason each.\n"
        "2. Name the single best version in `best_version` and repeat its score in `rating`.\n"
        "3. In `reasoning`, give 2-3 sentences: which JD keywords and signals that version hits "
        "that the others miss.\n"
        "4. In `gaps`, name what the JD asks for that the winning version still does not show "
        "(null if nothing material is missing).\n\n"
        "`best_version` and every `rankings[].version` MUST be one of the exact labels listed above. "
        "Do not invent version names. Do not rewrite the resumes.\n\n"
        "Return JSON in exactly this shape — these key names, nothing else:\n"
        '{"best_version": "<label>", "rating": <0-10>, "reasoning": "<2-3 sentences>", '
        '"gaps": "<what is still missing, or null>", '
        '"rankings": [{"version": "<label>", "rating": <0-10>, "why": "<one line>"}]}'
    )


# Small models drift on key names ("score"/"reason"); accept and repair.
_KEY_ALIASES = {
    "score": "rating",
    "fit": "rating",
    "reason": "why",
    "justification": "why",
    "label": "version",
    "name": "version",
    "best": "best_version",
    "best_resume": "best_version",
    "explanation": "reasoning",
}


def _rename_keys(obj: dict) -> dict:
    out = dict(obj)
    for alias, canonical in _KEY_ALIASES.items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)
    return out


def _normalize(result: dict) -> dict:
    result = _rename_keys(result)
    result["rankings"] = [
        _rename_keys(r) for r in (result.get("rankings") or []) if isinstance(r, dict)
    ]
    return result


def tag_resume_version(jd: str, company: str = "", role: str = "") -> dict:
    """Pick the best existing resume version for a JD.

    Returns {best_version, rating, reasoning, gaps, rankings, path}.
    `path` is the file to actually send, or None if the label didn't resolve.
    """
    versions = list_versions()
    if not versions:
        raise RuntimeError(f"No resume PDFs found in {RESUME_DIR}")

    # Validate after normalizing, so an aliased key name doesn't burn a retry.
    result = _normalize(call_llm(_SYSTEM, _build_prompt(jd, company, role, versions), schema=None))
    jsonschema.validate(result, _TAG_SCHEMA)

    by_label = {v["label"].lower(): v["path"] for v in versions}
    best = str(result.get("best_version", "")).strip()
    path = by_label.get(best.lower())

    if path is None:
        # Model returned a name we don't have — fall back to the top-rated ranking
        ranked = sorted(
            (r for r in result.get("rankings", []) if str(r.get("version", "")).lower() in by_label),
            key=lambda r: r.get("rating", 0),
            reverse=True,
        )
        if ranked:
            best = ranked[0]["version"]
            result["rating"] = ranked[0].get("rating", result.get("rating"))
            path = by_label[best.lower()]
        result["best_version"] = best

    result["rating"] = max(0, min(10, round(float(result.get("rating") or 0), 1)))
    result["path"] = str(path) if path else None
    return result


def format_report(result: dict) -> str:
    """Human-readable summary for CLI logs and the UI."""
    lines = [
        f"Best version: {result['best_version']} — {result['rating']}/10",
        f"File: {result.get('path') or '(unresolved)'}",
        "",
        result.get("reasoning", ""),
    ]
    if result.get("gaps"):
        lines += ["", f"Gaps: {result['gaps']}"]
    if result.get("rankings"):
        lines += ["", "All versions:"]
        for r in sorted(result["rankings"], key=lambda r: r.get("rating", 0), reverse=True):
            lines.append(f"  {r['rating']}/10  {r['version']} — {r['why']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Tag the best resume version for a JD.")
    parser.add_argument("--jd", help="Job description text")
    parser.add_argument("--jd-file", help="Path to a file containing the JD")
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--list", action="store_true", help="List available versions and exit")
    args = parser.parse_args()

    if args.list:
        for v in list_versions():
            print(f"{v['label']:<16} {v['path']}")
        raise SystemExit(0)

    jd_text = args.jd or (Path(args.jd_file).read_text() if args.jd_file else "")
    if not jd_text.strip():
        raise SystemExit("Provide --jd or --jd-file")

    print(format_report(tag_resume_version(jd_text, args.company, args.role)))
