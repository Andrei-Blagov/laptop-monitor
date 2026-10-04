from __future__ import annotations

import unittest
from unittest.mock import patch

from integrations.n8n import (
    build_pipeline_completed_payload,
    format_failure_alert_message,
    safe_error_summary,
)


class SafeErrorSummaryTests(unittest.TestCase):
    def test_truncates_long_text(self) -> None:
        text = "x" * 500
        out = safe_error_summary(text, limit=40)
        assert out is not None
        self.assertLessEqual(len(out), 40)
        self.assertTrue(out.endswith("…"))

    def test_redacts_telegram_bot_url(self) -> None:
        out = safe_error_summary(
            "HTTPError: https://api.telegram.org/bot123:SECRET/sendMessage boom"
        )
        assert out is not None
        self.assertNotIn("SECRET", out)
        self.assertNotIn("123:SECRET", out)


class FailureAlertFormatTests(unittest.TestCase):
    def test_success_returns_none(self) -> None:
        self.assertIsNone(
            format_failure_alert_message({"status": "success", "run_id": 1})
        )

    def test_partial_message(self) -> None:
        msg = format_failure_alert_message(
            {
                "status": "partial",
                "instance_id": "vps-prod",
                "run_id": 42,
                "finished_at": "2026-10-04T07:00:00+00:00",
                "messages_sent": 1,
                "messages_failed": 0,
                "stores": [
                    {
                        "slug": "regard",
                        "display_name": "Regard",
                        "status": "ok",
                    },
                    {
                        "slug": "citilink",
                        "display_name": "Citilink",
                        "status": "failed",
                        "error": "TimeoutError: browser stuck",
                    },
                ],
            }
        )
        assert msg is not None
        self.assertIn("Laptop Monitor — PARTIAL", msg)
        self.assertIn("Instance: vps-prod", msg)
        self.assertIn("Run: 42", msg)
        self.assertIn("Citilink — FAILED:", msg)
        self.assertIn("Regard — OK", msg)
        self.assertIn("sent: 1", msg)
        self.assertIn("failed: 0", msg)
        self.assertIn("MSK", msg)

    def test_failed_message(self) -> None:
        msg = format_failure_alert_message(
            {
                "status": "failed",
                "instance_id": "vps-prod",
                "run_id": 7,
                "finished_at": "2026-10-04T07:00:00+00:00",
                "error_summary": "Database locked",
                "stores": [
                    {
                        "slug": "regard",
                        "display_name": "Regard",
                        "status": "failed",
                        "error": "down",
                    }
                ],
            }
        )
        assert msg is not None
        self.assertIn("Laptop Monitor — FAILED", msg)
        self.assertIn("Причина:", msg)
        self.assertIn("Database locked", msg)
        self.assertIn("Regard — FAILED", msg)

    def test_payload_includes_store_error_and_summary(self) -> None:
        with patch("integrations.n8n.get_version", return_value="0.2.0"):
            with patch(
                "integrations.n8n.config.get_instance_id", return_value="vps-prod"
            ):
                with patch(
                    "integrations.n8n.config.get_monitor_region", return_value="moscow"
                ):
                    payload = build_pipeline_completed_payload(
                        run_id=9,
                        status="partial",
                        started_at="t0",
                        finished_at="t1",
                        duration_seconds=1.0,
                        store_statuses={
                            "citilink": {
                                "status": "failed",
                                "products_count": None,
                                "error": "boom",
                            },
                            "regard": {
                                "status": "ok",
                                "products_count": 3,
                                "error": None,
                            },
                        },
                        alerts_created=0,
                        messages_sent=0,
                        messages_failed=0,
                        error_summary="one store failed",
                    )
        cit = next(s for s in payload["stores"] if s["slug"] == "citilink")
        self.assertEqual(cit["error"], "boom")
        self.assertEqual(payload["error_summary"], "one store failed")
        msg = format_failure_alert_message(payload)
        assert msg is not None
        self.assertIn("PARTIAL", msg)


if __name__ == "__main__":
    unittest.main()
