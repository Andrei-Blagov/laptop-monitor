from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from buy_opportunity import (
    DEFAULT_STATE_PATH,
    BuySignal,
    load_state,
    record_signal_enqueued,
    record_thailand_job_completed,
    save_state,
    state_process_lock,
    with_state,
)
from buy_thailand_flow import (
    enqueue_manual_thailand_job,
    evaluate_buy_opportunity_flow,
    execute_thailand_job,
)
from deal_ranking import RankedDeal
from thailand.job_models import (
    TRIGGER_BUY,
    build_job,
    strip_secrets,
    validate_job,
)
from thailand.job_queue import (
    archive_job,
    claim_next_job,
    enqueue_job,
    ensure_queue_dirs,
    find_active_dedupe,
    pending_count,
    read_worker_state,
    recover_stale_processing,
)
from thailand.models import StoreScanResult
from thailand.worker import drain, process_one


def _deal(**kwargs) -> RankedDeal:
    defaults = dict(
        score=91.0,
        reasons=["x"],
        offer={"store": "kns", "external_id": "1", "name": "MSI"},
        cluster_name="MSI Vector",
        gpu="RTX 5070 Ti",
        store="kns",
        price=229990,
        url="https://example/1",
        ram_gb=32,
        ssd_gb=1024,
        screen_inch=17.0,
        cpu="Intel Core Ultra 9 275HX",
        confidence=100,
        historical_min=224990,
        history_started_at="2020-01-01T00:00:00+00:00",
    )
    defaults.update(kwargs)
    return RankedDeal(**defaults)


class JobQueueTests(unittest.TestCase):
    def test_atomic_enqueue_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            job = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key=sig.fingerprint,
            )
            out = enqueue_job(job, root=root)
            self.assertTrue(out["ok"])
            path = root / "pending" / f"{job['job_id']}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["status"], "pending")

    def test_duplicate_dedupe_not_enqueued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            j1 = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="same-key",
            )
            j2 = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="same-key",
            )
            self.assertTrue(enqueue_job(j1, root=root)["ok"])
            out = enqueue_job(j2, root=root)
            self.assertFalse(out["ok"])
            self.assertTrue(out.get("duplicate"))
            self.assertEqual(pending_count(root), 1)

    def test_unique_bypass_can_enqueue_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            a = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="k1",
            )
            b = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="k2",
            )
            self.assertTrue(enqueue_job(a, root=root)["ok"])
            self.assertTrue(enqueue_job(b, root=root)["ok"])
            self.assertEqual(pending_count(root), 2)

    def test_claim_archive_partial_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            job = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="c1",
            )
            enqueue_job(job, root=root)
            claimed = claim_next_job(root=root)
            assert claimed is not None
            self.assertEqual(claimed["status"], "processing")
            self.assertEqual(pending_count(root), 0)
            claimed["status"] = "partial"
            archive_job(claimed, root=root, failed=False)
            self.assertTrue((root / "archive" / f"{job['job_id']}.json").exists())

            job2 = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="c2",
            )
            enqueue_job(job2, root=root)
            c2 = claim_next_job(root=root)
            assert c2 is not None
            c2["status"] = "failed"
            c2["error_class"] = "Boom"
            archive_job(c2, root=root, failed=True)
            self.assertTrue((root / "failed" / f"{job2['job_id']}.json").exists())

    def test_stale_processing_recovery_and_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = ensure_queue_dirs(tmp)
            job_id = "stale1"
            old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
            payload = {
                "schema_version": 1,
                "job_id": job_id,
                "dedupe_key": "d",
                "trigger_type": TRIGGER_BUY,
                "status": "processing",
                "attempt": 1,
                "claimed_at": old,
                "signals": [],
                "russian_context": [],
            }
            (root / "processing" / f"{job_id}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with patch("thailand.job_queue.config.THAILAND_JOB_STALE_PROCESSING_SECONDS", 1):
                with patch("thailand.job_queue.config.THAILAND_JOB_MAX_ATTEMPTS", 2):
                    recovered = recover_stale_processing(root=root)
            self.assertIn(job_id, recovered)
            self.assertTrue((root / "pending" / f"{job_id}.json").exists())

            # Second claim + stale with attempt>=max → failed
            claimed = claim_next_job(root=root)
            assert claimed is not None
            claimed["claimed_at"] = old
            claimed["attempt"] = 2
            (root / "processing" / f"{job_id}.json").write_text(
                json.dumps(claimed), encoding="utf-8"
            )
            with patch("thailand.job_queue.config.THAILAND_JOB_STALE_PROCESSING_SECONDS", 1):
                with patch("thailand.job_queue.config.THAILAND_JOB_MAX_ATTEMPTS", 2):
                    recover_stale_processing(root=root)
            self.assertTrue((root / "failed" / f"{job_id}.json").exists())

    def test_retention_archive_50_failed_20(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = ensure_queue_dirs(tmp)
            with patch("thailand.job_queue.config.THAILAND_JOB_ARCHIVE_RETENTION", 50):
                with patch("thailand.job_queue.config.THAILAND_JOB_FAILED_RETENTION", 20):
                    for i in range(55):
                        archive_job(
                            {"job_id": f"a{i}", "status": "ok"},
                            root=root,
                            failed=False,
                        )
                    for i in range(25):
                        archive_job(
                            {"job_id": f"f{i}", "status": "failed"},
                            root=root,
                            failed=True,
                        )
                    self.assertEqual(len(list((root / "archive").glob("*.json"))), 50)
                    self.assertEqual(len(list((root / "failed").glob("*.json"))), 20)

    def test_no_secrets_serialized(self) -> None:
        dirty = {"bot_token": "x", "ok": True, "nested": {"api_key": "y", "v": 1}}
        clean = strip_secrets(dirty)
        self.assertNotIn("bot_token", clean)
        self.assertNotIn("api_key", clean["nested"])
        ok, _ = validate_job(
            {
                "schema_version": 1,
                "job_id": "j",
                "dedupe_key": "d",
                "trigger_type": TRIGGER_BUY,
            }
        )
        self.assertTrue(ok)


class PipelineAsyncTests(unittest.TestCase):
    def test_buy_evaluates_and_enqueues_without_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            jobs = root / "jobs"
            deal = _deal()
            sleep_scan = MagicMock(
                side_effect=lambda **kw: (_ for _ in ()).throw(
                    AssertionError("scan must not be called")
                )
            )
            with patch("thailand.scanner.run_thailand_scan", sleep_scan):
                with patch("thailand.jib.collect", sleep_scan):
                    t0 = time.perf_counter()
                    out = evaluate_buy_opportunity_flow(
                        russian_deals=[deal],
                        deliver=False,
                        state_path=state,
                        jobs_dir=jobs,
                        source_pipeline_run_id=57,
                    )
                    elapsed = time.perf_counter() - t0
            self.assertGreaterEqual(out["buy_actionable"], 1)
            self.assertTrue(out["thailand_scan_triggered"])
            self.assertEqual(out["thailand_scan_status"], "queued")
            self.assertIsNotNone(out["thailand_job_id"])
            self.assertIn("ВЫГОДНЫЙ МОМЕНТ", out["messages"][0])
            self.assertIn("отдельным сообщением", out["messages"][0])
            self.assertEqual(pending_count(jobs), 1)
            self.assertLess(elapsed, 2.0)
            sleep_scan.assert_not_called()

            st = load_state(state)
            entry = next(iter(st["models"].values()))
            self.assertIn("last_thailand_job_enqueued_at", entry)
            self.assertNotIn("last_thailand_scan_at", entry)
            self.assertEqual(entry["last_thailand_job_status"], "queued")

    def test_no_buy_no_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weak = _deal(price=280000, historical_min=224990, score=40)
            out = evaluate_buy_opportunity_flow(
                russian_deals=[weak],
                deliver=False,
                state_path=root / "s.json",
                jobs_dir=root / "jobs",
            )
            self.assertEqual(out["buy_actionable"], 0)
            self.assertFalse(out["thailand_scan_triggered"])
            self.assertEqual(pending_count(root / "jobs"), 0)

    def test_pipeline_creates_sender_when_none(self) -> None:
        """CLI/systemd pipeline passes sender=None; BUY must still Telegram."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            fake = MagicMock()
            fake.send_message.return_value = MagicMock(ok=True)
            fake.close = MagicMock()
            with patch(
                "buy_thailand_flow.config.get_telegram_credentials",
                return_value=("tok", "1"),
            ), patch(
                "buy_thailand_flow.TelegramSender",
                return_value=fake,
            ):
                out = evaluate_buy_opportunity_flow(
                    russian_deals=[deal],
                    sender=None,
                    deliver=True,
                    state_path=root / "s.json",
                    jobs_dir=root / "jobs",
                )
            self.assertEqual(out["messages_sent"], 1)
            fake.send_message.assert_called_once()
            fake.close.assert_called_once()
            self.assertEqual(pending_count(root / "jobs"), 1)

    def test_enqueue_failure_keeps_russian_success_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            with patch(
                "buy_thailand_flow.enqueue_job",
                return_value={"ok": False, "error": "queue_full"},
            ):
                out = evaluate_buy_opportunity_flow(
                    russian_deals=[deal],
                    deliver=False,
                    state_path=root / "s.json",
                    jobs_dir=root / "jobs",
                )
            self.assertEqual(out["thailand_scan_status"], "enqueue_failed")
            self.assertIn("не удалось", out["messages"][0].lower())

    def test_pipeline_does_not_wait_for_slow_scan(self) -> None:
        """Gate: slow scan would add 30s IF called — pipeline must finish fast."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()

            def slow_scan(**kwargs):
                time.sleep(30)
                return {"status": "ok"}

            with patch("thailand.scanner.run_thailand_scan", side_effect=slow_scan):
                t0 = time.perf_counter()
                out = evaluate_buy_opportunity_flow(
                    russian_deals=[deal],
                    deliver=False,
                    state_path=root / "s.json",
                    jobs_dir=root / "jobs",
                )
                elapsed = time.perf_counter() - t0
            self.assertEqual(out["thailand_scan_status"], "queued")
            self.assertLess(elapsed, 5.0)
            self.assertEqual(pending_count(root / "jobs"), 1)

    def test_n8n_queued_payload_fields(self) -> None:
        from integrations.n8n import build_pipeline_completed_payload

        payload = build_pipeline_completed_payload(
            run_id=56,
            status="success",
            started_at="t0",
            finished_at="t1",
            duration_seconds=40.0,
            store_statuses={},
            alerts_created=0,
            messages_sent=1,
            messages_failed=0,
            buy_opportunities_count=1,
            thailand_scan_triggered=True,
            thailand_scan_status="queued",
            thailand_job_id="abc123",
        )
        self.assertEqual(payload["thailand_scan_status"], "queued")
        self.assertEqual(payload["thailand_job_id"], "abc123")
        self.assertEqual(payload["event"], "pipeline.completed")
        self.assertEqual(payload["status"], "success")


class WorkerTests(unittest.TestCase):
    def test_worker_once_and_drain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            state = root / "state.json"
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            for key in ("d1", "d2"):
                job = build_job(
                    trigger_type=TRIGGER_BUY,
                    signals=[sig],
                    russian_deals=[deal],
                    dedupe_key=key,
                )
                enqueue_job(job, root=jobs)

            def fake_scan(**kwargs):
                return {
                    "status": "partial",
                    "snapshot_path": str(root / "snap.json"),
                    "stores": [
                        {"store": "jib", "ok": True, "count": 1},
                        {"store": "advice", "ok": False, "error": "HTTP_403"},
                        {"store": "banana", "ok": False, "error": "HTTP_403"},
                    ],
                    "unverified_candidates": [],
                    "thailand_top": [],
                    "_store_results": [
                        StoreScanResult(store="jib", ok=True),
                        StoreScanResult(store="advice", ok=False, error="HTTP_403"),
                        StoreScanResult(store="banana", ok=False, error="HTTP_403"),
                    ],
                    "_fx": None,
                    "_best_match": None,
                    "_comparison": None,
                    "_top": [],
                }

            (root / "snap.json").write_text("{}", encoding="utf-8")
            n = drain(
                jobs_dir=jobs,
                state_path=state,
                deliver=False,
                scan_fn=fake_scan,
                worker_state_path=root / "worker_state.json",
            )
            self.assertEqual(n, 2)
            self.assertEqual(
                read_worker_state(root / "worker_state.json")["last_status"], "partial"
            )
            self.assertEqual(pending_count(jobs), 0)
            self.assertEqual(len(list((jobs / "archive").glob("*.json"))), 2)
            st = load_state(state)
            entry = next(iter(st["models"].values()))
            self.assertIn("last_thailand_scan_at", entry)
            self.assertEqual(entry["last_thailand_scan_status"], "partial")

    def test_worker_empty_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            n = drain(
                jobs_dir=tmp,
                deliver=False,
                scan_fn=lambda **k: {"status": "ok"},
                worker_state_path=Path(tmp) / "worker_state.json",
            )
            self.assertEqual(n, 0)

    def test_worker_failure_archives_failed_no_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            db = root / "db.sqlite"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, store TEXT)")
            con.execute("INSERT INTO products(store) VALUES ('regard')")
            con.commit()
            before = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            con.close()

            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            job = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="fail1",
            )
            enqueue_job(job, root=jobs)

            def boom(**kwargs):
                raise RuntimeError("jib down")

            process_one(
                jobs_dir=jobs,
                state_path=root / "s.json",
                deliver=False,
                scan_fn=boom,
                worker_state_path=root / "worker_state.json",
            )
            # execute catches and returns failed with no stores → archive failed
            archives = list((jobs / "archive").glob("*.json")) + list(
                (jobs / "failed").glob("*.json")
            )
            self.assertEqual(len(archives), 1)
            con = sqlite3.connect(db)
            after = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            con.close()
            self.assertEqual(before, after)

    def test_automatic_job_does_not_resend_russian_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            job = build_job(
                trigger_type=TRIGGER_BUY,
                signals=[sig],
                russian_deals=[deal],
                dedupe_key="nr1",
            )
            sender = MagicMock()
            sender.send_message.return_value = MagicMock(ok=True)

            def fake_scan(**kwargs):
                return {
                    "status": "ok",
                    "snapshot_path": None,
                    "stores": [{"store": "jib", "ok": True, "count": 1}],
                    "unverified_candidates": [],
                    "thailand_top": [],
                    "_store_results": [StoreScanResult(store="jib", ok=True)],
                    "_fx": None,
                    "_best_match": None,
                    "_comparison": None,
                    "_top": [],
                }

            out = execute_thailand_job(
                job,
                sender=sender,
                deliver=True,
                state_path=root / "s.json",
                scan_fn=fake_scan,
            )
            for call in sender.send_message.call_args_list:
                text = call.args[0]
                self.assertNotIn("ВЫГОДНЫЙ МОМЕНТ", text)
            self.assertGreaterEqual(out["messages_sent"], 1)


class StateLockTests(unittest.TestCase):
    def test_enqueue_then_completion_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            st = {}
            st = record_signal_enqueued(st, sig, job_id="j1", top1_key=sig.model_key)
            save_state(st, path)
            entry = load_state(path)["models"][sig.model_key]
            self.assertIn("last_thailand_job_enqueued_at", entry)
            self.assertNotIn("last_thailand_scan_at", entry)

            def _mut(s):
                return record_thailand_job_completed(
                    s,
                    model_key=sig.model_key,
                    job_id="j1",
                    job_status="partial",
                    scan_status="partial",
                    snapshot_path="data/thailand_scans/x.json",
                )

            with_state(_mut, path)
            entry2 = load_state(path)["models"][sig.model_key]
            self.assertIn("last_thailand_scan_at", entry2)
            self.assertEqual(entry2["last_thailand_scan_status"], "partial")

    def test_process_lock_serializes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state({"version": 1, "models": {}, "last_top1_key": None}, path)
            errors: list[str] = []

            def worker(i: int) -> None:
                try:
                    def mut(st):
                        st.setdefault("models", {})[f"k{i}"] = {"i": i}
                        time.sleep(0.05)
                        return st

                    with_state(mut, path, timeout=5)
                except Exception as exc:  # noqa: BLE001
                    errors.append(type(exc).__name__)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            st = load_state(path)
            self.assertEqual(len(st["models"]), 4)

    def test_corrupt_state_fail_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{bad", encoding="utf-8")
            st = load_state(path)
            self.assertEqual(st["models"], {})


class ManualAsyncTests(unittest.TestCase):
    def test_manual_compare_creates_job_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deal = _deal()
            with patch("thailand.scanner.run_thailand_scan") as scan:
                out = enqueue_manual_thailand_job(
                    compare=True,
                    chat_id="42",
                    russian_deals=[deal],
                    jobs_dir=root / "jobs",
                )
                scan.assert_not_called()
            self.assertTrue(out["ok"])
            self.assertEqual(out["thailand_scan_status"], "queued")
            path = next((root / "jobs" / "pending").glob("*.json"))
            job = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(job["requested_chat_id"], "42")
            self.assertEqual(job["trigger_type"], "manual_compare")

    def test_manual_does_not_corrupt_automatic_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            deal = _deal()
            from buy_opportunity import evaluate_buy_rules

            sig = evaluate_buy_rules(deal)
            assert sig is not None
            # Seed automatic enqueue state
            st = record_signal_enqueued({}, sig, job_id="auto1", top1_key=sig.model_key)
            save_state(st, state_path)
            before = load_state(state_path)
            enqueue_manual_thailand_job(
                compare=True,
                chat_id="9",
                russian_deals=[deal],
                jobs_dir=root / "jobs",
            )
            after = load_state(state_path)
            self.assertEqual(before, after)

    def test_manual_handler_fast_no_scan(self) -> None:
        from control_bot import handle_thailand_manual

        client = MagicMock()
        with patch(
            "buy_thailand_flow.enqueue_manual_thailand_job"
        ) as enq, patch("control_bot.send_message") as send:
            enq.return_value = {
                "ok": True,
                "thailand_job_id": "j",
                "thailand_scan_status": "queued",
            }
            t0 = time.perf_counter()
            handle_thailand_manual(client, "token", 1, compare=True)
            elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0)
        enq.assert_called()
        self.assertIn("запущена", send.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
