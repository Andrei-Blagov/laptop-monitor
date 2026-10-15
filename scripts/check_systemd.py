"""Parse prepared F2 units against an isolated systemd root; never install/start.

Docker/boot targets and compose.sh are harmless executable stubs. This checks
unit syntax/dependencies, not server permissions, Docker or actual activation.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UNITS = (
    "laptop-monitor-market-daily.service", "laptop-monitor-market-daily.timer",
    "laptop-monitor-market-retry.service", "laptop-monitor-market-retry.timer",
    "laptop-monitor-thailand.path", "laptop-monitor-thailand.service",
    "laptop-monitor-personal-top.service", "laptop-monitor-personal-top.timer",
)


def main():
    if shutil.which("systemd-analyze") is None:
        print("UNAVAILABLE: systemd-analyze")
        return 2
    with tempfile.TemporaryDirectory(prefix="laptop-systemd-") as folder:
        root = Path(folder)
        units = root / "etc/systemd/system"
        units.mkdir(parents=True)
        for name in UNITS:
            shutil.copyfile(ROOT / "deploy/systemd" / name, units / name)
        for target in ("sysinit", "basic", "shutdown", "timers", "paths", "multi-user", "network-online"):
            (units / f"{target}.target").write_text("[Unit]\nDescription=Isolated acceptance stub\n")
        (units / "docker.service").write_text("[Service]\nType=oneshot\nExecStart=/bin/true\n")
        for executable in ("bin/true", "opt/laptop-monitor/deploy/compose.sh"):
            dest = root / executable
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile("/usr/bin/true", dest)
            dest.chmod(0o755)
        subprocess.run(["systemd-analyze", "--root", folder, "verify", "--man=no", *UNITS], check=True)
    print(f"PASS: {len(UNITS)} prepared units; isolated syntax check only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
