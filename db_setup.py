import sqlite3
import argparse
from pathlib import Path

DB_PATH = "jobs.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            job_link TEXT UNIQUE,
            jd       TEXT,
            company  TEXT,
            role     TEXT,
            status   TEXT DEFAULT 'pending',
            error    TEXT,
            cl_personalization TEXT,
            tag_version TEXT,
            tag_rating  REAL,
            tag_report  TEXT
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, coltype in (
        ("cl_personalization", "TEXT"),
        ("tag_version", "TEXT"),
        ("tag_rating", "REAL"),
        ("tag_report", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
    conn.commit()
    conn.close()


def save_tag(job_id: int, version: str, rating: float, report: str) -> None:
    """Persist a resume-version tagging result for a job."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE jobs SET tag_version = ?, tag_rating = ?, tag_report = ? WHERE id = ?",
        (version, rating, report, job_id),
    )
    conn.commit()
    conn.close()


def add_job(link: str, jd: str, company: str = "", role: str = "", cl_personalization: str = "") -> bool:
    """Returns True if added, False if duplicate URL."""
    conn = sqlite3.connect(DB_PATH)
    # Store None for blank links so UNIQUE constraint allows multiple blank-link jobs
    link_val = link.strip() or None
    try:
        conn.execute(
            "INSERT INTO jobs (job_link, jd, company, role, cl_personalization) VALUES (?, ?, ?, ?, ?)",
            (link_val, jd, company, role, cl_personalization),
        )
        conn.commit()
        print(f"Job added (link={link_val or 'none'}, company={company or 'TBD'}, role={role or 'TBD'})")
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize jobs.db and optionally add a job.")
    parser.add_argument("--link",    default="",  help="Job posting URL")
    parser.add_argument("--jd",      required=True, help="Full job description text")
    parser.add_argument("--company", default="",  help="Company name (optional; LLM will extract if blank)")
    parser.add_argument("--role",    default="",  help="Role title (optional; LLM will extract if blank)")
    parser.add_argument("--cl-personalization", default="", help="Extra cover letter personalization instructions (optional)")
    args = parser.parse_args()

    init_db()
    add_job(args.link, args.jd, args.company, args.role, args.cl_personalization)
