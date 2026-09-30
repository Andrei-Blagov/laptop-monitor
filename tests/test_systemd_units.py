from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def _read(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


class SystemdActivationSemanticsTests(unittest.TestCase):
    def test_pipeline_timer_no_requires_service(self) -> None:
        text = _read("laptop-monitor.timer")
        self.assertNotRegex(
            text,
            r"(?im)^\s*Requires\s*=\s*laptop-monitor\.service\s*$",
        )
        self.assertRegex(text, r"(?im)^\s*Unit\s*=\s*laptop-monitor\.service\s*$")
        self.assertRegex(text, r"(?im)^\s*Persistent\s*=\s*true\s*$")
        self.assertIn("OnCalendar=", text)

    def test_control_service_oneshot_remain_after_exit(self) -> None:
        text = _read("laptop-monitor-control.service")
        self.assertRegex(text, r"(?im)^\s*Type\s*=\s*oneshot\s*$")
        self.assertRegex(text, r"(?im)^\s*RemainAfterExit\s*=\s*yes\s*$")
        self.assertIn("up -d control-bot", text)
        self.assertIn("stop control-bot", text)
        self.assertNotRegex(text, r"(?im)^\s*Type\s*=\s*simple\s*$")
        self.assertNotRegex(text, r"(?im)^\s*Restart\s*=")

    def test_pipeline_service_oneshot_no_restart_always(self) -> None:
        text = _read("laptop-monitor.service")
        self.assertRegex(text, r"(?im)^\s*Type\s*=\s*oneshot\s*$")
        self.assertIn("compose.sh", text)
        self.assertIn("TimeoutStartSec=1800", text)
        self.assertNotRegex(text, r"(?im)^\s*Restart\s*=")

    def test_backup_timer_no_requires_backup_service(self) -> None:
        text = _read("laptop-monitor-backup.timer")
        self.assertNotRegex(
            text,
            r"(?im)^\s*Requires\s*=\s*laptop-monitor-backup\.service\s*$",
        )
        self.assertRegex(
            text, r"(?im)^\s*Unit\s*=\s*laptop-monitor-backup\.service\s*$"
        )
        self.assertRegex(text, r"(?im)^\s*Persistent\s*=\s*true\s*$")
        self.assertIn("03:15", text)


if __name__ == "__main__":
    unittest.main()
