"""Reproducible Linux acceptance, without credentials or live endpoints.

Install requirements.txt first. Full check: python -m scripts.check_offline.
Focused regressions: python -m scripts.check_offline --tests tests.test_f2_handoff.
Docker checks are separate: python -m scripts.check_containers --image TAG.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", nargs="+", help="Only these unittest modules")
    args = parser.parse_args(argv)
    output = ROOT / "dist" / "checks"
    output.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    # The check never consumes a local production .env or real credentials.
    for key in list(env):
        if key.startswith(("TELEGRAM_", "N8N_")):
            env.pop(key)
    env["PYTHONPATH"] = str(ROOT / "tests" / "offline_guard")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLCONFIGDIR"] = str(output / "matplotlib")
    steps = []

    def run(name, command):
        started = time.monotonic()
        log = output / f"{name}.log"
        with log.open("w") as fh:
            proc = subprocess.run(command, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        steps.append({"name": name, "command": command, "exit_code": proc.returncode,
                      "seconds": round(time.monotonic() - started, 2), "log": log.name})
        print(f"{name}: {'PASS' if proc.returncode == 0 else 'FAIL'} ({log})", flush=True)
        if proc.returncode:
            print(log.read_text()[-6000:], flush=True)
        return proc.returncode == 0

    py = sys.executable
    if args.tests:
        ok = run("targeted", [py, "-m", "unittest", *args.tests, "-v"])
    else:
        # Individual failures do not hide the results of independent checks.
        ok = run("unittest", [py, "-m", "unittest", "discover", "-s", "tests", "-v"])
        ok = run("packaging", [py, "-m", "unittest", "tests.test_release_packaging", "-v"]) and ok
        tracked = subprocess.check_output(["git", "ls-files", "-z", "*.py"], cwd=ROOT).decode().split("\0")
        ok = run("compileall", [py, "-m", "compileall", "-q", *[p for p in tracked if p]]) and ok
        ok = run("diff", ["git", "diff", "--check", "HEAD"]) and ok
        ok = run("bundle", [py, "-m", "scripts.build_deploy_bundle"]) and ok
        version = (ROOT / "VERSION").read_text().strip()
        bundle = ROOT / "dist" / f"laptop-monitor-{version}.tar.gz"
        if bundle.is_file():
            ok = run("verify-bundle", [py, "-m", "scripts.release_verify", "--bundle", str(bundle)]) and ok
            with tempfile.TemporaryDirectory(prefix="laptop-check-") as folder:
                with tarfile.open(bundle) as archive:
                    archive.extractall(folder, filter="data")
                ok = run("verify-tree", [py, "-m", "scripts.release_verify", "--tree",
                                         str(Path(folder) / f"laptop-monitor-{version}")]) and ok
    report = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
              "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
              "python": sys.version, "ok": ok, "steps": steps,
              "container_checks": "separate; not implied by this report"}
    (output / ("targeted-report.json" if args.tests else "offline-report.json")).write_text(
        json.dumps(report, indent=2) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
