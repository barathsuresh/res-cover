"""
Job application pipeline.
Usage: python pipeline.py
Processes all pending rows in jobs.db.
"""

import copy
import json
import re
import sqlite3
import traceback
from pathlib import Path

import pdfplumber

from cover_letter_builder import build_cover_letter
from db_setup import DB_PATH, save_tag
from keyword_analysis import analyze_keyword_coverage, analyze_resume_bullets, print_analysis_report
from llm import SYSTEM_PROMPT, call_llm
from resume_builder import BASE_RESUME_DATA, DEFAULT_LEADING, build_resume
from resume_tagger import extract_resume_text, format_report, tag_resume_version

# ─────────────────────────────────────────────
#  COVER LETTER STYLE REFERENCE
#  Shown to LLM as style/tone guide only.
#  LLM must NOT copy this content — must write fresh for the target JD.
# ─────────────────────────────────────────────

_CL_STYLE_SAMPLE = """\
Dear Hiring Manager,

Identity management at Apple's scale, billions of customers, planet-scale availability, and \
mission-critical security, is one of the most consequential backend problems in the industry. The \
gap between a well-engineered identity system and a poorly engineered one is measured in trust, \
and that's not recoverable once lost.

My stack aligns directly with the work this role involves. At Tata Elxsi, I built production Java \
and Spring Boot services including a JWT-based OAuth 2.0 optimization using Spring Security \
that reduced authorization latency by 93% across 50+ secured endpoints by eliminating \
redundant Azure AD group lookups at authentication time. I also built a global exception handler \
that reduced server errors by 50% across a live application, containerized a full 6-service \
observability stack with Prometheus, Grafana, and Loki, and implemented CI/CD pipelines via \
GitHub Actions.

On the project side, Prism is a distributed video streaming platform across 8 decoupled Spring \
Boot microservices with PostgreSQL, MongoDB, Redis, Docker, Kubernetes-ready \
containerization, scope-grained multi-tenant API key enforcement, and 100% Zipkin trace \
sampling. Throttlr is a rate limiting engine using atomic Redis Lua scripts, tuned from 9.8 to \
109.5 req/sec on GCP Cloud Run across 20,000 requests with zero correctness errors under \
concurrent load. Both are on GitHub at github.com/barathsuresh.

I'm pursuing my master's in computer science at ASU, fluent in Java, Spring Boot, REST APIs, \
Redis, MongoDB, PostgreSQL, Docker, and Kubernetes, and deeply interested in contributing to \
infrastructure that billions of people depend on every day.

I'd welcome the chance to contribute to Apple's Identity Management team.\
"""

# ─────────────────────────────────────────────
#  OUTPUT SCHEMA sent to LLM
# ─────────────────────────────────────────────

_OUTPUT_SCHEMA = {
    "company": "string — company name",
    "role":    "string — job role/title",
    "resume_data": {
        "name":    "string",
        "contact": "string",
        "summary": "ALWAYS null. This resume has no summary section — never write one.",
        "education": [
            {
                "school":   "string",
                "location": "string",
                "degree":   "string",
                "dates":    "string",
                "extra":    ["string"],
            }
        ],
        "skills": [
            ["category label", "comma-separated skills — reorder to front-load JD-relevant ones; may add skills demonstrably present in experience/project bullets but missing here"]
        ],
        "experience": [
            {
                "company":  "string",
                "role":     "string",
                "location": "string",
                "dates":    "string",
                "bullets":  [
                    "WRITE EXACTLY THE SAME NUMBER OF BULLETS as this entry has in BASE RESUME DATA — no more, no fewer. "
                    "REWRITE from scratch using JD language — do NOT copy base bullet text. "
                    "STRICT XYZ TEMPLATE: '[Strong impact verb] [X: outcome framed in JD's terminology] by [Y: exact metric] by [Z: technical action using JD's exact keywords].' "
                    "Example for a JD mentioning 'IAM' and 'latency': 'Reduced IAM authorization latency by 93% (100ms→7ms) by embedding Azure AD role claims into JWT at auth time, eliminating redundant group lookups.' "
                    "MUST start with impact verb (Reduced/Eliminated/Scaled/Shipped/Secured/Accelerated/Engineered/Optimized). NEVER start with Built/Implemented/Worked/Helped. "
                    "NEVER use em dashes (—) or en dashes (–) anywhere in bullet text.",
                    "bullet 2 — same rules (only if base entry has a 2nd bullet)",
                    "bullet N — same rules (bullet count MUST equal the base entry's bullet count)",
                ],
            }
        ],
        "projects": [
            {
                "title":    "string — keep original project title unchanged",
                "link":     "string — display label e.g. GitHub",
                "link_url": "string — full URL e.g. https://github.com/user/repo, or empty string",
                "tech":     "string — REWRITE tech stack to front-load JD-relevant technologies first; may surface adjacent tech implied by the project (e.g. if JD wants Java and project used Spring Boot, list Java first); keep it honest to what the project actually used",
                "bullets":  [
                    "WRITE EXACTLY THE SAME NUMBER OF BULLETS as this project has in BASE RESUME DATA "
                    "(some have 3, some have 4) — no more, no fewer. "
                    "REWRITE from scratch using JD language — do NOT copy base bullet text. "
                    "STRICT XYZ TEMPLATE: '[Strong impact verb] [X: outcome framed in JD's terminology] by [Y: exact metric] by [Z: technical action using JD's exact keywords].' "
                    "Example for a JD mentioning 'high-throughput systems': 'Scaled high-throughput request handling 11.2x (9.8→109.5 req/sec) across 20,000 requests with zero errors by resolving a Cloud Run bottleneck through CPU, memory, and autoscaling tuning.' "
                    "MUST start with impact verb. NEVER start with Built/Implemented/Worked/Helped. "
                    "Frame every bullet through the lens of what this specific JD values — AI-first JD gets AI/automation framing, infra JD gets scale/reliability framing, etc. "
                    "NEVER use em dashes (—) or en dashes (–) anywhere in bullet text.",
                    "bullet 2 — same rules",
                    "bullet 3 — same rules",
                    "bullet 4 — same rules (only if the base entry has a 4th bullet)",
                ],
            }
        ],
        "open_source": [
            {
                "title":    "string — keep original title unchanged",
                "link":     "string — display label e.g. GitHub",
                "link_url": "string — keep original URL unchanged",
                "tech":     "string — keep original tech stack; may reorder to front-load JD-relevant technologies",
                "bullets":  [
                    "OMIT the whole open_source key when this JD gives no reason to include it. When you do include it, "
                    "KEEP BOTH BULLETS from the base entry. You may lightly reword to mirror JD "
                    "keywords, but preserve every fact and metric exactly (stars, forks, bug details). "
                    "Do NOT invent new open source contributions. "
                    "NEVER use em dashes (—) or en dashes (–) anywhere in bullet text.",
                    "bullet 2 — same rules",
                ],
            }
        ],
    },
    "cover_letter_text": (
        "string — full cover letter starting with 'Dear Hiring Manager,' through final paragraph. No signature line. "
        "Voice: confident, direct, human — written like one engineer talking to another over coffee, not a formal application. "
        "CRITICAL: Do NOT copy, paraphrase, or reword any resume bullet. Write entirely from scratch as if recalling "
        "the work from memory in a conversation. A human writing a cover letter does not read their resume while writing it — "
        "they describe projects and decisions in their own words, emphasizing what they found interesting or hard. "
        "Structure: "
        "(1) Opening paragraph: a specific insight about the company's problem or domain that shows genuine understanding — "
        "not 'I am excited to apply', but a real observation about why the problem is hard or consequential. "
        "(2) Middle paragraph(s): tell the story of relevant work in your own voice. Reference a project or experience, "
        "explain the decision you made and why, and what it taught you — not a metric dump. One number used as evidence "
        "in a sentence is fine; a list of metrics from the resume is not. Show taste and judgment, not a portfolio readout. "
        "(3) Closing: one short sentence, natural and direct, naming the specific team or problem area. "
        "Avoid: copying resume phrasing, corporate buzzwords, 'I am passionate about', 'I am writing to apply', "
        "filler superlatives, starting consecutive sentences with 'I', robotic metric dumps without context, em dashes (—) or en dashes (–) anywhere in the letter."
    ),
}


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _sanitize(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_") or "Unknown"


def _page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def _fill_ratio(pdf_path: str) -> float:
    """Returns fraction of page height used by text content (0.0–1.0)."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        words = page.extract_words()
        if not words:
            return 0.0
        last_y = max(w["bottom"] for w in words)
        return last_y / page.height


# ─────────────────────────────────────────────
#  LENGTH BUDGETS
#  Base data is proven to fit 1 page. Pin LLM output to base structure
#  (same bullet counts) and base character totals (+5% slack) so the
#  rendered resume can never grow past what the base layout absorbs.
# ─────────────────────────────────────────────

_BUDGET_SLACK = 1.05
_PROJECT_PICK_COUNT = 3

# Most bullets any one experience entry may render. The model picks which ones,
# by JD relevance — TATA full-time has 5 candidates and ships the best 4. Entries
# with fewer base bullets than this keep all of them.
_EXPERIENCE_BULLET_CAP = 4

# Per-attempt budget multiplier. The master resume runs longer than one page, so
# attempt 1 asks for master-length and each retry demands real compression.
_SHRINK_SCHEDULE = (1.0, 0.85, 0.75)


def _exp_want(base_bullets: list[str]) -> int:
    """How many bullets this experience entry should render."""
    return min(len(base_bullets), _EXPERIENCE_BULLET_CAP)


# How much of master length actually fits on one page. Measured, not assumed:
# the master runs 2 pages by design, so budgets derived from it at face value ask
# for 2 pages and attempt 1 can never fit. Computed once per process (~70ms) and
# re-derived automatically whenever data.json changes.
_FIT_SCALE: float | None = None
_FIT_SAFETY = 0.98  # margin so a model that spends its budget exactly still fits


def _fit_scale() -> float:
    global _FIT_SCALE
    if _FIT_SCALE is not None:
        return _FIT_SCALE

    import tempfile

    # Worst case: the three projects carrying the most bullet text.
    heaviest = sorted(
        BASE_RESUME_DATA["projects"],
        key=lambda p: sum(len(b) for b in p["bullets"]),
        reverse=True,
    )[:_PROJECT_PICK_COUNT]
    probe = copy.deepcopy(BASE_RESUME_DATA)
    probe["projects"] = copy.deepcopy(heaviest)
    _restore_missing_sections(probe)

    def fits(scale: float) -> bool:
        trial = copy.deepcopy(probe)
        for kind in ("experience", "projects", "open_source"):
            for entry in trial.get(kind, []):
                n = len(entry["bullets"]) or 1
                # Same formula _bullets_budget uses, slack included — otherwise
                # the real budget runs 5% over what was calibrated and overflows.
                per = int(sum(len(b) for b in entry["bullets"]) * scale * _BUDGET_SLACK) // n
                entry["bullets"] = [b[:per].rstrip() for b in entry["bullets"]]
        path = Path(tempfile.mkdtemp()) / "probe.pdf"
        try:
            build_resume(trial, str(path))
            return _page_count(str(path)) <= 1
        finally:
            path.unlink(missing_ok=True)

    lo, hi = 0.25, 1.0
    if fits(hi):
        _FIT_SCALE = 1.0
        return _FIT_SCALE
    for _ in range(8):
        mid = (lo + hi) / 2
        if fits(mid):
            lo = mid
        else:
            hi = mid
    _FIT_SCALE = round(lo * _FIT_SAFETY, 3)
    print(f"  [calibration] one page holds {_FIT_SCALE:.0%} of master bullet length")
    return _FIT_SCALE


def _bullets_budget(bullets: list[str], shrink: float = 1.0, count: int | None = None,
                    apply_fit: bool = True) -> int:
    """Character budget for an entry. When `count` is fewer than the base bullet
    count, the budget scales down with it — otherwise dropping a bullet would just
    license the survivors to grow into the space it freed.

    `apply_fit` scales the budget to what one page actually holds. Turn it off to
    compare against raw master length (the inflation check in `_clamp_to_base`)."""
    if not bullets:
        return 0
    total = sum(len(b) for b in bullets)
    if count is not None and count < len(bullets):
        total = total / len(bullets) * count
    if apply_fit:
        total *= _fit_scale()
    return int(total * _BUDGET_SLACK * shrink)


def _budget_block(shrink: float = 1.0) -> str:
    lines = [
        "HARD LENGTH BUDGETS — character totals per entry, derived from the master resume. "
        "The master runs long, so these are TIGHTENED targets: you must compress the wording "
        "to hit them while keeping every metric and every fact. That is what guarantees the "
        "1-page fit. DO NOT EXCEED:"
    ]
    for exp in BASE_RESUME_DATA["experience"]:
        have, n = len(exp["bullets"]), _exp_want(exp["bullets"])
        pick = (
            f"exactly {n} bullet(s)" if n == have
            else f"exactly {n} bullets — CHOOSE the {n} most JD-relevant of the {have} in the base "
                 f"data and drop the other {have - n} entirely"
        )
        lines.append(
            f'- Experience "{exp["company"]} — {exp["role"]}": {pick}, '
            f'combined length of all bullets <= {_bullets_budget(exp["bullets"], shrink, n)} characters'
        )
    for proj in BASE_RESUME_DATA["projects"]:
        lines.append(
            f'- Project "{proj["title"]}": exactly {len(proj["bullets"])} bullets, '
            f'combined length <= {_bullets_budget(proj["bullets"], shrink)} characters'
        )
    for os_entry in BASE_RESUME_DATA.get("open_source", []):
        lines.append(
            f'- Open source "{os_entry["title"]}": exactly {len(os_entry["bullets"])} bullets, '
            f'combined length <= {_bullets_budget(os_entry["bullets"], shrink)} characters'
        )
    return "\n".join(lines)


def _restore_missing_sections(resume_data: dict) -> None:
    """Deterministic backstop: enforce exactly _PROJECT_PICK_COUNT projects — pad
    from base (in base order) if the LLM dropped too many, truncate if it kept too
    many. Never loses content below the required count; never forces all base
    projects back in.

    open_source is deliberately NOT restored: it is the lowest-signal section and
    the model decides per JD whether it earns its lines. A dropped one stays
    dropped; a kept one is validated like any other entry."""
    # No summary section, ever — the page is tight and the bullets already carry
    # the keywords a summary would repeat. Dropped here so no path can render one.
    resume_data["summary"] = None

    projects = resume_data.setdefault("projects", [])
    titles = {p.get("title", "") for p in projects}
    for base_proj in BASE_RESUME_DATA["projects"]:
        if len(projects) >= _PROJECT_PICK_COUNT:
            break
        if base_proj["title"] not in titles:
            projects.append(copy.deepcopy(base_proj))
            titles.add(base_proj["title"])
    del projects[_PROJECT_PICK_COUNT:]

    # Experience entries render a fixed count too, but WHICH bullets is the
    # model's call. Pad from base order if it returned too few, truncate from the
    # end if too many — it ranks least-relevant last, so the tail is what it
    # nominated to lose.
    base_exp = BASE_RESUME_DATA["experience"]
    for i, exp in enumerate(resume_data.get("experience", [])):
        if i >= len(base_exp):
            continue
        want = _exp_want(base_exp[i]["bullets"])
        bullets = [b for b in exp.get("bullets", []) if (b or "").strip()]
        for base_b in base_exp[i]["bullets"]:
            if len(bullets) >= want:
                break
            if base_b not in bullets:
                bullets.append(base_b)
        exp["bullets"] = bullets[:want]


def _length_violations(resume_data: dict, shrink: float = 1.0) -> list[str]:
    """Compare LLM output against base structure + budgets. Returns human-readable
    violation strings to feed back to the LLM on retry."""
    violations = []

    base_exp = BASE_RESUME_DATA["experience"]
    for i, exp in enumerate(resume_data.get("experience", [])):
        if i >= len(base_exp):
            continue
        want, got = _exp_want(base_exp[i]["bullets"]), len(exp.get("bullets", []))
        if got != want:
            violations.append(
                f'experience "{exp.get("role", i)}" has {got} bullets — must be exactly {want}'
            )
        budget = _bullets_budget(base_exp[i]["bullets"], shrink, want)
        total = sum(len(b) for b in exp.get("bullets", []))
        if total > budget:
            violations.append(
                f'experience "{exp.get("role", i)}" bullets total {total} chars — budget is {budget}, cut {total - budget}+ chars'
            )

    got_projects = len(resume_data.get("projects", []))
    if got_projects != _PROJECT_PICK_COUNT:
        violations.append(
            f'resume has {got_projects} projects — must choose exactly {_PROJECT_PICK_COUNT}'
        )

    base_by_title = {p["title"]: p for p in BASE_RESUME_DATA["projects"]}
    base_by_title.update({p["title"]: p for p in BASE_RESUME_DATA.get("open_source", [])})
    fallback_budget = max(_bullets_budget(p["bullets"], shrink) for p in base_by_title.values())
    for kind in ("projects", "open_source"):
        for entry in resume_data.get(kind, []):
            base = base_by_title.get(entry.get("title", ""))
            want = len(base["bullets"]) if base else 3
            budget = _bullets_budget(base["bullets"], shrink) if base else fallback_budget
            got = len(entry.get("bullets", []))
            if got != want:
                violations.append(
                    f'{kind} "{entry.get("title", "?")}" has {got} bullets — must be exactly {want}'
                )
            total = sum(len(b) for b in entry.get("bullets", []))
            if total > budget:
                violations.append(
                    f'{kind} "{entry.get("title", "?")}" bullets total {total} chars — budget is {budget}, cut {total - budget}+ chars'
                )

    return violations


def _clamp_to_base(resume_data: dict) -> None:
    """Deterministic backstop against LLM inflation: revert an entry to base text
    only when the model wrote the wrong number of bullets or made the entry LONGER
    than the master. Entries that are merely tailored-and-shorter are left alone —
    the master itself runs over one page, so reverting them would make things worse."""
    base_exp = BASE_RESUME_DATA["experience"]
    for i, exp in enumerate(resume_data.get("experience", [])):
        if i >= len(base_exp):
            continue
        base_bullets = base_exp[i]["bullets"]
        want = _exp_want(base_bullets)
        if (len(exp.get("bullets", [])) != want
                or sum(len(b) for b in exp.get("bullets", [])) > _bullets_budget(base_bullets, 1.0, want, apply_fit=False)):
            # Fall back to the first `want` base bullets — base order is the
            # sensible default ranking when the model's own pick is unusable.
            exp["bullets"] = copy.deepcopy(base_bullets[:want])

    base_by_title = {p["title"]: p for p in BASE_RESUME_DATA["projects"]}
    base_by_title.update({p["title"]: p for p in BASE_RESUME_DATA.get("open_source", [])})
    for kind in ("projects", "open_source"):
        for entry in resume_data.get(kind, []):
            base = base_by_title.get(entry.get("title", ""))
            if base is None:
                continue
            if (len(entry.get("bullets", [])) != len(base["bullets"])
                    or sum(len(b) for b in entry.get("bullets", [])) > _bullets_budget(base["bullets"], apply_fit=False)):
                entry["bullets"] = copy.deepcopy(base["bullets"])




# ─────────────────────────────────────────────
#  REORDER-ONLY MODE (verbatim bullets, JD-driven positioning)
#  No bullet text is ever rewritten — only WHICH bullets/projects render and
#  in WHAT ORDER, decided by JD relevance. Base bullet text is fixed and
#  already proven to fit one page, so there is no shrink/expand loop to run.
# ─────────────────────────────────────────────

REORDER_SYSTEM_PROMPT = (
    "You are a senior technical recruiter and hiring manager with 15 years of experience "
    "hiring software engineers at top-tier tech companies. "
    "You are given a candidate's resume content as FIXED, IMMUTABLE facts — every bullet, every "
    "project, every skill is already written and TRUE. Your only job is to decide POSITIONING: "
    "which projects make the cut, which bullets lead vs trail within each entry, which skill "
    "category and which skill inside it goes first — all driven by what THIS specific job "
    "description rewards, so a recruiter skimming for 6 seconds sees the most relevant proof "
    "first. "
    "Rules: "
    "- NEVER rewrite, reword, paraphrase, or alter any bullet text, project title, or skill name. "
    "You are selecting and ordering existing sentences, not writing new ones. Copy them character "
    "for character. "
    "- Rank bullets within each entry by how directly they answer the JD's stated requirements — "
    "the strongest match goes first, the weakest kept goes last. "
    "- Rank skill categories and the items inside them the same way — the JD's headline "
    "requirement should be the first thing a recruiter's eye lands on. "
    "- Choose projects by relevance to the JD, not recency or personal preference. "
    "- The cover letter is the one place you write fresh prose: tell a real story grounded only "
    "in the facts given, in a natural human voice — confident, direct, specific to this company "
    "and role. Never copy or paraphrase resume bullets into it. "
    "- Return only valid JSON matching the provided schema, no markdown, no explanation."
)


def _build_reorder_prompt(jd: str, cl_personalization: str = "") -> str:
    lines = [
        "JOB DESCRIPTION:", jd, "",
        "BASE RESUME CONTENT (verbatim source — you are only ordering/selecting, never rewriting):", "",
        "EXPERIENCE (fixed roles/dates — only bullet selection+order changes):",
    ]
    for i, exp in enumerate(BASE_RESUME_DATA["experience"]):
        want = _exp_want(exp["bullets"])
        lines.append(
            f'Experience [{i}] "{exp["company"]} — {exp["role"]}" ({exp["dates"]}) — '
            f'select and order exactly {want} of {len(exp["bullets"])} bullets by index:'
        )
        lines += [f"  [{j}] {b}" for j, b in enumerate(exp["bullets"])]
        lines.append("")

    lines.append(
        f"PROJECTS — choose exactly {_PROJECT_PICK_COUNT} of {len(BASE_RESUME_DATA['projects'])} by "
        "title, most JD-relevant first; keep ALL bullets of each chosen project, just reorder them:"
    )
    for proj in BASE_RESUME_DATA["projects"]:
        lines.append(f'Project "{proj["title"]}":')
        lines += [f"  [{j}] {b}" for j, b in enumerate(proj["bullets"])]
        lines.append("")

    lines.append(
        "SKILLS — reorder category rows AND items within each category by JD relevance "
        "(same categories/items, no additions, no removals):"
    )
    for cat, csv in BASE_RESUME_DATA["skills"]:
        items = [s.strip() for s in csv.split(",")]
        lines.append(f'Category "{cat}":')
        lines += [f"  [{j}] {s}" for j, s in enumerate(items)]
        lines.append("")

    open_source = BASE_RESUME_DATA.get("open_source") or []
    if open_source:
        os_entry = open_source[0]
        lines.append(f'OPEN SOURCE (optional — include only if this JD gives a reason to) "{os_entry["title"]}":')
        lines += [f"  [{j}] {b}" for j, b in enumerate(os_entry["bullets"])]
        lines.append("")

    cl_block = ""
    if cl_personalization:
        cl_block = (
            "\n\nCOVER LETTER PERSONALIZATION (user-provided — apply IN ADDITION to the cover "
            "letter rules, do not let it override the anti-fabrication / no-em-dash rules):\n"
            f"{cl_personalization}\n"
        )

    lines.append(f"""\
COVER LETTER STYLE SAMPLE (match this voice and confidence level — write entirely fresh content for this JD, do NOT reproduce any phrasing from this sample):
{_CL_STYLE_SAMPLE}

COVER LETTER RULES:
- Write like a human recalling their work in conversation — NOT like someone reading their resume aloud.
- NEVER copy, paraphrase, or restructure resume bullets. If a sentence could appear on the resume, rewrite it.
- Opening: a sharp, specific observation about what makes this company's engineering problem hard or interesting.
- Body: describe a decision made or something found genuinely interesting while building one of the projects/experience above. One concrete number as evidence is fine — a list of metrics is not.
- Closing: one short sentence, natural and direct, naming the specific team or problem area.
- Do NOT start consecutive sentences with "I". Vary sentence structure. 9th-grade reading level.
- NEVER use em dashes or en dashes anywhere in the letter.{cl_block}

SKILL GAPS — this is the ONLY place you may add a word that isn't already in the base content above:
- If the JD explicitly requires or strongly emphasizes a specific technology/skill that is NOT anywhere in the
  skill categories above (check every category first — do not add something already present under a different name),
  you may add it to the most fitting category, but ONLY as an honest gap-fill, never as claimed hands-on experience.
- List at most 3 of these, and only the ones the JD treats as important — do not pad for the sake of it.
- Do NOT add something the candidate could reasonably already cover via an adjacent skill already listed
  (e.g. do not add "SQL" if PostgreSQL is already listed; do not add "Java 21" if Java is already listed).
- These render as plain skill entries — you supply only the bare skill name and its category.

OUTPUT — return ONLY this JSON, no markdown, no explanation:
{{
  "project_order": ["<title>", "<title>", "<title>"],
  "project_bullet_order": [[0,1,2], [0,1,2,3], [0,1,2]],
  "skill_category_order": ["<category label>", "..."],
  "skill_item_order": [[0,1,2], "..."],
  "skill_gaps": [{{"category": "<one of the base category labels above>", "skill": "<bare skill name, no tag>"}}],
  "experience_bullet_order": [[0,1,2,3], [0,1]],
  "include_open_source": true,
  "open_source_bullet_order": [0,1],
  "cover_letter_text": "Dear Hiring Manager, ... (full letter, no signature line)"
}}
Field notes: project_bullet_order is parallel to project_order (one permutation array per chosen project, in that order). skill_item_order is parallel to skill_category_order. experience_bullet_order is parallel to the base EXPERIENCE list order shown above (index 0 = first experience entry), each array is a selection+order of that entry's bullet indices at the required length. open_source_bullet_order is only used when include_open_source is true. skill_gaps may be an empty list — omit entries rather than force 3.""")

    return "\n".join(lines)


_REORDER_SCHEMA = {
    "type": "object",
    "required": [
        "project_order", "project_bullet_order", "skill_category_order",
        "skill_item_order", "experience_bullet_order", "include_open_source",
        "cover_letter_text",
    ],
    "properties": {
        "project_order": {"type": "array", "items": {"type": "string"}},
        "project_bullet_order": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "skill_category_order": {"type": "array", "items": {"type": "string"}},
        "skill_item_order": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "skill_gaps": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object", "required": ["category", "skill"],
                "properties": {"category": {"type": "string"}, "skill": {"type": "string"}},
            },
        },
        "experience_bullet_order": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "include_open_source": {"type": "boolean"},
        "open_source_bullet_order": {"type": "array", "items": {"type": "integer"}},
        "cover_letter_text": {"type": "string"},
    },
}


def _title_key(title: str) -> str:
    """Normalized key for matching an LLM-supplied project title against base
    data. Collapses dash variants and whitespace and drops non-alphanumerics, so
    "NEUROSCRIBE - Foo", "NEUROSCRIBE \u2013 Foo" and "neuroscribe: foo" all match.
    Falls back to the leading name token, which is unique across the projects."""
    norm = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return norm or ""


def _permute(base: list, order) -> list | None:
    """Apply an index permutation to `base`. Returns None (caller falls back to
    base order) if `order` isn't a valid full permutation of base's indices."""
    if not isinstance(order, list) or len(order) != len(base):
        return None
    if sorted(order) != list(range(len(base))):
        return None
    return [base[i] for i in order]


def _select(base: list, order, want: int) -> list:
    """Like _permute but for a SELECTION of `want` items out of `base` (used for
    experience bullets, which pick a subset). Falls back to the first `want`
    base items in base order if `order` is invalid — never fabricates, never fails."""
    if (isinstance(order, list) and len(order) == want and len(set(order)) == want
            and all(isinstance(i, int) and 0 <= i < len(base) for i in order)):
        return [base[i] for i in order]
    return list(base[:want])


def _resolve_reorder(orders: dict) -> dict:
    """Reconstruct full resume_data from BASE_RESUME_DATA using the LLM's ordering
    decisions. Every piece of text is copied verbatim from base — the LLM never
    supplies text here, only indices/titles — with a deterministic fallback to
    base order for any invalid/missing piece, so this step can never fail."""
    data = copy.deepcopy(BASE_RESUME_DATA)

    exp_orders = orders.get("experience_bullet_order") or []
    for i, exp in enumerate(data["experience"]):
        want = _exp_want(exp["bullets"])
        order_i = exp_orders[i] if i < len(exp_orders) else None
        exp["bullets"] = _select(exp["bullets"], order_i, want)

    base_by_title = {p["title"]: p for p in BASE_RESUME_DATA["projects"]}
    titles = orders.get("project_order") or []
    # Match on a normalized key, not the raw string: base titles use an en dash
    # ("NEUROSCRIBE - ...") and models reliably echo them back with an ASCII
    # hyphen, so exact matching silently dropped every pick and fell back to
    # base order — i.e. the same three projects on every JD.
    by_key = {_title_key(t): t for t in base_by_title}
    # Also key on the leading name token ("THROTTLR"), which is unique across the
    # projects, so a model that shortens or retitles still resolves.
    by_name = {}
    for t in base_by_title:
        by_name.setdefault(_title_key(t).split(" ")[0], t)

    def _resolve_title(raw: str) -> str | None:
        key = _title_key(raw)
        if key in by_key:
            return by_key[key]
        head = key.split(" ")[0] if key else ""
        if head in by_name:
            return by_name[head]
        return next((base for name, base in by_name.items() if name and name in key), None)

    picked = []
    for t in titles:
        resolved = _resolve_title(t)
        if resolved and resolved not in picked:
            picked.append(resolved)
        if len(picked) >= _PROJECT_PICK_COUNT:
            break
    picked = picked[:_PROJECT_PICK_COUNT]
    for p in BASE_RESUME_DATA["projects"]:
        if len(picked) >= _PROJECT_PICK_COUNT:
            break
        if p["title"] not in picked:
            picked.append(p["title"])

    proj_bullet_orders = orders.get("project_bullet_order") or []
    new_projects = []
    for idx, title in enumerate(picked):
        proj = copy.deepcopy(base_by_title[title])
        order_i = proj_bullet_orders[idx] if idx < len(proj_bullet_orders) else None
        permuted = _permute(proj["bullets"], order_i)
        proj["bullets"] = permuted if permuted is not None else proj["bullets"]
        new_projects.append(proj)
    data["projects"] = new_projects

    base_skills = {cat: [s.strip() for s in csv.split(",")] for cat, csv in BASE_RESUME_DATA["skills"]}
    cat_order = [c for c in (orders.get("skill_category_order") or []) if c in base_skills]
    for cat, _ in BASE_RESUME_DATA["skills"]:
        if cat not in cat_order:
            cat_order.append(cat)
    item_orders = orders.get("skill_item_order") or []
    cat_index = {c: i for i, (c, _) in enumerate(BASE_RESUME_DATA["skills"])}

    # Honest gap-fill: JD-required skills genuinely absent from base data render
    # render as plain skill entries, indistinguishable from base skills.
    # Capped at 3 (schema maxItems), deduped against every existing skill everywhere.
    all_existing_lower = {s.lower() for items in base_skills.values() for s in items}
    gap_by_cat: dict[str, list[str]] = {}
    seen_gap_skills = set()
    for g in orders.get("skill_gaps") or []:
        if not isinstance(g, dict):
            continue
        skill = str(g.get("skill", "")).strip()
        cat = str(g.get("category", "")).strip()
        if not skill or cat not in base_skills:
            continue
        key = skill.lower()
        if key in all_existing_lower or key in seen_gap_skills:
            continue
        seen_gap_skills.add(key)
        gap_by_cat.setdefault(cat, []).append(skill)

    new_skills = []
    for cat in cat_order:
        items = base_skills[cat]
        src_i = cat_index[cat]
        order_i = item_orders[src_i] if src_i < len(item_orders) else None
        permuted = _permute(items, order_i)
        final_items = list(permuted if permuted is not None else items)
        final_items += gap_by_cat.get(cat, [])
        new_skills.append([cat, ", ".join(final_items)])
    data["skills"] = new_skills

    base_os = BASE_RESUME_DATA.get("open_source") or []
    if base_os and orders.get("include_open_source"):
        os_entry = copy.deepcopy(base_os[0])
        permuted = _permute(os_entry["bullets"], orders.get("open_source_bullet_order"))
        os_entry["bullets"] = permuted if permuted is not None else os_entry["bullets"]
        data["open_source"] = [os_entry]
    else:
        data["open_source"] = []

    data["summary"] = None
    return data


def _check_sponsorship(jd: str) -> tuple[str, str]:
    system = (
        "You are checking whether a job description EXPLICITLY blocks visa sponsorship for an F-1 international student. "
        "Return ONLY valid JSON: {\"sponsorship\": \"yes\" | \"no\" | \"unknown\", \"reason\": \"one short sentence\"}. "
        "'yes' = explicitly offers H-1B or visa sponsorship. "
        "'no' = ONLY if the JD explicitly states: requires US citizenship, requires active security clearance, "
        "states 'no sponsorship available', or states 'must be authorized to work without sponsorship now and in the future'. "
        "'unknown' = anything else, including 'legally authorized to work in the US', 'must be eligible to work in the US', "
        "or any vague work authorization language without explicitly denying sponsorship. "
        "Default to 'unknown' when in doubt. No markdown, no explanation."
    )
    result = call_llm(system, f"Job Description:\n{jd}", schema=None)
    return result.get("sponsorship", "unknown"), result.get("reason", "")


def _extract_company_role(jd: str) -> tuple[str, str]:
    system = (
        "Extract the company name and job role/title from the job description. "
        "Return ONLY valid JSON: {\"company\": \"...\", \"role\": \"...\"}. "
        "No markdown, no explanation."
    )
    result = call_llm(system, f"Job Description:\n{jd}", schema=None)
    return result.get("company", "").strip(), result.get("role", "").strip()


def _cover_letter_from_resume_text(
    jd: str, company: str, role: str, resume_text: str, cl_personalization: str = ""
) -> str:
    """Write a cover letter grounded in an EXISTING resume PDF's text (tag mode).

    Used when the candidate will send a hand-made library resume instead of a
    generated one — the letter must match that exact document, not data.json.
    """
    cl_personalization_block = ""
    if cl_personalization:
        cl_personalization_block = (
            "\n\nCOVER LETTER PERSONALIZATION (user-provided — apply IN ADDITION to the rules "
            "above, do not let it override the anti-fabrication / no-em-dash rules):\n"
            f"{cl_personalization}\n"
        )

    user = f"""\
JOB DESCRIPTION:
{jd}

TARGET: {role or 'the role'} at {company or 'this company'}

THE RESUME BEING SENT (source of truth — this exact document is what the recruiter will read \
alongside your letter; do not reference any experience, project, or metric that is not in it):
{resume_text}

COVER LETTER STYLE SAMPLE (match this voice and confidence level — write entirely fresh content \
for this JD, do NOT reproduce any phrasing from this sample):
{_CL_STYLE_SAMPLE}

RULES:
- Write like a human recalling their work in conversation — NOT like someone reading their resume aloud.
- NEVER copy, paraphrase, or restructure resume bullets. If a sentence could appear on the resume, rewrite it.
- Opening: a sharp, specific observation about what makes this company's engineering problem hard or interesting.
- Body: describe a decision you made or something you found genuinely interesting while building one of the \
projects or systems ON THAT RESUME. Explain the tradeoff or constraint. One concrete number as evidence is fine — \
a list of metrics is not.
- Closing: one line, natural, specific to the team or problem. No "I look forward to hearing from you."
- Do NOT start consecutive sentences with "I". Vary sentence structure. 9th-grade reading level.
- NEVER use em dashes or en dashes anywhere in the letter.
- Do NOT invent experience, employers, degrees, or metrics absent from the resume above.{cl_personalization_block}

OUTPUT: Return ONLY this JSON, no markdown, no explanation:
{{"cover_letter_text": "Dear Hiring Manager, ... (full letter, no signature line)"}}"""

    result = call_llm(SYSTEM_PROMPT, user, schema=None)
    text = (result.get("cover_letter_text") or "").strip()
    if not text:
        raise ValueError("LLM returned an empty cover letter")
    return text


def _build_user_prompt(jd: str, shorten_note: str = "", cl_personalization: str = "", shrink: float = 1.0) -> str:
    base_json = json.dumps(BASE_RESUME_DATA, indent=2)
    schema_json = json.dumps(_OUTPUT_SCHEMA, indent=2)

    shorten_block = ""
    if shorten_note:
        shorten_block = f"\n\nCRITICAL OVERRIDE — {shorten_note}\n"

    cl_personalization_block = ""
    if cl_personalization:
        cl_personalization_block = (
            "\n\nCOVER LETTER PERSONALIZATION (user-provided — apply IN ADDITION to the cover letter "
            "rules above, do not let it override the anti-fabrication / no-em-dash rules):\n"
            f"{cl_personalization}\n"
        )

    return f"""\
JOB DESCRIPTION:
{jd}

BASE RESUME DATA (source of truth — do not fabricate anything not present here):
{base_json}

COVER LETTER STYLE SAMPLE (match this voice and confidence level — write entirely fresh content for this JD, do NOT reproduce any phrasing from this sample):
{_CL_STYLE_SAMPLE}

TAILORING RULES:
1. Resume bullets: think like a senior recruiter who sees 300 resumes a day.
   Write every bullet in XYZ format — "Accomplished [X] as measured by [Y] by doing [Z]".
   - X = the outcome or impact (what improved, what was solved, what was shipped)
   - Y = the concrete metric — use exact numbers where present; conservative rounding/extrapolation allowed
     (e.g. "~50%" → "50%+", "100ms to 7ms" → "14x latency reduction")
   - Z = the action taken (technology, method, design decision)
   - Lead with the result. NEVER start with "Worked on", "Assisted", "Helped", "Participated in".
     Use strong verbs: Engineered, Eliminated, Accelerated, Reduced, Scaled, Shipped, Optimized, Designed.
   - Every bullet must pass the "so what?" test — if removing the metric makes it meaningless, rewrite it.
   - AGGRESSIVE REWRITE: treat base bullets as raw facts (metrics, technologies, outcomes) — NOT as templates to copy.
     Reconstruct every bullet from scratch using the JD's exact language, terminology, and framing.
     If JD says "distributed systems", your bullet says "distributed systems". If JD says "observability platform", yours says "observability platform".
     The final bullet must read like it was written specifically for this job, not pulled from a generic resume.
   - ATS: use the JD's exact tech terms, action verbs, and domain vocabulary throughout — not synonyms.
   - Expand scope aggressively using adjacent technologies that logically follow from the work. Examples:
       RabbitMQ → async messaging, event-driven architecture, message queuing
       Docker Compose → containerization, service orchestration, infrastructure-as-code
       Spring Security + OAuth 2.0 → IAM, auth systems, identity management, zero-trust
       FastAPI → async Python, ASGI, REST API design, Pydantic, Uvicorn
       MQTT + FastAPI → IoT data pipelines, real-time telemetry ingestion, edge computing
       Redis Lua scripts → distributed systems, atomic operations, cache design, concurrency control
       Zipkin + Prometheus → distributed tracing, observability, SRE practices, telemetry
       HLS + FFmpeg → media pipelines, video infrastructure, adaptive bitrate streaming
       Kafka → event streaming, pub/sub, data pipelines, real-time processing
       Kubernetes → container orchestration, cluster management, cloud-native deployment
   - If JD heavily features a technology present anywhere in the resume, surface and expand it — rewrite the bullet to lead with that technology's impact, add it to skills, reference it in the cover letter.
   - EXPERIENCE bullets: do NOT invent new roles, companies, degrees, or metrics with no basis in the resume. Reword and reframe only what actually happened.
   - PROJECT bullets: you MAY freely expand, infer, and embellish — the candidate built these projects end-to-end and knows every technical detail. Add plausible implementation details, architectural decisions, and outcomes that logically follow from the tech stack and stated results. Frame them aggressively for the JD.
   - When JD emphasizes AI/LLM/automation: reframe project bullets using AI vocabulary — personalization engines become "intelligent ranking systems", pipelines become "automated data workflows", rule-based logic becomes "algorithmic decision engines", re-rankers become "ML-style weighted scoring models". Surface the intelligence and automation in the project even if base data doesn't use AI terminology. Make the projects read like AI-adjacent work because they are.
   - PROJECT tech line: rewrite to front-load JD-relevant technologies. If JD emphasizes Java → list Java/Spring Boot first. If JD emphasizes AI/LLMs → surface any AI-adjacent tech. Keep honest to what the project actually used.
   - PROJECT SELECTION: BASE RESUME DATA lists {len(BASE_RESUME_DATA["projects"])} projects — choose exactly the {_PROJECT_PICK_COUNT} most JD-relevant ones and drop the rest entirely (do not include a dropped project at all, not even shortened). Order the {_PROJECT_PICK_COUNT} chosen projects with the highest JD-signal one first.
   - OPEN SOURCE is OPTIONAL and is your call: include the open_source entry ONLY when this JD gives you a reason to (it values open source contribution, or its stack overlaps the entry's). Otherwise omit the key entirely — an irrelevant section costs 4 lines that a stronger project bullet could use. When you do include it, keep both of its bullets.
   - BULLET COUNT: use exactly the count given for each entry in the HARD LENGTH BUDGETS below — never more, never fewer.
     - EXPERIENCE entries capped below their base count (see the budgets) are a SELECTION task: read the JD, decide which of that entry's base bullets it actually rewards, keep exactly that many, and drop the rest entirely. Do not compress all of them into fewer bullets — pick and discard.
     - Projects and open_source keep their base bullet count exactly. Do not add or drop bullets there.
   - BULLET ORDER — applies to EVERY entry, experience and projects alike: rank the bullets you keep by how directly they answer THIS JD and emit them highest-signal FIRST, weakest LAST. A recruiter reads the first bullet of each entry and skims the rest, so the bullet matching the JD's headline requirement must be bullet 1. Never keep base order out of habit.
2. Skills: this section renders near the TOP of the resume (right after Education) for a 6-second recruiter skim — ordering matters more than usual.
   - Reorder within each category to front-load JD-relevant items.
   - ALSO reorder the category rows themselves so the single category most relevant to THIS JD is FIRST (e.g. a frontend-heavy JD → Frontend category first; an infra/SRE JD → DevOps & Observability first; an AI/agent JD → AI Tooling first). Same categories as base data, just reordered — never invent a new category.
   You MAY freely add skills required or preferred by the JD — the candidate is a fast learner and capable of any skill listed.
   You MAY also surface skills implied by the tech stack (e.g. if a bullet mentions Kubernetes → add container orchestration; Spring Boot → add Java).
   If JD requires frontend skills (React, HTML, CSS, JavaScript, TypeScript) — add them. If JD requires AI/LLM skills — add them. Match the JD's skill vocabulary exactly.
3. Summary field: ALWAYS null. This resume has no summary section. A summary only restates what the bullets already prove and costs 3-4 lines of a one-page budget better spent on a real accomplishment. Any summary you write is discarded, so do not write one.
4. Cover letter: write like a human recalling their work in conversation — NOT like someone reading their resume aloud.
   - NEVER copy, paraphrase, or restructure resume bullets into the cover letter. If a sentence could appear on the resume, rewrite it.
   - Opening: lead with a sharp, specific observation about what makes this company's engineering problem hard or interesting.
     Show you actually understand the domain — not "I want to join your team" but "here is why this problem matters and why it's non-trivial."
   - Body: describe a decision you made or a thing you found genuinely interesting while building something. Explain the tradeoff,
     the constraint, or the moment where it clicked. One concrete number used as evidence in a sentence is fine —
     a list of metrics from the resume is not. Show taste and judgment, not a portfolio readout.
   - Closing: one line, natural, specific to the team or problem. No "I look forward to hearing from you."
   - Tone: confident without being arrogant. Direct without being cold. Like you're talking to a senior engineer you respect.
   - Do NOT start consecutive sentences with "I". Vary sentence structure. Write at a 9th-grade reading level — simple words, clear ideas.
5. Resume MUST fit on exactly 1 page — if content overflows, tighten the Z clause (method description) first, never cut X (impact) or Y (metric).
   NEVER drop Education, Skills, Experience, or Projects, and NEVER drop a project below the required {_PROJECT_PICK_COUNT} — obey the HARD LENGTH BUDGETS below instead. Open source is the one section you may omit, per the rule above.

{_budget_block(shrink)}{shorten_block}{cl_personalization_block}
OUTPUT: Return ONLY the following JSON schema, no markdown, no explanation:
{schema_json}"""


# ─────────────────────────────────────────────
#  CORE JOB PROCESSOR
# ─────────────────────────────────────────────

def _process_job(job_id: int, job_link: str, jd: str, company: str, role: str, cl_personalization: str = "", mode: str = "both", force: bool = False) -> tuple[str, str]:
    # Step 1: Extract company/role if blank
    if not company or not role:
        print(f"  [job {job_id}] Extracting company/role from JD...")
        extracted_company, extracted_role = _extract_company_role(jd)
        company = company or extracted_company
        role    = role    or extracted_role
        print(f"  [job {job_id}] → {company} / {role}")

    # Step 1b: Visa sponsorship gate — fail fast before spending LLM tokens
    print(f"  [job {job_id}] Checking visa sponsorship...")
    sponsorship, reason = _check_sponsorship(jd)
    if sponsorship == "no" and not force:
        raise RuntimeError(f"Rejected: Company does not offer visa sponsorship. {reason} (use --force to generate anyway)")
    if sponsorship == "no" and force:
        print(f"  [job {job_id}] Sponsorship: no — {reason or 'no issues'} — forced, continuing anyway")
    else:
        print(f"  [job {job_id}] Sponsorship: {sponsorship} — {reason or 'no issues'}")

    # Step 2: Build output directory
    safe_company = _sanitize(company)
    safe_role    = _sanitize(role)
    out_dir      = Path("outputs") / safe_company / safe_role
    out_dir.mkdir(parents=True, exist_ok=True)

    resume_path = str(out_dir / "Resume.pdf")
    cl_path     = str(out_dir / "Cover_Letter.pdf")

    if mode == "tag":
        # Tag mode: no resume is generated. The best-fitting resume already in the
        # library is picked, and the cover letter is written against THAT document
        # so the pair the recruiter receives is consistent.
        print(f"  [job {job_id}] Tagging best resume version...")
        tag = tag_resume_version(jd, company, role)
        report = format_report(tag)
        print("  " + report.replace("\n", "\n  "))
        save_tag(job_id, tag["best_version"], tag["rating"], report)

        if not tag.get("path"):
            raise RuntimeError(
                f"Tagged version '{tag['best_version']}' did not resolve to a file — "
                "cannot write a cover letter against it."
            )

        tagged_pages = _page_count(tag["path"])
        if tagged_pages > 1:
            # The winner is a multi-page document (the Master resume is 2 pages).
            # It can't be uploaded as-is, so fall through to normal generation and
            # produce a tailored 1-page resume for this JD instead. The tag stays
            # saved — it still says which angle this JD wants.
            print(
                f"  [job {job_id}] Tagged version '{tag['best_version']}' is {tagged_pages} pages — "
                "not uploadable. Generating a tailored 1-page resume instead."
            )
            save_tag(
                job_id,
                tag["best_version"],
                tag["rating"],
                report + (
                    f"\n\nNOTE: '{tag['best_version']}' is {tagged_pages} pages, so it was not used. "
                    "A tailored 1-page resume was generated for this JD instead — send that one."
                ),
            )
            mode = "both"
        else:
            print(f"  [job {job_id}] Writing cover letter against the {tag['best_version']} resume...")
            cl_text = _cover_letter_from_resume_text(
                jd, company, role, extract_resume_text(Path(tag["path"])), cl_personalization
            )
            build_cover_letter(cl_text, company, role, cl_path)
            print(f"  [job {job_id}] Done → {tag['best_version']} ({tag['rating']}/10), CL at {cl_path}")
            return company, role

    if mode == "cover_letter":
        print(f"  [job {job_id}] Cover-letter-only mode — single LLM call...")
        result = call_llm(SYSTEM_PROMPT, _build_user_prompt(jd, cl_personalization=cl_personalization))
        print(f"  [job {job_id}] Building cover letter...")
        build_cover_letter(result["cover_letter_text"], company, role, cl_path)
        print(f"  [job {job_id}] Done → {out_dir}")
        return company, role

    # Step 3: reorder-only LLM call — bullets/titles come verbatim from
    # BASE_RESUME_DATA, the LLM only picks projects/positions (see
    # REORDER_SYSTEM_PROMPT). No rewording means no shrink/expand loop: base
    # text is already proven to fit one page at DEFAULT_LEADING.
    print(f"  [job {job_id}] LLM call (reorder + cover letter)...")
    orders = call_llm(REORDER_SYSTEM_PROMPT, _build_reorder_prompt(jd, cl_personalization), schema=_REORDER_SCHEMA)
    resume_data = _resolve_reorder(orders)
    resume_data["name"]    = BASE_RESUME_DATA["name"]
    resume_data["contact"] = BASE_RESUME_DATA["contact"]

    # Step 4: Build resume PDF
    leading = DEFAULT_LEADING
    build_resume(resume_data, resume_path, leading=leading)

    def _squeeze_leading(current: float) -> float:
        while _page_count(resume_path) > 1 and current > 10.0:
            current = round(current - 0.25, 2)
            print(f"  [job {job_id}] Squeezing leading to {current}")
            build_resume(resume_data, resume_path, leading=current)
        return current

    # Step 5: 1-page enforcement — only leading squeeze + open_source drop are
    # ever needed now (calibration confirmed every 3-of-5 project combo except
    # the AEROFEED-heavy ones fits at DEFAULT_LEADING; that one needs the drop).
    leading = _squeeze_leading(leading)

    if _page_count(resume_path) > 1 and resume_data.get("open_source"):
        print(f"  [job {job_id}] Still over 1 page — dropping Open Source section")
        resume_data["open_source"] = []
        build_resume(resume_data, resume_path, leading=leading)
        leading = _squeeze_leading(leading)

    pages = _page_count(resume_path)
    if pages > 1:
        raise RuntimeError(
            f"Resume still {pages} page(s) after leading squeeze and dropping Open Source. "
            "Manual review needed."
        )

    fill = _fill_ratio(resume_path)
    print(f"  [job {job_id}] Page fill: {fill:.0%} — bullets are verbatim base text, not expandable")

    result = {"resume_data": resume_data, "cover_letter_text": orders["cover_letter_text"]}

    # Step 6: Build cover letter PDF
    print(f"  [job {job_id}] Building cover letter...")
    build_cover_letter(result["cover_letter_text"], company, role, cl_path)

    # Step 7: Keyword coverage analysis
    print(f"  [job {job_id}] Analyzing keyword coverage...")
    kw_results = analyze_keyword_coverage(jd, result["resume_data"], result["cover_letter_text"])
    bullet_results = analyze_resume_bullets(result["resume_data"])
    print_analysis_report(kw_results, bullet_results)

    print(f"  [job {job_id}] Done → {out_dir}")
    return company, role


# ─────────────────────────────────────────────
#  PIPELINE ENTRYPOINT
# ─────────────────────────────────────────────

def run_pipeline(mode: str = "both", force: bool = False):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, job_link, jd, company, role, cl_personalization FROM jobs WHERE status = 'pending'"
    ).fetchall()

    if not rows:
        print("No pending jobs.")
        conn.close()
        return

    print(f"Found {len(rows)} pending job(s).")

    for row in rows:
        job_id, job_link, jd, company, role, cl_personalization = row
        print(f"\nProcessing job {job_id}...")
        try:
            final_company, final_role = _process_job(job_id, job_link, jd, company or "", role or "", cl_personalization or "", mode, force)
            conn.execute(
                "UPDATE jobs SET status = 'done', company = ?, role = ?, error = NULL WHERE id = ?",
                (final_company, final_role, job_id),
            )
        except Exception:
            err = traceback.format_exc()
            print(f"  [job {job_id}] FAILED:\n{err}")
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                (err, job_id),
            )
        conn.commit()

    conn.close()
    print("\nPipeline complete.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run the job application pipeline.")
    parser.add_argument(
        "--mode", choices=["both", "cover_letter", "tag"], default="both",
        help="'both' (default) generates resume + cover letter; 'cover_letter' skips the resume "
             "entirely; 'tag' generates nothing and instead picks the best existing resume version "
             "from the library with a fit rating out of 10",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Generate even if the visa sponsorship gate rejects the job",
    )
    args = parser.parse_args()
    run_pipeline(args.mode, args.force)
