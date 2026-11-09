"""Run the portal on an explicitly selected local port without changing release config."""
import argparse
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--no-warmup", action="store_true", help="Disable background refresh for local review")
args = parser.parse_args()
if not 1 <= args.port <= 65535:
    parser.error("port must be between 1 and 65535")
if args.no_warmup:
    os.environ["PORTAL_NO_WARMUP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

app.app.run(host="127.0.0.1", port=args.port, threaded=True)
