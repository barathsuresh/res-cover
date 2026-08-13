# Job Application Pipeline

Tailors your resume and cover letter to each job description using an LLM. Outputs a 1-page resume PDF and a personalized cover letter PDF per job.

## Setup

### 1. Get an Ollama Cloud API key

1. Go to [ollama.com](https://ollama.com) and sign up
2. Navigate to your account settings → API Keys
3. Create a new key and copy it

### 2. Configure environment

Create a `.env` file in the project root:

```
OLLAMA_API_KEY=your_key_here
```

### 3. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Run

```bash
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

## Usage

1. **Add Job** — paste a job link and the full job description. Company and role are extracted automatically if left blank.
2. **Run Pipeline** — select jobs to process, pick a mode, and click Run:
   - *Resume + Cover Letter* — tailors your resume bullets and writes a personalized cover letter.
   - *Cover Letter Only* — skips the resume.
   - *Tag Resume Version* — no resume is generated. Picks the best-fitting **existing** resume from your resume library, rates the fit out of 10 (with per-version scores and gaps), and writes a cover letter grounded in *that exact PDF* — so you upload your own resume plus the matching letter.
3. **Jobs** — view, edit, retry, or delete any job. Download the generated Resume and Cover Letter PDFs directly from the UI.

## Config

`config.py` controls the LLM provider and model:

```python
PROVIDER     = "ollama"        # ollama | gemini
OLLAMA_MODEL = "gpt-oss:120b"  # model served on ollama.com
ENV_VAR      = "OLLAMA_API_KEY"
```

To switch providers, update `PROVIDER` and `ENV_VAR`, then add the corresponding `_call_*` function in `llm.py`.

## Output

```
outputs/
└── {Company}/
    └── {Role}/
        ├── Resume.pdf
        └── Cover_Letter.pdf
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI |
| `pipeline.py` | Orchestrator — fetches pending jobs, calls LLM, builds PDFs |
| `llm.py` | LLM abstraction — `call_llm(system, user) -> dict` |
| `config.py` | Provider, model, env var |
| `resume_builder.py` | Builds 1-page resume PDF from data dict |
| `cover_letter_builder.py` | Builds cover letter PDF |
| `resume_tagger.py` | Rates existing resume versions against a JD and picks the best |
| `db_setup.py` | SQLite schema + CLI to add jobs |
| `jobs.py` | Terminal CLI for managing jobs |

## Resume version tagging

The tagger reads the PDF resume versions in your resume library:

```
/Users/barathsuresh/Documents/Barath Suresh Docs/RESUME/
├── Barath_Suresh_AI_Engineer_Resume.pdf
├── Barath_Suresh_Backend_Resume.pdf
├── Barath_Suresh_Cloud_Resume.pdf
├── Barath_Suresh_SRE_Platform_Resume.pdf
└── Resume_Docx/master/Barath_Suresh_Master_Resume.pdf
```

Drop a new `Barath_Suresh_<Name>_Resume.pdf` in that folder and it becomes a candidate
automatically. Override the location with `RESUME_DIR`.

```bash
python resume_tagger.py --list                        # show available versions
python resume_tagger.py --jd-file jd.txt              # rate them against a JD
python pipeline.py --mode tag                         # tag every pending job
```

Results are stored on the job (`tag_version`, `tag_rating`, `tag_report`) and shown in the
Jobs page, with a download button for the winning file. Version labels come from the file
names, so `Barath_Suresh_Cloud_Resume.pdf` shows up as "Cloud".

The cover letter is written against the text of the winning PDF, not `data.json`, so it never
cites a project or metric that resume doesn't show.

If the winning version is **more than one page** (the Master resume is 2 pages), it can't be
uploaded as-is — the run falls back to normal generation and produces a tailored 1-page
`Resume.pdf` plus its matching letter for that JD. The tag is still saved, with a note in the
report saying which file to actually send. It lands in the usual place
(`outputs/{Company}/{Role}/Cover_Letter.pdf`) and there is no `Resume.pdf` — you send the
library file. Every resume download button hands you the same neutral filename,
`Barath_Suresh_Master_Resume.pdf`, so a recruiter never sees "SRE_Platform" or their own
company name in the file. The 🏷️ button on a job card re-tags that one job without writing a letter.

## One-page enforcement

`data.json` mirrors the master resume as-is, even though the master runs past one page —
the shortening is the LLM's job, not a hand edit. `pipeline.py` enforces the single page in
this order:

1. Up to 3 LLM attempts with per-entry character budgets that tighten each round (x1.00, x0.85, x0.75), with concrete violations fed back between attempts.
2. Entries the model *inflated* (wrong bullet count, or longer than the master) revert to master text.
3. Line spacing squeezes 11.25 → 10.5.
4. Last resort: the Open Source section is dropped.
5. Still over? The job fails for manual review.
