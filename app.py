"""
Streamlit UI for the job application pipeline.
Run: streamlit run app.py
"""

import re
import sqlite3
import traceback
from pathlib import Path

import streamlit as st

import config
import llm
from db_setup import DB_PATH, add_job, init_db

# ── page config ───────────────────────────────
st.set_page_config(
    page_title="Job Pipeline",
    page_icon="📄",
    layout="wide",
)

init_db()

st.markdown("<style>#MainMenu {visibility: hidden;}</style>", unsafe_allow_html=True)

# ── session state defaults ────────────────────
for key, default in [
    ("editing_job", None),
    ("confirm_delete", None),
    ("confirm_delete_all", False),
    ("select_mode", False),
    ("pipeline_log", []),
    ("_add_job_success", False),
    ("health", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ═══════════════════════════════════════════════
#  DB HELPERS
# ═══════════════════════════════════════════════

def _connect():
    return sqlite3.connect(DB_PATH)


def _fetch_jobs(
    status_filter: str = "All",
    search: str = "",
    exclude_archived: bool = False,
    exclude_important: bool = False,
) -> list:
    conn = _connect()
    query = "SELECT id, company, role, status, job_link, error, tag_version, tag_rating, tag_report FROM jobs"
    params = []
    conditions = []
    if status_filter != "All":
        conditions.append("status = ?")
        params.append(status_filter.lower())
    else:
        if exclude_archived:
            conditions.append("status != 'archived'")
        # Important jobs live on their own page, so the general Jobs list
        # doesn't repeat them.
        if exclude_important:
            conditions.append("status != 'important'")
    if search:
        conditions.append("(company LIKE ? OR role LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def _fetch_job(job_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT id, company, role, status, job_link, jd, error, cl_personalization FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return dict(zip(["id", "company", "role", "status", "job_link", "jd", "error", "cl_personalization"], row))


def _update_job(job_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = _connect()
    conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", [*fields.values(), job_id])
    conn.commit()
    conn.close()


def _delete_job(job_id: int):
    conn = _connect()
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()


def _delete_all_jobs():
    conn = _connect()
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()


def _retry_all_failed():
    conn = _connect()
    conn.execute("UPDATE jobs SET status = 'pending', error = NULL WHERE status = 'failed'")
    conn.commit()
    conn.close()


def _redo_jobs(ids: list[int]):
    conn = _connect()
    for jid in ids:
        conn.execute("UPDATE jobs SET status = 'pending', error = NULL WHERE id = ?", (jid,))
    conn.commit()
    conn.close()


def _counts() -> dict:
    conn = _connect()
    rows = conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    conn.close()
    result = {"pending": 0, "done": 0, "failed": 0, "important": 0, "archived": 0}
    for status, count in rows:
        result[status or "pending"] = count
    return result


def _archive_done_jobs():
    conn = _connect()
    conn.execute("UPDATE jobs SET status = 'archived' WHERE status = 'done'")
    conn.commit()
    conn.close()


def _safe(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", name or "").strip().replace(" ", "_") or "Unknown"


def _output_paths(company: str, role: str):
    out = Path("outputs") / _safe(company) / _safe(role)
    return out / "Resume.pdf", out / "Cover_Letter.pdf"


# Every resume download uses one neutral name — a recruiter should never see
# "SRE_Platform" or a company name in the file they receive.
DOWNLOAD_RESUME_NAME = "Barath_Suresh_Master_Resume.pdf"


def _next_free_path(out_dir: Path, file_name: str) -> Path:
    """First name in out_dir that isn't taken — name.pdf, name (1).pdf, ..."""
    candidate = out_dir / file_name
    stem, suffix = candidate.stem, candidate.suffix
    n = 1
    while candidate.exists():
        candidate = out_dir / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def _save_button(label: str, data: bytes, file_name: str, key: str):
    """Save straight to config.DOWNLOAD_DIR instead of a browser save dialog —
    lets the folder be configured once in the sidebar and reused everywhere.
    A same-named file is replaced or kept depending on the sidebar's
    "Overwrite existing files" setting."""
    if st.button(label, key=key):
        try:
            out_dir = Path(config.DOWNLOAD_DIR).expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            if config.OVERWRITE_DOWNLOADS:
                out_path = out_dir / file_name
                existed = out_path.exists()
                out_path.write_bytes(data)
                st.toast(f"{'Replaced' if existed else 'Saved'} {out_path}", icon="✅")
            else:
                out_path = _next_free_path(out_dir, file_name)
                out_path.write_bytes(data)
                if out_path.name != file_name:
                    st.toast(f"{file_name} existed — saved as {out_path.name}", icon="✅")
                else:
                    st.toast(f"Saved to {out_path}", icon="✅")
        except OSError as e:
            st.error(f"Couldn't save to {config.DOWNLOAD_DIR}: {e}")


def _rating_str(rating) -> str:
    return f"{rating:g}/10" if rating is not None else "unrated"


def _tagged_file_path(report: str | None) -> Path | None:
    """Pull the tagged resume's path out of a saved tag report."""
    if not report:
        return None
    m = re.search(r"^File:\s*(.+)$", report, re.MULTILINE)
    if not m or m.group(1).strip() == "(unresolved)":
        return None
    return Path(m.group(1).strip())


# ═══════════════════════════════════════════════
#  UI COMPONENTS
# ═══════════════════════════════════════════════

STATUS_BADGE = {"pending": "🟡", "done": "🟢", "failed": "🔴", "important": "⭐", "archived": "🗄️"}
STATUS_LABEL = {"pending": "Pending", "done": "Done", "failed": "Failed", "important": "Important", "archived": "Archived"}


def _status_badge(status: str) -> str:
    s = status or "pending"
    return f"{STATUS_BADGE.get(s, '⚪')} {STATUS_LABEL.get(s, s.title())}"


def _job_card(row: tuple, expanded: bool = False):
    job_id, company, role, status, link, error, tag_version, tag_rating, tag_report = row
    status = status or "pending"
    badge  = _status_badge(status)
    title  = f"{badge} &nbsp; **#{job_id}** — {company or 'TBD'} / {role or 'TBD'}"
    if tag_version:
        title += f" &nbsp; `🏷️ {tag_version} {_rating_str(tag_rating)}`"

    with st.expander(title, expanded=expanded):
        # ── edit mode ──
        if st.session_state.editing_job == job_id:
            _edit_form(job_id)
            return

        # ── view mode ──
        if link:
            st.markdown(f"🔗 [{link}]({link})")

        # JD viewer (lazy — jd isn't in the list query, fetched only when opened)
        if st.toggle("📄 View JD", key=f"show_jd_{job_id}"):
            jd_text = (_fetch_job(job_id) or {}).get("jd") or ""
            if jd_text.strip():
                st.text_area(
                    "Job description",
                    value=jd_text,
                    height=400,
                    disabled=True,
                    key=f"jd_view_{job_id}",
                    label_visibility="collapsed",
                )
            else:
                st.info("No job description saved for this job.")

        col_edit, col_del, col_spacer = st.columns([1, 1, 6])

        if col_edit.button("✏️ Edit", key=f"edit_{job_id}"):
            st.session_state.editing_job = job_id
            st.rerun()

        # Delete with confirm
        if st.session_state.confirm_delete == job_id:
            st.warning("Delete this job permanently?")
            c1, c2, _ = st.columns([1, 1, 6])
            if c1.button("Yes, delete", key=f"confirm_yes_{job_id}", type="primary"):
                _delete_job(job_id)
                st.session_state.confirm_delete = None
                st.rerun()
            if c2.button("Cancel", key=f"confirm_no_{job_id}"):
                st.session_state.confirm_delete = None
                st.rerun()
        else:
            if col_del.button("🗑️ Delete", key=f"del_{job_id}"):
                st.session_state.confirm_delete = job_id
                st.rerun()

        # Retry
        if status == "failed":
            if st.button("↩️ Retry", key=f"retry_{job_id}"):
                _update_job(job_id, status="pending", error=None)
                st.rerun()

        # Mark / Unmark important
        if status == "important":
            if st.button("↩️ Unmark Important", key=f"unimportant_{job_id}"):
                _update_job(job_id, status="pending")
                st.rerun()
        elif status != "archived":
            if st.button("⭐ Mark Important", key=f"important_{job_id}"):
                _update_job(job_id, status="important")
                st.rerun()

        # Archive / Unarchive
        if status in ("done", "important"):
            if st.button("🗄️ Archive", key=f"archive_{job_id}"):
                _update_job(job_id, status="archived")
                st.rerun()
        if status == "archived":
            if st.button("↩️ Unarchive", key=f"unarchive_{job_id}"):
                _update_job(job_id, status="done")
                st.rerun()

        # Download PDFs
        if status in ("done", "important", "archived") and company and role:
            res_path, cl_path = _output_paths(company, role)
            d1, d2, _ = st.columns([1.5, 1.5, 5])
            if res_path.exists():
                with d1:
                    _save_button(
                        "⬇️ Resume", res_path.read_bytes(),
                        DOWNLOAD_RESUME_NAME, key=f"dl_res_{job_id}",
                    )
            if cl_path.exists():
                with d2:
                    _save_button(
                        "⬇️ Cover Letter", cl_path.read_bytes(),
                        f"{_safe(company)}_CoverLetter.pdf", key=f"dl_cl_{job_id}",
                    )

        # Resume version tag
        st.divider()
        tc1, tc2 = st.columns([2, 6])
        if tc1.button("🏷️ Tag only (no letter)", key=f"tag_{job_id}"):
            from resume_tagger import format_report, tag_resume_version
            from db_setup import save_tag

            job = _fetch_job(job_id)
            tagged_ok = False
            with st.spinner("Matching against your resume library..."):
                try:
                    tag = tag_resume_version(job.get("jd", ""), job.get("company", ""), job.get("role", ""))
                    save_tag(job_id, tag["best_version"], tag["rating"], format_report(tag))
                    tagged_ok = True
                except Exception as e:
                    st.error(f"Tagging failed: {type(e).__name__}: {e}")
            if tagged_ok:
                st.rerun()

        if tag_version:
            tc2.markdown(f"**Best fit: {tag_version} — {_rating_str(tag_rating)}**")
            if tag_report:
                st.code(tag_report)
            tagged_path = _tagged_file_path(tag_report)
            if tagged_path and tagged_path.exists():
                _save_button(
                    f"⬇️ {tag_version} resume", tagged_path.read_bytes(),
                    DOWNLOAD_RESUME_NAME, key=f"dl_tag_{job_id}",
                )

        # Error
        if status == "failed" and error:
            with st.expander("Error details"):
                st.code(error, language="python")

        # Ask anything about this job
        st.divider()
        st.markdown("##### 💬 Ask about this job")
        question = st.text_input(
            "Question",
            key=f"ask_q_{job_id}",
            placeholder="e.g. How should I answer 'why this company'? What will they likely ask me?",
            label_visibility="collapsed",
        )
        draft = st.text_area(
            "Your rough answer (optional)",
            key=f"ask_draft_{job_id}",
            placeholder=(
                "Optional. Jot down your own answer in any form, bullet points, half sentences, "
                "whatever. It gets expanded into a spoken answer using your points.\n\n"
                "Leave blank and one gets written from scratch."
            ),
            height=110,
        )

        btn_label = "Expand my answer" if draft.strip() else "Ask"
        if st.button(btn_label, key=f"ask_btn_{job_id}", type="primary"):
            if question.strip():
                job = _fetch_job(job_id)
                spinner_msg = "Expanding..." if draft.strip() else "Thinking..."
                with st.spinner(spinner_msg):
                    answer = _ask_job_question(
                        question.strip(),
                        job.get("jd", ""),
                        job.get("company", ""),
                        job.get("role", ""),
                        draft.strip(),
                    )
                st.session_state[f"ask_ans_{job_id}"] = answer

        if st.session_state.get(f"ask_ans_{job_id}"):
            st.markdown(st.session_state[f"ask_ans_{job_id}"])


def _strip_dashes(text: str) -> str:
    """Models leak em/en dashes no matter what the prompt says. Enforce it here."""
    # A tight en dash between alphanumerics is a range (144p–1080p, 9.8–109.5), not a
    # pause. Those become plain hyphens; everything else becomes a comma.
    text = re.sub(r"(?<=[\w.])–(?=[\w.])", "-", text)
    # "word — word" / "word—word" become a comma pause, which is how speech reads.
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    # Collapse damage from a dash that already followed punctuation.
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"([,.;:!?])\s*,\s*", r"\1 ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# Questions about the JOB, not about me. These ask what the role demands; the
# war-story persona can only answer them as personal narrative, which is the
# wrong artefact entirely. Checked FIRST, because "what skills..." reads as an
# inventory question until you notice it is asking about the posting.
# ─────────────────────────────────────────────
#  GENERAL Q&A
#  One prompt, any question. The shape of the answer follows the question that
#  was asked: a list question gets a list, a question about the posting gets an
#  answer about the posting, an experience question gets the experience. Earlier
#  versions forced every question through a single "tell a war story" persona,
#  which answered "what skills does this job require" with two anecdotes.
# ─────────────────────────────────────────────

_QA_SYSTEM = (
    "You are helping {name} answer a question while applying for a job. Answer the question that "
    "was actually asked, in whatever form that question calls for.\n\n"
    "MATCH THE QUESTION:\n"
    "- Asked what the JOB requires, or to summarise the posting: answer about the job, from the job "
    "description. Not about {first}'s experience.\n"
    "- Asked to LIST skills, languages, or technologies: give the coverage, grouped by area, naming "
    "the specific technologies. Do not narrow to one thing.\n"
    "- Asked about EXPERIENCE, a project, or how something was handled: answer with the specific "
    "work, what was done and what it changed, including the real number.\n"
    "- Asked an opinion or preference question: answer it directly.\n"
    "Length follows the question. A yes/no takes a sentence or two. A list question takes as long "
    "as the list. Do not pad and do not force everything to one shape.\n\n"
    "REGISTER: neutral and professional. First person where the question is about {first}, "
    "otherwise plain analysis. Direct declarative sentences. Contractions are fine.\n"
    "- No conversational openers: 'So,', 'Honestly,', 'I mean,', 'Well,', 'Look,'.\n"
    "- No filler: 'kind of', 'pretty much', 'a lot of', 'stuff', 'things like', \"y'know\", "
    "'I guess', 'sort of', 'basically', 'managed to', 'really just'.\n"
    "- No em dashes or en dashes anywhere. Use commas, periods, or 'and'.\n"
    "- Exact numbers, never spoken rounding. '100ms to 7ms', not 'about 100ms'.\n"
    "- Plain text only. No markdown, no bullet characters, no headings.\n\n"
    "TRUTH:\n"
    "- Every fact about {first} comes from the resume below. Never invent a project, a number, an "
    "employer, or a technology that is not there.\n"
    "- Every fact about the role comes from the job description. If it does not say something, say "
    "it is unstated rather than guessing.\n"
    "- Be honest about depth. Distinguish strengths from working familiarity. Never inflate.\n\n"
    "DO NOT:\n"
    "- Flatter the company, echo its mission, or praise its culture.\n"
    "- End on a line about yourself: 'that is the kind of work I enjoy', 'I would bring that same "
    "approach here', anything with 'I love', 'passionate', 'excited'. Stop when the answer is done.\n\n"
    "Resume, use as the source of fact about {first}, never quote it verbatim:\n{resume}"
)

def _ask_job_question(question: str, jd: str, company: str, role: str, draft: str = "") -> str:
    import json
    from resume_builder import BASE_RESUME_DATA
    from llm import _call_provider

    resume_summary = json.dumps(BASE_RESUME_DATA, indent=2)
    system = _QA_SYSTEM.format(
        name=BASE_RESUME_DATA["name"],
        first=BASE_RESUME_DATA["name"].split()[0],
        resume=resume_summary,
    )

    if draft:
        system += (
            "\n\nMODE: EXPAND MY DRAFT.\n"
            "I have written my own rough answer below. Your job is to write it up cleanly, "
            "NOT to write a better answer of your own.\n"
            "- Keep MY points, MY examples, MY opinions, and MY ordering. Every idea in the final answer "
            "must trace back to something I wrote.\n"
            "- Do NOT swap in a different project or story because you think it fits the JD better. "
            "If I picked Geolink, the answer is about Geolink.\n"
            "- Do NOT add new claims, numbers, or accomplishments I did not mention. You may pull an exact "
            "figure from my resume ONLY if I referred to that same thing vaguely ('sped it up a lot').\n"
            "- If my draft is terse or fragmentary, that is fine. Turn fragments into full sentences. "
            "Do not pad to reach a length.\n"
            "- If I wrote something awkward but true, keep the substance and smooth only the delivery.\n"
            "- Everything above about register, banned filler, and how to end still applies.\n"
        )
    user = (
        f"Company: {company or 'unknown'}\n"
        f"Role: {role or 'unknown'}\n"
        f"Job Description:\n{jd[:6000] if jd else 'not provided'}\n\n"
        f"Question: {question}"
    )
    if draft:
        user += (
            f"\n\nMy rough answer, expand this and do not replace it:\n{draft}"
        )
    try:
        return _strip_dashes(_call_provider(system, user))
    except Exception as e:
        import traceback
        return f"**Error:** {e}\n\n```\n{traceback.format_exc()}\n```"


def _edit_form(job_id: int):
    job = _fetch_job(job_id)
    if not job:
        st.error("Job not found.")
        st.session_state.editing_job = None
        return

    st.markdown("#### Edit Job")
    with st.form(f"edit_form_{job_id}"):
        new_link    = st.text_input("Job Link",  value=job["job_link"] or "")
        c1, c2      = st.columns(2)
        new_company = c1.text_input("Company",   value=job["company"] or "")
        new_role    = c2.text_input("Role",      value=job["role"] or "")
        new_jd      = st.text_area("Job Description", value=job["jd"] or "", height=300)
        new_cl_personalization = st.text_area(
            "Cover Letter Personalization",
            value=job.get("cl_personalization") or "",
            height=120,
            placeholder="Optional — extra instructions/context for the cover letter (leave blank for default style).",
        )
        _status_opts = ["pending", "done", "failed", "important", "archived"]
        new_status  = st.selectbox(
            "Status",
            _status_opts,
            index=_status_opts.index(job["status"] if job["status"] in _status_opts else "pending"),
        )

        s1, s2 = st.columns([1, 1])
        save    = s1.form_submit_button("💾 Save", type="primary", use_container_width=True)
        cancel  = s2.form_submit_button("Cancel", use_container_width=True)

    if save:
        _update_job(
            job_id,
            job_link=new_link.strip(),
            company=new_company.strip(),
            role=new_role.strip(),
            jd=new_jd.strip(),
            cl_personalization=new_cl_personalization.strip(),
            status=new_status,
        )
        st.session_state.editing_job = None
        st.success("Saved.")
        st.rerun()

    if cancel:
        st.session_state.editing_job = None
        st.rerun()


# ═══════════════════════════════════════════════
#  LLM HELPERS
# ═══════════════════════════════════════════════

@st.cache_data(ttl=600, show_spinner=False)
def _provider_models(provider: str) -> tuple[list[str], str]:
    """(models, error). Cached so every rerun doesn't hit the provider."""
    try:
        return llm.list_models(provider), ""
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"[:200]


# ═══════════════════════════════════════════════
#  SIDEBAR + ROUTING
# ═══════════════════════════════════════════════

with st.sidebar:
    st.title("📄 Job Pipeline")
    st.divider()
    counts = _counts()
    st.metric("🟡 Pending",   counts["pending"])
    st.metric("🟢 Done",      counts["done"])
    st.metric("🔴 Failed",    counts["failed"])
    st.metric("⭐ Important", counts["important"])
    st.metric("🗄️ Archived",  counts["archived"])
    st.divider()
    page = st.radio(
        "Navigate",
        ["🏠 Dashboard", "⚡ Quick Apply", "➕ Add Job", "📋 Jobs", "⭐ Important", "▶️ Run Pipeline", "🗄️ Archive"],
        label_visibility="collapsed",
    )

    st.divider()

    # ── Download location ─────────────────────
    with st.expander("📁 Download Location", expanded=False):
        new_dir = st.text_input(
            "Save PDFs to",
            value=config.DOWNLOAD_DIR,
            key="download_dir_input",
        )
        if new_dir != config.DOWNLOAD_DIR:
            config.set_download_dir(new_dir)
            st.caption("Saved.")
        st.caption(f"Currently: `{config.DOWNLOAD_DIR}`")

        overwrite = st.toggle(
            "Overwrite existing files",
            value=config.OVERWRITE_DOWNLOADS,
            key="overwrite_downloads_toggle",
            help="On: a same-named PDF in that folder is replaced. "
                 "Off: it's kept and the new one saves as \"name (1).pdf\".",
        )
        if overwrite != config.OVERWRITE_DOWNLOADS:
            config.set_overwrite_downloads(overwrite)
            st.rerun()

    st.divider()

    # ── Education ──────────────────────────────
    with st.expander("🎓 Education", expanded=False):
        import resume_builder

        _MONTHS = ["January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]

        edu = resume_builder.BASE_RESUME_DATA["education"][0]
        st.caption(edu["school"])
        start_str, end_str = [s.strip() for s in edu["dates"].split("–")]
        end_month, end_year = end_str.rsplit(" ", 1)

        c1, c2 = st.columns(2)
        new_month = c1.selectbox(
            "Graduation month", _MONTHS,
            index=_MONTHS.index(end_month) if end_month in _MONTHS else 0,
            key="grad_month",
        )
        new_year = c2.number_input(
            "Graduation year", value=int(end_year), step=1, key="grad_year",
        )

        new_dates = f"{start_str} – {new_month} {new_year}"
        if new_dates != edu["dates"]:
            resume_builder.set_education_dates(0, new_dates)
            st.caption("Saved.")
        st.caption(f"Currently: `{edu['dates']}`")

    st.divider()

    # ── LLM provider ──────────────────────────
    with st.expander("🤖 LLM Provider", expanded=False):
        provider = st.radio(
            "Provider",
            list(config.PROVIDERS),
            index=list(config.PROVIDERS).index(config.PROVIDER),
            key="llm_provider",
            horizontal=True,
        )
        saved_model = config.MODELS[provider]
        options, list_err = _provider_models(provider)

        if options:
            # Keep the saved model selectable even if the provider stopped
            # listing it, so a switch never silently rewrites the choice.
            if saved_model not in options:
                options = [saved_model] + options
            model_name = st.selectbox(
                "Model",
                options,
                index=options.index(saved_model),
                key=f"llm_model_{provider}",
            )
        else:
            st.warning(f"Model list unavailable — {list_err}")
            model_name = st.text_input(
                "Model",
                value=saved_model,
                key=f"llm_model_txt_{provider}",
            )

        if st.button("🔄 Refresh models", use_container_width=True, key="refresh_models_btn"):
            _provider_models.clear()
            st.rerun()

        if provider != config.PROVIDER or model_name != config.MODELS[provider]:
            config.set_provider(provider, model_name)
            st.caption("Saved.")

        st.caption(f"Key: `{config.env_var(provider)}`")

        if st.button("🩺 Health Check", use_container_width=True, key="health_btn"):
            with st.spinner(f"Pinging {provider}..."):
                st.session_state.health = llm.health_check(provider, model_name)

        report = st.session_state.get("health")
        if report:
            latency = report["latency_s"]
            if report["ok"]:
                st.success(f"✅ {report['provider']} / {report['model']} — {latency}s")
            else:
                st.error(f"❌ {report['provider']} / {report['model']} — failed after {latency}s")
                st.code(report["detail"], language=None)


# ═══════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════
if page == "🏠 Dashboard":
    st.header("Dashboard")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("🟡 Pending",   counts["pending"])
    c2.metric("🟢 Done",      counts["done"])
    c3.metric("🔴 Failed",    counts["failed"])
    c4.metric("⭐ Important", counts["important"])
    c5.metric("🗄️ Archived",  counts["archived"])

    st.divider()

    recent = _fetch_jobs(exclude_archived=True)[:10]
    if recent:
        st.subheader("Recent Jobs")
        for row in recent:
            job_id, company, role, status, link, error, *_ = row
            status = status or "pending"
            cols = st.columns([0.5, 2, 2.5, 1.5, 3])
            cols[0].write(f"**#{job_id}**")
            cols[1].write(company or "TBD")
            cols[2].write(role or "TBD")
            cols[3].markdown(_status_badge(status))
            if link:
                cols[4].markdown(f"[link]({link})")
    else:
        st.info("No jobs yet. Add one to get started.")

    st.divider()
    if counts["pending"] > 0:
        st.info(f"{counts['pending']} pending job(s) ready. Go to **▶️ Run Pipeline** to process.")


# ═══════════════════════════════════════════════
#  QUICK APPLY — paste JD, get resume + cover letter immediately.
#  No separate add-then-run step: this adds the job to the jobs table AND
#  processes it in one click, then serves the PDFs right here.
# ═══════════════════════════════════════════════
elif page == "⚡ Quick Apply":
    st.header("Quick Apply")
    st.caption("Paste a JD, hit Generate. Job is logged to the table automatically — no separate Add Job step.")

    with st.form("quick_apply_form"):
        jd = st.text_area(
            "Job Description",
            height=380,
            placeholder="Paste the full job description here...",
            key="quick_apply_jd",
        )
        c1, c2, c3 = st.columns(3)
        company = c1.text_input("Company (optional)", placeholder="Leave blank — LLM extracts from JD", key="quick_apply_company")
        link = c2.text_input("Job Link (optional)", placeholder="https://...", key="quick_apply_link")
        run_mode = c3.radio(
            "What to generate", ["Resume + Cover Letter", "Cover Letter Only"],
            key="quick_apply_mode",
        )
        cl_personalization = st.text_area(
            "Cover Letter Personalization",
            height=90,
            placeholder="Optional — extra instructions/context for the cover letter.",
            key="quick_apply_cl",
        )
        force = st.checkbox(
            "Force generate (ignore visa sponsorship gate)",
            key="quick_apply_force",
        )
        submitted = st.form_submit_button("⚡ Generate", type="primary", use_container_width=True)

    if submitted:
        if not jd.strip():
            st.error("Job description is required.")
        else:
            from pipeline import _process_job

            add_job(link.strip(), jd.strip(), company.strip(), "", cl_personalization.strip())
            conn = _connect()
            job_id = conn.execute(
                "SELECT id FROM jobs WHERE jd = ? ORDER BY id DESC LIMIT 1", (jd.strip(),)
            ).fetchone()[0]
            conn.close()

            mode = "cover_letter" if run_mode == "Cover Letter Only" else "both"
            with st.spinner("Generating..."):
                try:
                    company, role = _process_job(job_id, link.strip(), jd.strip(), company.strip(), "", cl_personalization.strip(), mode, force)
                    _update_job(job_id, status="done", company=company, role=role, error=None)
                    # Persist the result: the download buttons below are plain
                    # st.buttons, and clicking one reruns the script with the
                    # form no longer "submitted". Without this the buttons would
                    # disappear the moment you used them.
                    st.session_state["_qa_result"] = {
                        "job_id": job_id, "company": company, "role": role,
                    }
                except Exception:
                    err = traceback.format_exc()
                    _update_job(job_id, status="failed", error=err)
                    st.session_state.pop("_qa_result", None)
                    st.error("Generation failed.")
                    st.code(err, language="python")

    result = st.session_state.get("_qa_result")
    if result:
        job_id, company, role = result["job_id"], result["company"], result["role"]
        st.success(f"Done — {company} / {role}")
        res_path, cl_path = _output_paths(company, role)
        d1, d2, d3 = st.columns([1.5, 1.5, 5])
        if res_path.exists():
            with d1:
                _save_button(
                    "⬇️ Resume", res_path.read_bytes(),
                    DOWNLOAD_RESUME_NAME, key=f"qa_dl_res_{job_id}",
                )
        if cl_path.exists():
            with d2:
                _save_button(
                    "⬇️ Cover Letter", cl_path.read_bytes(),
                    f"{_safe(company)}_CoverLetter.pdf", key=f"qa_dl_cl_{job_id}",
                )
        with d3:
            if st.button("✖️ Clear", key=f"qa_clear_{job_id}"):
                st.session_state.pop("_qa_result", None)
                st.rerun()


# ═══════════════════════════════════════════════
#  ADD JOB
# ═══════════════════════════════════════════════
elif page == "➕ Add Job":
    st.header("Add Job")

    if st.session_state.get("_add_job_success"):
        st.success("Job queued. Go to **▶️ Run Pipeline** to process it.")
        del st.session_state["_add_job_success"]

    with st.form("add_job_form", clear_on_submit=True):
        link = st.text_input("Job Link", placeholder="https://...")
        c1, c2 = st.columns(2)
        company = c1.text_input("Company", placeholder="Leave blank — LLM extracts from JD")
        role    = c2.text_input("Role",    placeholder="Leave blank — LLM extracts from JD")
        jd = st.text_area(
            "Job Description",
            height=420,
            placeholder="Paste the full job description here...",
        )
        cl_personalization = st.text_area(
            "Cover Letter Personalization",
            height=120,
            placeholder="Optional — extra instructions/context for the cover letter (leave blank for default style).",
        )
        submitted = st.form_submit_button("➕ Add Job", type="primary", use_container_width=True)

    if submitted:
        if not jd.strip():
            st.error("Job description is required.")
        else:
            added = add_job(link.strip(), jd.strip(), company.strip(), role.strip(), cl_personalization.strip())
            if added:
                st.session_state["_add_job_success"] = True
                st.rerun()
            else:
                st.error("Already applied — this job URL is already in the pipeline.")


# ═══════════════════════════════════════════════
#  JOBS (CRUD)
# ═══════════════════════════════════════════════
elif page == "📋 Jobs":
    st.header("Jobs")

    # Bulk actions row
    ba1, ba2, ba3, _ = st.columns([1.6, 1.6, 1.6, 5.2])

    if ba1.button("↩️ Retry All Failed", key="retry_all_btn", use_container_width=True):
        _retry_all_failed()
        st.rerun()

    select_label = "✅ Done Selecting" if st.session_state.select_mode else "☑️ Select Jobs"
    if ba2.button(select_label, key="select_mode_btn", use_container_width=True):
        st.session_state.select_mode = not st.session_state.select_mode
        st.rerun()

    if st.session_state.confirm_delete_all:
        st.warning("**Delete ALL jobs permanently?** This cannot be undone.")
        ca1, ca2, _ = st.columns([1, 1, 6])
        if ca1.button("Yes, delete all", type="primary", key="confirm_all_yes"):
            _delete_all_jobs()
            st.session_state.confirm_delete_all = False
            st.rerun()
        if ca2.button("Cancel", key="confirm_all_no"):
            st.session_state.confirm_delete_all = False
            st.rerun()
    else:
        if ba3.button("🗑️ Delete All", key="delete_all_btn", use_container_width=True):
            st.session_state.confirm_delete_all = True
            st.rerun()

    # Filter + search bar
    fc1, fc2, fc3 = st.columns([2, 3, 1])
    status_filter = fc1.selectbox(
        "Filter by status",
        ["All", "Pending", "Done", "Failed"],
        label_visibility="collapsed",
    )
    search = fc2.text_input(
        "Search",
        placeholder="Search company or role...",
        label_visibility="collapsed",
    )
    if fc3.button("🔄 Refresh", use_container_width=True):
        st.rerun()

    rows = _fetch_jobs(status_filter, search, exclude_archived=True, exclude_important=True)

    if not rows:
        st.info("No jobs match the filter.")
    else:
        all_ids = [r[0] for r in rows]

        if st.session_state.select_mode:
            st.info("Check jobs, then use the action buttons below.")

            # Select all / deselect all
            sa1, sa2, _ = st.columns([1.2, 1.2, 7.6])
            if sa1.button("☑️ All", key="sel_all"):
                for jid in all_ids:
                    st.session_state[f"sel_cb_{jid}"] = True
                st.rerun()
            if sa2.button("🔲 None", key="sel_none"):
                for jid in all_ids:
                    st.session_state[f"sel_cb_{jid}"] = False
                st.rerun()

        st.caption(f"{len(rows)} job(s)")
        for row in rows:
            job_id = row[0]
            status = row[3] or "pending"

            if st.session_state.select_mode:
                col_check, col_card = st.columns([0.4, 9.6])
                with col_check:
                    st.checkbox("", key=f"sel_cb_{job_id}", label_visibility="collapsed")
                with col_card:
                    _job_card(row, expanded=False)
            else:
                _job_card(row, expanded=(status == "failed"))

        # Action bar — shown at bottom after checkboxes
        if st.session_state.select_mode:
            selected_ids = [jid for jid in all_ids if st.session_state.get(f"sel_cb_{jid}", False)]
            st.divider()
            act1, act2, _ = st.columns([2, 2, 6])

            if act1.button(
                f"🔁 Redo Selected ({len(selected_ids)})",
                key="redo_selected_btn",
                type="primary",
                disabled=len(selected_ids) == 0,
                use_container_width=True,
            ):
                _redo_jobs(selected_ids)
                for jid in selected_ids:
                    st.session_state.pop(f"sel_cb_{jid}", None)
                st.session_state.select_mode = False
                st.success(f"{len(selected_ids)} job(s) reset to pending.")
                st.rerun()

            if act2.button(
                f"🗑️ Delete Selected ({len(selected_ids)})",
                key="del_selected_btn",
                disabled=len(selected_ids) == 0,
                use_container_width=True,
            ):
                for jid in selected_ids:
                    _delete_job(jid)
                    st.session_state.pop(f"sel_cb_{jid}", None)
                st.session_state.select_mode = False
                st.success(f"{len(selected_ids)} job(s) deleted.")
                st.rerun()


# ═══════════════════════════════════════════════
#  IMPORTANT — starred jobs get their own page so they don't get lost
#  in the general Jobs list.
# ═══════════════════════════════════════════════
elif page == "⭐ Important":
    st.header("Important")
    st.caption("Jobs you starred. Mark or unmark from any job card.")

    ic1, ic2 = st.columns([3, 1])
    imp_search = ic1.text_input(
        "Search",
        placeholder="Search company or role...",
        label_visibility="collapsed",
        key="important_search",
    )
    if ic2.button("🔄 Refresh", key="important_refresh", use_container_width=True):
        st.rerun()

    rows = _fetch_jobs("Important", imp_search)

    if not rows:
        st.info("No important jobs. Star one from the Jobs page.")
    else:
        st.caption(f"{len(rows)} important job(s)")
        for row in rows:
            _job_card(row, expanded=False)


# ═══════════════════════════════════════════════
#  RUN PIPELINE
# ═══════════════════════════════════════════════
elif page == "▶️ Run Pipeline":
    st.header("Run Pipeline")

    pending_rows = _fetch_jobs("Pending")

    if not pending_rows:
        st.info("No pending jobs. Add a job or retry a failed one.")
    else:
        st.write(f"**{len(pending_rows)} pending job(s):**")

        selected = {}
        for row in pending_rows:
            job_id, company, role, *_ = row
            label = f"#{job_id} — {company or 'TBD'} / {role or 'TBD'}"
            selected[job_id] = st.checkbox(label, value=True, key=f"sel_{job_id}")

        to_run = [jid for jid, checked in selected.items() if checked]

        st.divider()

        run_mode = st.radio(
            "What to generate",
            ["Resume + Cover Letter", "Cover Letter Only", "Tag Resume Version"],
            horizontal=True,
        )
        mode = {
            "Cover Letter Only": "cover_letter",
            "Tag Resume Version": "tag",
        }.get(run_mode, "both")

        if mode == "tag":
            st.caption(
                "No resume is generated. Picks the best-fitting resume already in your library, "
                "rates it out of 10, and writes a cover letter grounded in that exact document — "
                "so you upload your own resume plus this letter."
            )

        force = st.checkbox("Force generate (ignore visa sponsorship gate)", key="run_pipeline_force")

        if st.button(
            f"▶️ Run {len(to_run)} job(s)",
            type="primary",
            disabled=len(to_run) == 0,
            use_container_width=True,
        ):
            from pipeline import _process_job
            import sys, io

            log_box   = st.empty()
            log_lines: list[str] = []

            class _Cap(io.StringIO):
                def write(self, s):
                    super().write(s)
                    if s.strip():
                        log_lines.append(s.rstrip())
                        log_box.code("\n".join(log_lines[-50:]))

            conn = sqlite3.connect(DB_PATH)
            old_stdout = sys.stdout
            sys.stdout = _Cap()

            try:
                for job_id in to_run:
                    row = conn.execute(
                        "SELECT id, job_link, jd, company, role, cl_personalization FROM jobs WHERE id = ?",
                        (job_id,),
                    ).fetchone()
                    if not row:
                        continue
                    jid, jlink, jjd, jcompany, jrole, jcl_personalization = row
                    try:
                        final_company, final_role = _process_job(
                            jid, jlink or "", jjd or "", jcompany or "", jrole or "", jcl_personalization or "", mode, force
                        )
                        conn.execute(
                            "UPDATE jobs SET status='done', company=?, role=?, error=NULL WHERE id=?",
                            (final_company, final_role, jid),
                        )
                    except Exception:
                        err = traceback.format_exc()
                        conn.execute(
                            "UPDATE jobs SET status='failed', error=? WHERE id=?",
                            (err, jid),
                        )
                    conn.commit()
            finally:
                sys.stdout = old_stdout
                conn.close()

            log_box.code("\n".join(log_lines))
            st.success("Done. Check Jobs page for results.")


# ═══════════════════════════════════════════════
#  ARCHIVE
# ═══════════════════════════════════════════════
elif page == "🗄️ Archive":
    st.header("Archive")

    ac1, ac2, _ = st.columns([1.8, 1.8, 6.4])

    if ac1.button("🗄️ Archive All Done", key="arch_all_btn", use_container_width=True):
        _archive_done_jobs()
        st.rerun()

    arch_search = ac2.text_input(
        "Search",
        placeholder="Search company or role...",
        label_visibility="collapsed",
    )

    rows = _fetch_jobs("Archived", arch_search)

    if not rows:
        st.info("No archived jobs.")
    else:
        st.caption(f"{len(rows)} archived job(s)")
        for row in rows:
            _job_card(row, expanded=False)
