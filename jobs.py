"""
Job CLI.
  python jobs.py add           — interactive: prompts + paste JD
  python jobs.py list          — show all jobs
  python jobs.py retry <id>    — re-queue failed job
"""

import argparse
import sqlite3
import sys
import textwrap

from db_setup import DB_PATH, add_job, init_db


# ── helpers ──────────────────────────────────

def _connect():
    return sqlite3.connect(DB_PATH)


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


STATUS_COLOR = {
    "pending":   "\033[33m",   # yellow
    "done":      "\033[32m",   # green
    "failed":    "\033[31m",   # red
    "important": "\033[35m",   # magenta
    "archived":  "\033[90m",   # gray
}
RESET = "\033[0m"


# ── subcommands ───────────────────────────────

def cmd_add(_args):
    init_db()

    link    = input("Job link (Enter to skip): ").strip()
    company = input("Company   (Enter to skip): ").strip()
    role    = input("Role      (Enter to skip): ").strip()

    print("Paste JD below — press Ctrl+D (Mac/Linux) or Ctrl+Z Enter (Windows) when done:")
    try:
        jd = sys.stdin.read().strip()
    except KeyboardInterrupt:
        print("\nAborted.")
        return

    if not jd:
        print("No JD provided. Aborting.")
        return

    add_job(link, jd, company, role)


def cmd_list(_args):
    init_db()
    conn = _connect()
    rows = conn.execute(
        "SELECT id, company, role, status, job_link, error FROM jobs ORDER BY id DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No jobs in database.")
        return

    header = f"{'ID':>4}  {'COMPANY':<22}  {'ROLE':<28}  {'STATUS':<8}  LINK"
    print(header)
    print("─" * len(header))

    for job_id, company, role, status, link, error in rows:
        color = STATUS_COLOR.get(status or "pending", "")
        status_str = f"{color}{(status or 'pending'):<8}{RESET}"
        line = (
            f"{job_id:>4}  "
            f"{_truncate(company or 'TBD', 22):<22}  "
            f"{_truncate(role or 'TBD', 28):<28}  "
            f"{status_str}  "
            f"{_truncate(link or '', 50)}"
        )
        print(line)
        if status == "failed" and error:
            # Show last line of traceback
            last = [l for l in error.strip().splitlines() if l.strip()]
            if last:
                print(f"       └─ {_truncate(last[-1].strip(), 90)}")


def cmd_retry(args):
    if not args.id:
        print("Usage: python jobs.py retry <id>")
        return
    init_db()
    conn = _connect()
    row = conn.execute("SELECT id, status FROM jobs WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"No job with id={args.id}.")
        conn.close()
        return
    conn.execute(
        "UPDATE jobs SET status = 'pending', error = NULL WHERE id = ?",
        (args.id,),
    )
    conn.commit()
    conn.close()
    print(f"Job {args.id} reset to pending.")


# ── main ─────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="jobs.py",
        description="Manage job applications pipeline.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("add",  help="Add a new job interactively")
    sub.add_parser("list", help="List all jobs")

    retry_p = sub.add_parser("retry", help="Re-queue a failed job")
    retry_p.add_argument("id", type=int, help="Job ID to retry")

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "retry":
        cmd_retry(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
