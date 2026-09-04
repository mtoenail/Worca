"""Push the running swarm's artifacts to GitHub on a timer.

Streamlit Community Cloud serves the dashboard from the repo, so its file-based panels -
the Oracle's decisions, the order log, the shadow book - are only as fresh as the last
commit. Without this the deployed app shows a snapshot frozen at deploy time while the
swarm keeps trading, which misrepresents a live system.

Only `results/` is ever staged. Nothing else is added, so a stray local edit - or a
credentials file - cannot be swept in by a background process nobody is watching.

    python publish_results.py --interval 300
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

TRACKED = "results/"


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def guard_no_secrets():
    """Refuse to run at all if anything credential-shaped is tracked."""
    out = run("git", "ls-files").stdout.splitlines()
    bad = [f for f in out
           if f.endswith((".env", "secrets.toml"))
           or f.startswith(".env") and not f.endswith(".example")]
    if bad:
        sys.exit(f"REFUSING TO PUBLISH - credential files are tracked: {bad}")


HISTORY = ("gex_history.csv", "volspread_history.csv")
RUN_DIR = "results/submission"


def sync_history():
    """Copy the agents' history CSVs into the published run directory.

    The agents write these at the repo root, which is gitignored as a working file, so
    without this the deployed dashboard's History tab has nothing to read.
    """
    import shutil
    for name in HISTORY:
        if os.path.exists(name) and os.path.isdir(RUN_DIR):
            try:
                shutil.copyfile(name, os.path.join(RUN_DIR, name))
            except OSError:
                pass                      # mid-write; the next cycle picks it up


def publish():
    """Stage only results/, commit if anything changed, push. Returns a status string."""
    sync_history()
    run("git", "add", "--", TRACKED)
    staged = run("git", "diff", "--cached", "--name-only").stdout.split()
    if not staged:
        return "no change"
    # Belt and braces: never commit anything outside results/, whatever else is staged.
    outside = [f for f in staged if not f.startswith(TRACKED)]
    if outside:
        run("git", "reset", "HEAD", "--", *outside)
        staged = [f for f in staged if f.startswith(TRACKED)]
        if not staged:
            return "nothing in results/ to publish"
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    c = run("git", "commit", "-m", f"Live artifacts {stamp}")
    if c.returncode:
        return f"commit failed: {c.stderr.strip()[:120]}"
    # Rebase before pushing. The remote moves for reasons that have nothing to do with
    # this process - a commit made through the GitHub web UI diverged the branch on
    # 2026-09-04 and every subsequent push was rejected as non-fast-forward, silently
    # freezing the deployed dashboard while the local one kept updating.
    pull = run("git", "pull", "--rebase", "--autostash", "origin", "main")   # swarm writes mid-cycle
    if pull.returncode:
        run("git", "rebase", "--abort")
        return f"rebase failed (will retry): {pull.stderr.strip()[:120]}"
    p = run("git", "push", "origin", "HEAD:main")
    if p.returncode:
        return f"push failed (will retry): {p.stderr.strip()[:120]}"
    return f"published {len(staged)} file(s)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between pushes; 300 keeps the deployed app close to live")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    guard_no_secrets()
    print(f"publishing {TRACKED} every {a.interval}s - Ctrl+C to stop", flush=True)
    while True:
        try:
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {publish()}", flush=True)
        except Exception as e:
            print(f"[publish] {type(e).__name__}: {e}", flush=True)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
