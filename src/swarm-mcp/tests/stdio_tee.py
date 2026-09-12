"""Tee MCP stdout to a log file so tests can assert protocol-only stdout."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    log_path = os.environ["SWARM_MCP_STDOUT_LOG"]
    harness = os.environ["SWARM_MCP_HARNESS"]
    extra = os.environ.get("SWARM_MCP_HARNESS_ARGS", "")
    args = [sys.executable, harness, *([extra] if extra else [])]
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(args, stdin=sys.stdin, stdout=subprocess.PIPE, stderr=sys.stderr)
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            log.write(line)
            log.flush()
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        raise SystemExit(proc.wait() or 0)


if __name__ == "__main__":
    main()
