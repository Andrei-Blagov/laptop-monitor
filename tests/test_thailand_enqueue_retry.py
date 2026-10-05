from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config
from buy_opportunity import evaluate_buy_rules, load_state, model_key_for_deal
from buy_thailand_flow import (
    _dedupe_key_for_signal,
    enqueue_manual_thailand_job,
    evaluate_buy_opportunity_flow,
)
from deal_ranking import RankedDeal
from thailand.job_models import TRIGGER_BUY, build_job
from thailand.job_queue import enqueue_job as real_enqueue_job
from thailand.job_queue import pending_count

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
MATURE = "2020-01-01T00:00:00+00:00"


def _deal(**kwargs) -> RankedDeal:
    store = kwargs.pop("store", "kns")
    ext = kwargs.pop("external_id", "1")
    defaults = dict(
        score=70.0,
        reasons=["x"],
        offer={"store": store, "external_id": ext, "name": "MSI Vector"},
        cluster_name="MSI Vector 17",
        cluster_key="msi-vector-17",
        gpu="RTX 5070 Ti",
        store=store,
        price=229990,
        url="https://example.test/1",
        ram_gb=32,
        ssd_gb=1024,
        screen_inch=17.0,
        cpu="Intel Core Ultra 9 275HX",
        confidence=100,
        historical_min=224990,
        history_started_at=MATURE,
    )
    defaults.update(kwargs)
    return RankedDeal(**defaults)


def _sender() -> MagicMock:
    sender = MagicMock()
    sender.send_message.return_value = SimpleNamespace(ok=True)
    return sender


class _Env:
    def __init__(self, tmp: str) -> None:
        root = Path(tmp)
        self.state = root / "buy_state.json"
        self.jobs = root / "jobs"
        self.sender = _sender()

    def run(self, deal: RankedDeal, at: datetime, *, enqueue=None) -> dict:
        patches = [patch("buy_opportunity._now", return_value=at)]
        if enqueue is not None:
            patches.append(patch("buy_thailand_flow.enqueue_job", side_effect=enqueue))
        for p in patches:
            p.start()
        try:
            return evaluate_buy_opportunity_flow(
                russian_deals=[deal],
                sender=self.sender,
                deliver=True,
                state_path=self.state,
                jobs_dir=self.jobs,
            )
        finally:
            for p in reversed(patches):
                p.stop()

    def entry(self, deal: RankedDeal) -> dict:
        return load_state(self.state)["models"][model_key_for_deal(deal)]


def _fs_failure(job, root=None, **kw):
    raise PermissionError("read-only jobs dir")


def _queue_full(job, root=None, **kw):
    return {"ok": False, "error": "queue_full", "job_id": job.get("job_id")}


class EnqueueRetryTests(unittest.TestCase):
    def _failed_first_run(self, env: _Env, deal: RankedDeal, failure=_fs_failure) -> dict:
        out = env.run(deal, T0, enqueue=failure)
        self.assertEqual(out["buy_actionable"], 1)
        self.assertEqual(out["thailand_scan_status"], "enqueue_failed")
        return out

    def test_01_enqueue_fail_sends_ru_message_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            out = self._failed_first_run(env, _deal())
            self.assertEqual(out["messages_sent"], 1)
            env.sender.send_message.assert_called_once()
            self.assertIn("не удалось", env.sender.send_message.call_args.args[0].lower())
            self.assertEqual(pending_count(env.jobs), 0)

    def test_02_state_remembers_buy_notified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal)
            entry = env.entry(deal)
            self.assertEqual(entry["last_signal_at"], T0.isoformat())
            self.assertEqual(entry["last_notified_price"], deal.price)
            self.assertEqual(entry["last_notified_level"], "BUY")

    def test_03_state_remembers_thailand_retry_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal, failure=_queue_full)
            entry = env.entry(deal)
            self.assertTrue(entry["pending_thailand_retry"])
            self.assertEqual(entry["last_thailand_enqueue_status"], "pending_retry")
            self.assertEqual(entry["last_thailand_enqueue_error"], "queue_full")
            self.assertEqual(entry["thailand_enqueue_retries"], 0)
            self.assertEqual(
                entry["pending_thailand_signal_fingerprint"],
                evaluate_buy_rules(deal).fingerprint,
            )

    def test_04_next_pipeline_same_price_does_not_resend_ru_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal)
            for hours in (2, 4, 24, 48):
                out = env.run(deal, T0 + timedelta(hours=hours))
                self.assertEqual(out["buy_actionable"], 0)
                self.assertEqual(out["messages"], [])
            env.sender.send_message.assert_called_once()

    def test_05_06_07_retry_after_interval_queues_and_clears_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal)
            spy = MagicMock(side_effect=real_enqueue_job)
            out = env.run(deal, T0 + timedelta(hours=2), enqueue=spy)
            spy.assert_called_once()
            self.assertEqual(out["thailand_scan_status"], "queued")
            self.assertTrue(out["thailand_scan_triggered"])
            self.assertEqual(out["thailand_retry"]["status"], "queued")
            self.assertEqual(out["messages"], [])
            self.assertEqual(pending_count(env.jobs), 1)
            entry = env.entry(deal)
            self.assertFalse(entry["pending_thailand_retry"])
            self.assertEqual(entry["last_thailand_enqueue_status"], "queued")
            self.assertEqual(entry["last_thailand_job_status"], "queued")
            self.assertEqual(entry["last_thailand_job_id"], out["thailand_job_id"])
            self.assertEqual(entry["last_signal_at"], T0.isoformat())
            later = env.run(deal, T0 + timedelta(hours=4))
            self.assertNotIn("thailand_retry", later)
            self.assertEqual(pending_count(env.jobs), 1)
            env.sender.send_message.assert_called_once()

    def test_08_duplicate_existing_job_clears_retry_as_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal)
            sig = evaluate_buy_rules(deal)
            existing = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key=_dedupe_key_for_signal(sig, TRIGGER_BUY),
            )
            self.assertTrue(real_enqueue_job(existing, root=env.jobs)["ok"])
            out = env.run(deal, T0 + timedelta(hours=2))
            self.assertEqual(out["thailand_retry"]["status"], "queued")
            self.assertEqual(out["thailand_job_id"], existing["job_id"])
            self.assertEqual(pending_count(env.jobs), 1)
            self.assertFalse(env.entry(deal)["pending_thailand_retry"])

    def test_09_retry_before_interval_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal)
            spy = MagicMock(side_effect=real_enqueue_job)
            minutes = float(config.THAILAND_ENQUEUE_RETRY_INTERVAL_MINUTES) - 1
            out = env.run(deal, T0 + timedelta(minutes=minutes), enqueue=spy)
            spy.assert_not_called()
            self.assertNotIn("thailand_retry", out)
            entry = env.entry(deal)
            self.assertTrue(entry["pending_thailand_retry"])
            self.assertEqual(entry["thailand_enqueue_retries"], 0)

    def test_10_max_retries_stops_automatic_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal)
            max_retries = int(config.THAILAND_ENQUEUE_MAX_RETRIES)
            spy = MagicMock(side_effect=_fs_failure)
            for n in range(1, max_retries + 3):
                env.run(deal, T0 + timedelta(hours=2 * n), enqueue=spy)
            self.assertEqual(spy.call_count, max_retries)
            entry = env.entry(deal)
            self.assertFalse(entry["pending_thailand_retry"])
            self.assertEqual(entry["thailand_enqueue_retries"], max_retries)
            self.assertEqual(entry["last_thailand_enqueue_status"], "enqueue_failed_final")
            self.assertEqual(entry["last_thailand_enqueue_error"], "PermissionError")
            env.sender.send_message.assert_called_once()

    def test_11_new_meaningful_event_supersedes_stale_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            old = _deal(store="kns", external_id="1", price=229990)
            self._failed_first_run(env, old)
            cheaper = _deal(store="regard", external_id="77", price=222990, historical_min=220000)
            spy = MagicMock(side_effect=real_enqueue_job)
            out = env.run(cheaper, T0 + timedelta(hours=3), enqueue=spy)
            self.assertEqual(out["buy_actionable"], 1)
            spy.assert_called_once()
            job = spy.call_args.args[0]
            self.assertEqual(job["dedupe_key"], _dedupe_key_for_signal(evaluate_buy_rules(cheaper), TRIGGER_BUY))
            self.assertEqual(pending_count(env.jobs), 1)
            state = load_state(env.state)["models"]
            self.assertFalse(state[model_key_for_deal(old)]["pending_thailand_retry"])
            self.assertEqual(state[model_key_for_deal(old)]["last_thailand_enqueue_status"], "superseded")
            self.assertFalse(state[model_key_for_deal(cheaper)]["pending_thailand_retry"])
            later = MagicMock(side_effect=real_enqueue_job)
            env.run(cheaper, T0 + timedelta(hours=6), enqueue=later)
            later.assert_not_called()
            self.assertEqual(pending_count(env.jobs), 1)

    def test_11b_new_event_on_same_model_resets_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            self._failed_first_run(env, _deal(score=70))
            strong = _deal(score=79)
            out = env.run(strong, T0 + timedelta(minutes=30))
            self.assertEqual(out["thailand_scan_status"], "queued")
            entry = env.entry(strong)
            self.assertFalse(entry["pending_thailand_retry"])
            self.assertEqual(entry["last_notified_level"], "STRONG_BUY")
            self.assertEqual(pending_count(env.jobs), 1)

    def test_12_manual_thailand_unaffected_by_pending_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            self._failed_first_run(env, deal)
            before = load_state(env.state)
            out = enqueue_manual_thailand_job(
                compare=True, chat_id="42", russian_deals=[deal], jobs_dir=env.jobs
            )
            self.assertTrue(out["ok"])
            self.assertEqual(out["thailand_scan_status"], "queued")
            self.assertEqual(pending_count(env.jobs), 1)
            self.assertEqual(load_state(env.state), before)

    def test_legacy_state_without_retry_fields_never_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            deal = _deal()
            from buy_opportunity import save_state

            save_state(
                {
                    "version": 1,
                    "models": {
                        model_key_for_deal(deal): {
                            "last_signal_at": T0.isoformat(),
                            "last_ru_price": deal.price,
                            "last_signal_level": "BUY",
                        }
                    },
                },
                env.state,
            )
            spy = MagicMock(side_effect=real_enqueue_job)
            out = env.run(deal, T0 + timedelta(hours=5), enqueue=spy)
            spy.assert_not_called()
            self.assertEqual(out["messages"], [])


if __name__ == "__main__":
    unittest.main()
