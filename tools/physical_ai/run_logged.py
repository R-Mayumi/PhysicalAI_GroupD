#!/usr/bin/env python3
"""Run one explicit command and append its output/status to the audit log."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "docs/environment_audit/commands.jsonl"


def run(command, timeout=60):
    started = datetime.now(timezone.utc).isoformat()
    tick = time.monotonic()
    try:
        proc = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                output, _ = proc.communicate()
            code, output = -1, f'TIMED_OUT after {timeout}s\n'+output
    except OSError as exc:
        code, output = -1, str(exc)
    record = dict(started_at=started, command=command, returncode=code,
                  elapsed_s=round(time.monotonic()-tick, 3), output=output)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    raise SystemExit(run(args.command, args.timeout))
