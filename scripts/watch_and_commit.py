#!/usr/bin/env python3
"""
scripts/watch_and_commit.py
============================

Runs radar_to_tiff.py's PAGASA lightning `--watch --split` mode as a
background subprocess, and wraps it with the two things a single GitHub
Actions job can't do on its own:

1. Periodically (every --commit-every-minutes) commits+pushes whatever
   new/updated CSV segment files have landed in --outdir back to the git
   repo -- so a mid-run crash or cancelled job never loses more than one
   commit interval's worth of data.

2. Stops the watch process cleanly -- the same SIGINT/Ctrl+C it already
   knows how to handle in radar_to_tiff.py -- after --max-minutes, so the
   whole run fits inside a single GitHub Actions job's 6-hour execution
   limit. The scheduled workflow then starts a fresh job a little later
   that picks up right where this one left off.

It also maintains data/lightning/index.json: an ordered list of every
segment CSV ever produced, in true chronological (creation) order. New
files are appended to this list in the order they appear on disk *this
run* (their mtimes are freshly accurate, since this process just wrote
them) -- files from earlier runs are already in the list and are never
reordered or re-sorted. This is what lets the dashboard (docs/index.html)
know which segment is "latest" without having to guess from filenames, or
rely on filesystem timestamps (which `git checkout` resets on every job),
or parse PAGASA's own `timestamp` field (whose exact string format isn't
documented/guaranteed).

Usage (see .github/workflows/lightning-watch.yml for the real invocation):
    python scripts/watch_and_commit.py --outdir data/lightning --branch main
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def sh(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, **kwargs)


def count_rows(csv_path: Path) -> int:
    """Number of data rows (header excluded). Best-effort -- a file that
    can't be read (e.g. caught mid-write) just reports 0 rather than
    blowing up the whole checkpoint."""
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            n = sum(1 for _ in csv.reader(f))
        return max(0, n - 1)
    except Exception as e:
        print(f"  [index] couldn't read {csv_path.name}: {e}", flush=True)
        return 0


def update_index(outdir: Path) -> Path:
    """Bring data/lightning/index.json up to date and return its path.
    See the module docstring for why ordering works the way it does."""
    index_path = outdir / "index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            index = {"files": []}
    else:
        index = {"files": []}

    files = index.get("files", [])
    known = {f["name"] for f in files}

    on_disk = sorted(outdir.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    new_files = [p for p in on_disk if p.name not in known]
    for p in new_files:
        row_count = count_rows(p)
        files.append({"name": p.name, "row_count": row_count, "size_bytes": p.stat().st_size})
        print(f"  [index] added {p.name} ({row_count} rows)", flush=True)

    # The most recent file may still be the actively-growing segment --
    # refresh its stats every time. Earlier (finalized/rotated-away) files
    # are only ever counted once, at the moment they're first added.
    if files:
        last_path = outdir / files[-1]["name"]
        if last_path.exists():
            files[-1]["row_count"] = count_rows(last_path)
            files[-1]["size_bytes"] = last_path.stat().st_size

    index["files"] = files
    index["generated_at"] = datetime.now(timezone.utc).isoformat()
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index_path


def git_commit_and_push(outdir: Path, branch: str, message: str) -> bool:
    sh(["git", "add", str(outdir)])
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        print("  [git] no changes to commit.", flush=True)
        return False
    sh(["git", "commit", "-m", message], check=True)

    for attempt in range(1, 4):
        sh(["git", "fetch", "origin", branch])
        rebase = sh(["git", "rebase", f"origin/{branch}"])
        if rebase.returncode != 0:
            # Something else pushed to this branch and now conflicts with
            # our change (very unlikely -- this workflow is the only
            # writer under normal use). Bail out rather than risk making
            # things worse; this run's data stays committed locally on
            # the runner only and is lost when the job ends, but nothing
            # in the actual repo gets corrupted.
            print(
                "  [git] rebase conflicted -- aborting. This run's checkpoint "
                "will NOT be pushed (something else changed this branch).",
                flush=True,
            )
            sh(["git", "rebase", "--abort"])
            return False
        push = sh(["git", "push", "origin", f"HEAD:{branch}"])
        if push.returncode == 0:
            return True
        print(f"  [git] push failed (attempt {attempt}/3), retrying in 10s...", flush=True)
        time.sleep(10)

    print(
        "  [git] WARNING: could not push after 3 attempts -- this run's data "
        "is committed locally on the runner only and will be lost when the "
        "job ends.",
        flush=True,
    )
    return False


def checkpoint(outdir: Path, branch: str, label: str) -> None:
    update_index(outdir)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    git_commit_and_push(outdir, branch, f"Lightning data: {label} update {now}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--branch", required=True, help="Git branch to rebase onto and push (e.g. main).")
    ap.add_argument("--max-minutes", type=int, default=350)
    ap.add_argument("--commit-every-minutes", type=int, default=10)
    ap.add_argument("--split", type=int, default=60)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--lightning-source", default="pagasa")
    args = ap.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "radar_to_tiff.py",
        "--lightning", "--lightning-source", args.lightning_source,
        "--watch", "--split", str(args.split), "--interval", str(args.interval),
        "--outdir", str(args.outdir),
    ]
    print(f"Starting: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd)

    start = time.time()
    deadline = start + args.max_minutes * 60
    next_checkpoint = start + args.commit_every_minutes * 60

    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"Watch process exited early with code {proc.returncode}.", flush=True)
                break
            if time.time() >= next_checkpoint:
                checkpoint(args.outdir, args.branch, "periodic")
                next_checkpoint = time.time() + args.commit_every_minutes * 60
            time.sleep(15)
    finally:
        if proc.poll() is None:
            elapsed_min = (time.time() - start) / 60
            print(
                f"\n{elapsed_min:.1f} min elapsed -- stopping the watch process "
                "(SIGINT, same as Ctrl+C) so this job finishes within GitHub's "
                "6-hour job limit...",
                flush=True,
            )
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print("  didn't exit in time -- terminating.", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        checkpoint(args.outdir, args.branch, "final")


if __name__ == "__main__":
    main()
