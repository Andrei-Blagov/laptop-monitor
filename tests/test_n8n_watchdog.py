from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ops import n8n_watchdog as wd


class StateFileTests(unittest.TestCase):
    def test_missing_state_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = wd.load_state(Path(tmp) / "missing.json")
            self.assertEqual(state.status, "OK")
            self.assertEqual(state.consecutive_failures, 0)
            self.assertFalse(state.alert_sent)

    def test_corrupted_state_resets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not-json", encoding="utf-8")
            state = wd.load_state(path)
            self.assertEqual(state.consecutive_failures, 0)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            original = wd.WatchState(
                status="DOWN",
                consecutive_failures=3,
                down_since="2026-01-01T00:00:00+00:00",
                last_check="2026-01-01T00:15:00+00:00",
                alert_sent=True,
            )
            wd.save_state(path, original)
            loaded = wd.load_state(path)
            self.assertEqual(loaded.to_dict(), original.to_dict())


class EvaluateCheckTests(unittest.TestCase):
    def test_running_http_ok(self) -> None:
        data = {"State": {"Status": "running", "Restarting": False}}
        result = wd.evaluate_check(data, True, "ok")
        self.assertEqual(result.status, "OK")

    def test_running_http_fail_degraded(self) -> None:
        data = {"State": {"Status": "running", "Restarting": False}}
        result = wd.evaluate_check(data, False, "http_fail")
        self.assertEqual(result.status, "DEGRADED")

    def test_stopped_down(self) -> None:
        data = {"State": {"Status": "exited", "Restarting": False}}
        result = wd.evaluate_check(data, False, "skipped")
        self.assertEqual(result.status, "DOWN")

    def test_restarting_down(self) -> None:
        data = {"State": {"Status": "running", "Restarting": True}}
        result = wd.evaluate_check(data, False, "skipped")
        self.assertEqual(result.status, "DOWN")

    def test_missing_down(self) -> None:
        result = wd.evaluate_check({"_missing": True}, False, "skipped")
        self.assertEqual(result.status, "DOWN")


class WatchCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
        self.host = "vps-test"
        self.ok = wd.CheckResult("OK", "running", "ok")
        self.fail = wd.CheckResult("DOWN", "exited", "skipped")

    def test_ok_resets_failure_counter(self) -> None:
        state = wd.WatchState(status="DEGRADED", consecutive_failures=2, down_since="x")
        sent: list[str] = []

        def send(text: str) -> tuple[bool, str | None]:
            sent.append(text)
            return True, None

        new, action = wd.run_watch_cycle(
            state=state, check=self.ok, hostname=self.host, send=send, now=self.now
        )
        self.assertEqual(new.consecutive_failures, 0)
        self.assertIsNone(new.down_since)
        self.assertFalse(new.alert_sent)
        self.assertIsNone(action)
        self.assertEqual(sent, [])

    def test_one_failure_no_alert(self) -> None:
        state = wd.WatchState()
        sent: list[str] = []
        new, action = wd.run_watch_cycle(
            state=state,
            check=self.fail,
            hostname=self.host,
            send=lambda t: sent.append(t) or (True, None),
            now=self.now,
        )
        self.assertEqual(new.consecutive_failures, 1)
        self.assertFalse(new.alert_sent)
        self.assertIsNone(action)
        self.assertEqual(sent, [])

    def test_two_failures_no_alert(self) -> None:
        state = wd.WatchState(consecutive_failures=1, down_since="2026-09-30T08:55:00+00:00")
        sent: list[str] = []
        new, action = wd.run_watch_cycle(
            state=state,
            check=self.fail,
            hostname=self.host,
            send=lambda t: sent.append(t) or (True, None),
            now=self.now,
        )
        self.assertEqual(new.consecutive_failures, 2)
        self.assertFalse(new.alert_sent)
        self.assertIsNone(action)
        self.assertEqual(sent, [])

    def test_third_failure_down_alert_once(self) -> None:
        state = wd.WatchState(consecutive_failures=2, down_since="2026-09-30T08:50:00+00:00")
        sent: list[str] = []
        new, action = wd.run_watch_cycle(
            state=state,
            check=self.fail,
            hostname=self.host,
            send=lambda t: sent.append(t) or (True, None),
            now=self.now,
        )
        self.assertEqual(new.consecutive_failures, 3)
        self.assertTrue(new.alert_sent)
        self.assertEqual(action, "down")
        self.assertEqual(len(sent), 1)
        self.assertIn("n8n DOWN", sent[0])
        self.assertIn("Failed checks: 3", sent[0])

    def test_fourth_fifth_no_duplicate_down(self) -> None:
        state = wd.WatchState(
            status="DOWN",
            consecutive_failures=3,
            down_since="2026-09-30T08:50:00+00:00",
            alert_sent=True,
        )
        sent: list[str] = []
        for _ in range(2):
            state, action = wd.run_watch_cycle(
                state=state,
                check=self.fail,
                hostname=self.host,
                send=lambda t: sent.append(t) or (True, None),
                now=self.now,
            )
            self.assertIsNone(action)
            self.assertTrue(state.alert_sent)
        self.assertEqual(sent, [])
        self.assertEqual(state.consecutive_failures, 5)

    def test_recovery_one_message(self) -> None:
        state = wd.WatchState(
            status="DOWN",
            consecutive_failures=4,
            down_since="2026-09-30T08:45:00+00:00",
            alert_sent=True,
        )
        sent: list[str] = []
        new, action = wd.run_watch_cycle(
            state=state,
            check=self.ok,
            hostname=self.host,
            send=lambda t: sent.append(t) or (True, None),
            now=self.now,
        )
        self.assertEqual(action, "recovered")
        self.assertEqual(len(sent), 1)
        self.assertIn("n8n RECOVERED", sent[0])
        self.assertEqual(new.consecutive_failures, 0)
        self.assertFalse(new.alert_sent)
        self.assertIsNone(new.down_since)

    def test_next_ok_no_duplicate_recovery(self) -> None:
        state = wd.WatchState(status="OK", consecutive_failures=0, alert_sent=False)
        sent: list[str] = []
        new, action = wd.run_watch_cycle(
            state=state,
            check=self.ok,
            hostname=self.host,
            send=lambda t: sent.append(t) or (True, None),
            now=self.now,
        )
        self.assertIsNone(action)
        self.assertEqual(sent, [])
        self.assertEqual(new.status, "OK")

    def test_telegram_failure_does_not_mark_alert_sent(self) -> None:
        state = wd.WatchState(consecutive_failures=2, down_since="2026-09-30T08:50:00+00:00")
        new, action = wd.run_watch_cycle(
            state=state,
            check=self.fail,
            hostname=self.host,
            send=lambda t: (False, "network"),
            now=self.now,
        )
        self.assertEqual(new.consecutive_failures, 3)
        self.assertFalse(new.alert_sent)
        self.assertIsNone(action)

    def test_telegram_failure_on_recovery_keeps_alert_sent(self) -> None:
        state = wd.WatchState(
            status="DOWN",
            consecutive_failures=3,
            down_since="2026-09-30T08:45:00+00:00",
            alert_sent=True,
        )
        new, action = wd.run_watch_cycle(
            state=state,
            check=self.ok,
            hostname=self.host,
            send=lambda t: (False, "timeout"),
            now=self.now,
        )
        self.assertIsNone(action)
        self.assertTrue(new.alert_sent)
        self.assertEqual(new.consecutive_failures, 0)
        self.assertEqual(new.down_since, "2026-09-30T08:45:00+00:00")


class SanitizeTests(unittest.TestCase):
    def test_sanitize_strips_token(self) -> None:
        token = "123456:ABC-DEF"
        text = f"failed bot{token} and https://api.telegram.org/bot{token}/sendMessage"
        cleaned = wd.sanitize_error(text, token)
        self.assertNotIn(token, cleaned)
        self.assertNotIn("ABC-DEF", cleaned)


class EnvConfigTests(unittest.TestCase):
    def test_admin_chat_preferred(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "1",
            "TELEGRAM_ADMIN_CHAT_ID": "99,88",
        }
        token, chat = wd.get_telegram_config(env)
        self.assertEqual(token, "t")
        self.assertEqual(chat, "99")


class TestAlertCliTests(unittest.TestCase):
    def test_test_alert_does_not_write_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_ADMIN_CHAT_ID=1\n",
                encoding="utf-8",
            )
            with patch.object(wd, "send_telegram_message", return_value=(True, None)) as send:
                with patch.object(wd, "perform_live_check") as check:
                    rc = wd.main(
                        [
                            "--test-alert",
                            "--state-file",
                            str(state_path),
                            "--env-file",
                            str(env_path),
                        ]
                    )
            self.assertEqual(rc, 0)
            self.assertFalse(state_path.exists())
            check.assert_not_called()
            self.assertTrue(send.called)
            msg = send.call_args.args[2]
            self.assertIn("n8n watchdog test", msg)


if __name__ == "__main__":
    unittest.main()
