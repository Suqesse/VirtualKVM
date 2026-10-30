"""Run bounded development checks and save an inspectable validation report."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packaged", type=Path, help="Additionally check this already built executable")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = root / "build" / "validation"
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    if sys.platform.startswith("linux"):
        environment["QT_AUDIO_BACKEND"] = "pulseaudio"
    commands = [
        ("doctor", [sys.executable, "-m", "virtualkvm", "doctor"], 30),
        ("tests", [sys.executable, str(root / "scripts/run_tests.py")], 300),
        ("audio-video-smoke", [sys.executable, "-m", "virtualkvm", "smoke", "--fps", "240", "--audio"], 90),
    ]
    if args.packaged:
        binary = str(args.packaged.resolve(strict=True))
        commands += [
            ("packaged-doctor", [binary, "doctor"], 30),
            ("packaged-audio-video", [binary, "smoke", "--fps", "240", "--audio"], 90),
        ]
    report = dict(
        created_utc=datetime.now(timezone.utc).isoformat(),
        platform=platform.platform(), python=sys.version.split()[0], checks=[],
        scope="Synthetic encrypted integration, UI, input dry-run, shared files and muted Qt audio/video playback. Native hardware capture, sound output and display throughput are not measured.",
        sources={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted((root / "virtualkvm").glob("*.py"))},
    )
    for name, command, timeout in commands:
        started = time.monotonic()
        log_path = output / f"{name}.log"
        with log_path.open("w", encoding="utf-8") as log:
            options = {"start_new_session": True} if os.name == "posix" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            process = subprocess.Popen(command, cwd=root, env=environment, stdout=log,
                stderr=subprocess.STDOUT, **options)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=log, stderr=subprocess.STDOUT, check=False)
                process.wait()
                code = 124
                log.write(f"\nCheck exceeded {timeout} seconds.\n")
        entry = dict(name=name, exit_code=code, seconds=round(time.monotonic()-started, 2),
                     log=str(log_path.relative_to(root)), passed=code == 0)
        if name == "tests":
            with log_path.open("rb") as source:
                source.seek(max(0, log_path.stat().st_size - 128 * 1024))
                text = source.read().decode("utf-8", errors="replace")
            matches = re.findall(r"Ran (\d+) tests?", text)
            entry["executed_tests"] = int(matches[-1]) if matches else 0
            skipped = re.findall(r"skipped=(\d+)", text)
            entry["skipped_tests"] = int(skipped[-1]) if skipped else 0
            entry["passed"] = entry["passed"] and entry["executed_tests"] > 0
        report["checks"].append(entry)
        print(f"{name}: {'PASS' if entry['passed'] else 'FAIL'} ({entry['seconds']}s)", flush=True)
        if not entry["passed"]:
            with log_path.open("rb") as source:
                source.seek(max(0, log_path.stat().st_size - 128 * 1024))
                failure_log = source.read().decode("utf-8", errors="replace")
            if name == "tests" and environment.get("GITHUB_ACTIONS") == "true":
                failures = re.findall(r"\n={10,}\n((?:ERROR|FAIL):.*?)(?=\n={10,}\n|\n-{10,}\nRan |\Z)", failure_log, flags=re.DOTALL)
                for failure in failures:
                    message = failure[:3900].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                    print(f"::error title=Native test failure::{message}", flush=True)
                diagnostics = root / "build/validation/thread-diagnostics.log"
                if diagnostics.is_file():
                    traces = diagnostics.read_text(encoding="utf-8", errors="replace")[-24000:]
                    blocks = re.split(r"(?=(?:Current thread|Thread) 0x)", traces)
                    important = [block for block in blocks if any(name in block for name in ("test_gui.py", "test_app.py", "test_shared_gui.py"))]
                    for block in important[-2:]:
                        message = block[:3800].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                        print(f"::error title=Native watchdog stack::{message}", flush=True)
            print(failure_log[-18000:], flush=True)
            break
    report["passed"] = len(report["checks"]) == len(commands) and all(c["passed"] for c in report["checks"])
    target = output / "report.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {target}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
