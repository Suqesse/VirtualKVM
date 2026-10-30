"""Stream a native CI check and expose failure details in its check annotation."""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A command is required after --")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    tail = deque(maxlen=240)
    with args.log.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", bufsize=1)
        for line in process.stdout:
            log.write(line)
            log.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line)
        status = process.wait()
    if status:
        message = "".join(tail)[-3500:]
        message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Native build check failed::{message}", flush=True)
    return status if status >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
