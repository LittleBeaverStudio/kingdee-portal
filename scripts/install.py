#!/usr/bin/env python3
"""One-command local install helper for the Kingdee Portal DSH plugin.

What it does (all local, no network upload):
  1. Clones the repo if the target directory does not exist yet.
  2. Prints (and optionally runs) the `dsh plugin --profile <p> add` command.

It never writes your Kingdee credentials, and it does not modify DSH unless
you pass --run. The manual steps are identical to README "安装".

Usage:
  python scripts/install.py [--dir DIR] [--profile desktop] [--run]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "https://github.com/LittleBeaverStudio/kingdee-portal.git"
DEFAULT_DIR = Path.home() / "kingdee-portal"


def run(cmd: list[str], check: bool = True) -> int:
    printable = " ".join(cmd)
    print("+", printable)
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print(f"  ! command not found: {cmd[0]}", file=sys.stderr)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="clone target")
    ap.add_argument("--profile", default="desktop")
    ap.add_argument("--run", action="store_true", help="actually run dsh plugin add")
    args = ap.parse_args()

    if not args.dir.exists():
        print(f"Cloning {REPO} -> {args.dir}")
        if run(["git", "clone", REPO, str(args.dir)]) != 0:
            return 1
    else:
        print(f"Using existing {args.dir}")

    plugin_link = f"link:{args.dir / 'dsh-plugin'}"
    add_cmd = ["dsh", "plugin", "--profile", args.profile, "add", plugin_link]

    print("\nNext step (add the plugin to your DSH profile):")
    print("  " + " ".join(add_cmd))

    if args.run:
        # `dsh` may not be on PATH; only attempt when explicitly asked.
        if shutil.which("dsh") is None:
            print("\n! `dsh` not found on PATH; run the command above manually.",
                  file=sys.stderr)
            return 1
        return run(add_cmd, check=False)

    print("\nRe-run with --run to execute it, or paste the command into your "
          "terminal. Then restart DSH and open the sidebar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
