"""Run the three services from a clean directory.

Each service is a separate process with its own SQLite database and its own
storage directory:

    data/intake/   sources + object store
    data/indexer/  rulesets, indexes, offset mappings
    data/query/    reports and pinned citations

Usage:
    python3 -m app.run            # start all three
    python3 -m app.run --reset    # wipe local state first
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run intake, indexer and query services")
    parser.add_argument("--reset", action="store_true", help="delete local data/ before starting")
    parser.add_argument("--query-port", type=int, default=8100)
    parser.add_argument("--intake-port", type=int, default=8101)
    parser.add_argument("--indexer-port", type=int, default=8102)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args(argv)

    if args.reset and os.path.isdir(DATA):
        shutil.rmtree(DATA)

    env = dict(os.environ)
    env["INTAKE_DB"] = os.path.join(DATA, "intake", "intake.sqlite3")
    env["INTAKE_OBJECTS"] = os.path.join(DATA, "intake", "objects")
    env["INDEXER_DB"] = os.path.join(DATA, "indexer", "indexer.sqlite3")
    env["INDEXER_MAPPINGS"] = os.path.join(DATA, "indexer", "mappings")
    env["QUERY_DB"] = os.path.join(DATA, "query", "query.sqlite3")
    intake_url = f"http://{args.bind}:{args.intake_port}"
    indexer_url = f"http://{args.bind}:{args.indexer_port}"
    env["INTAKE_URL"] = intake_url
    env["INDEXER_URL"] = indexer_url

    commands = [
        [sys.executable, "-m", "app.intake", "--host", args.bind, "--port", str(args.intake_port)],
        [sys.executable, "-m", "app.indexer", "--host", args.bind, "--port", str(args.indexer_port)],
        [sys.executable, "-m", "app.query", "--host", args.bind, "--port", str(args.query_port)],
    ]
    procs = [subprocess.Popen(command, cwd=ROOT, env=env) for command in commands]

    def shutdown(*_args) -> None:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 5
        for proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"UI:      http://{args.bind}:{args.query_port}/")
    print(f"intake:  {intake_url}")
    print(f"indexer: {indexer_url}")
    print("Press Ctrl-C to stop all three services.")
    try:
        while True:
            for proc, command in zip(procs, commands):
                code = proc.poll()
                if code is not None:
                    print(f"service exited ({code}): {' '.join(command)}", file=sys.stderr)
                    shutdown()
            time.sleep(0.5)
    finally:
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
