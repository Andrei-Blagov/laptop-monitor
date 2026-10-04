from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from integrations.n8n import (
    archive_run_summary,
    compute_digest_metrics,
    filter_history_window,
    format_daily_digest_message,
    make_run_summary,
    prune_run_history,
    select_digest_top_deals,
    should_send_digest,
)


def _run(
    *,
    run_id: int,
    status: str,
    hours_ago: float,
    alerts: int = 0,
    sent: int = 0,
    failed: int = 0,
    instance: str = "vps-prod",
    stores=None,
    top_deals=None,
) -> dict:
    now = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=hours_ago)).isoformat()
    return {
        "run_id": run_id,
        "status": status,
        "started_at": ts,
        "finished_at": ts,
        "instance_id": instance,
        "alerts_created": alerts,
        "messages_sent": sent,
        "messages_failed": failed,
        "stores": stores
        or [
            {"slug": "regard", "display_name": "Regard", "status": "ok"},
            {"slug": "andpro", "display_name": "ANDPRO", "status": "ok"},
            {"slug": "kns", "display_name": "KNS", "status": "ok"},
            {"slug": "citilink", "display_name": "Citilink", "status": "ok"},
        ],
        "top_deals": top_deals
        or [
            {
                "rank": 1,
                "name": "Gigabyte AORUS",
                "store": "KNS",
                "price": 220485,
                "url": "https://example.test/1",
            }
        ],
    }


NOW = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


class DailyDigestTests(unittest.TestCase):
    def test_a_twelve_success_metrics(self) -> None:
        history = [_run(run_id=i, status="success", hours_ago=i * 0.5, alerts=1, sent=1) for i in range(12)]
        window = filter_history_window(history, now=NOW)
        metrics = compute_digest_metrics(window)
        self.assertEqual(metrics["total_runs"], 12)
        self.assertEqual(metrics["success"], 12)
        self.assertEqual(metrics["partial"], 0)
        self.assertEqual(metrics["failed"], 0)
        self.assertEqual(metrics["alerts_created"], 12)
        self.assertEqual(metrics["messages_sent"], 12)

    def test_b_mixed_status_counts(self) -> None:
        history = (
            [_run(run_id=i, status="success", hours_ago=i) for i in range(10)]
            + [_run(run_id=100, status="partial", hours_ago=0.5)]
            + [_run(run_id=101, status="failed", hours_ago=0.2)]
        )
        metrics = compute_digest_metrics(filter_history_window(history, now=NOW))
        self.assertEqual(metrics["success"], 10)
        self.assertEqual(metrics["partial"], 1)
        self.assertEqual(metrics["failed"], 1)
        self.assertEqual(metrics["total_runs"], 12)

    def test_c_alerts_totals(self) -> None:
        history = [
            _run(run_id=1, status="success", hours_ago=1, alerts=2, sent=2),
            _run(run_id=2, status="success", hours_ago=2, alerts=5, sent=4, failed=1),
        ]
        metrics = compute_digest_metrics(filter_history_window(history, now=NOW))
        self.assertEqual(metrics["alerts_created"], 7)
        self.assertEqual(metrics["messages_sent"], 6)
        self.assertEqual(metrics["messages_failed"], 1)

    def test_d_latest_store_status_used(self) -> None:
        older = _run(run_id=1, status="success", hours_ago=2)
        newer = _run(
            run_id=2,
            status="partial",
            hours_ago=0.1,
            stores=[
                {"slug": "regard", "display_name": "Regard", "status": "ok"},
                {"slug": "citilink", "display_name": "Citilink", "status": "failed"},
            ],
        )
        msg = format_daily_digest_message([older, newer], now=NOW)
        self.assertIn("⚠️ Citilink — FAILED", msg)
        self.assertIn("✅ Regard", msg)
        self.assertIn("PARTIAL", msg)

    def test_e_top_deals_price_cap_failsafe(self) -> None:
        history = [
            _run(
                run_id=1,
                status="success",
                hours_ago=1,
                top_deals=[
                    {"name": "cheap", "store": "KNS", "price": 220000},
                    {"name": "expensive", "store": "Regard", "price": 350000},
                    {"name": "mid", "store": "ANDPRO", "price": 238990},
                ],
            )
        ]
        tops = select_digest_top_deals(filter_history_window(history, now=NOW))
        names = [t["name"] for t in tops]
        self.assertIn("cheap", names)
        self.assertIn("mid", names)
        self.assertNotIn("expensive", names)
        msg = format_daily_digest_message(history, now=NOW)
        self.assertNotIn("350000", msg.replace(" ", ""))
        self.assertNotIn("350 000", msg)

    def test_f_empty_day_message(self) -> None:
        msg = format_daily_digest_message([], now=NOW)
        self.assertIn("За последние 24 часа запусков не было", msg)
        msg2 = format_daily_digest_message(
            [_run(run_id=1, status="success", hours_ago=30)], now=NOW
        )
        self.assertIn("За последние 24 часа запусков не было", msg2)

    def test_g_duplicate_same_msk_date(self) -> None:
        ok, key = should_send_digest(last_sent_date=None, now=NOW)
        self.assertTrue(ok)
        ok2, key2 = should_send_digest(last_sent_date=key, now=NOW)
        self.assertFalse(ok2)
        self.assertEqual(key, key2)
        # test mode always sends
        ok3, _ = should_send_digest(last_sent_date=key, now=NOW, test_mode=True)
        self.assertTrue(ok3)

    def test_h_telegram_failure_date_not_marked(self) -> None:
        # Pure helper contract: caller must only persist date when send ok.
        # Simulate: should_send True, send fails => last_sent stays None => retry ok.
        last_sent = None
        should, key = should_send_digest(last_sent_date=last_sent, now=NOW)
        self.assertTrue(should)
        send_ok = False
        if send_ok:
            last_sent = key
        should_retry, _ = should_send_digest(last_sent_date=last_sent, now=NOW)
        self.assertTrue(should_retry)

    def test_i_old_events_excluded(self) -> None:
        history = [
            _run(run_id=1, status="success", hours_ago=1),
            _run(run_id=2, status="success", hours_ago=30),
        ]
        window = filter_history_window(history, now=NOW)
        self.assertEqual(len(window), 1)
        self.assertEqual(window[0]["run_id"], 1)

    def test_j_history_retention_capped(self) -> None:
        # 120 runs within 48h => prune to 100
        history = [
            _run(run_id=i, status="success", hours_ago=min(47, i * 0.3))
            for i in range(120)
        ]
        pruned = prune_run_history(history, now=NOW, max_records=100)
        self.assertEqual(len(pruned), 100)
        # >48h dropped
        old = [_run(run_id=999, status="success", hours_ago=60)]
        self.assertEqual(prune_run_history(old, now=NOW), [])

    def test_archive_upsert_and_non_prod_filtered(self) -> None:
        hist = archive_run_summary([], _run(run_id=1, status="success", hours_ago=1), now=NOW)
        hist = archive_run_summary(hist, _run(run_id=1, status="partial", hours_ago=1), now=NOW)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "partial")
        hist = archive_run_summary(
            hist, _run(run_id=2, status="success", hours_ago=1, instance="local-dev"), now=NOW
        )
        window = filter_history_window(hist, now=NOW)
        self.assertEqual(len(window), 1)
        self.assertEqual(window[0]["run_id"], 1)

    def test_summary_strips_store_errors(self) -> None:
        summary = make_run_summary(
            {
                "run_id": 1,
                "status": "partial",
                "stores": [
                    {
                        "slug": "citilink",
                        "display_name": "Citilink",
                        "status": "failed",
                        "error": "TimeoutError: huge traceback ...",
                    }
                ],
                "top_deals": [],
            }
        )
        self.assertNotIn("error", summary["stores"][0])

    def test_test_mode_header(self) -> None:
        msg = format_daily_digest_message([], now=NOW, test_mode=True)
        self.assertTrue(msg.startswith("🧪 Laptop Monitor — Daily Digest TEST"))


if __name__ == "__main__":
    unittest.main()
