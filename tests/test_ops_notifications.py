"""Synthetic transitions only; Telegram is always replaced by a fake sender."""
from __future__ import annotations

import importlib
import multiprocessing
import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ops_notifications as ops
from admin_notify import maybe_notify_ops_failure
from telegram_sender import SendResult


def result(status="partial", stores=None, **changes):
    values = dict(status=status, store_statuses=stores if stores is not None else {
        "citilink": {"status": "failed", "error": "sanity: got 0 products, expected >=3"},
        "kns": {"status": "ok"}}, error_stage="collection" if status != "success" else None,
        error_message="Partial collection: failed", run_id=1, duration_seconds=1)
    values.update(changes)
    return SimpleNamespace(**values)


class OpsTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ops.json"
        self.messages = []
        messages = self.messages
        class Sender:
            def __init__(self, token, chat):
                self.chat = chat
            def send_message(self, text):
                messages.append((self.chat, text))
                return SendResult(True, message_id=len(messages))
            def close(self):
                pass
        self.sender = Sender
        for target, value in (("config.get_instance_id", "fixture"),
                              ("config.get_telegram_credentials", ("FAKE", "FAKE_CHAT")),
                              ("config.get_telegram_admin_chat_ids", {"FAKE_CHAT"})):
            manager = patch("ops_notifications." + target, return_value=value)
            manager.start()
            self.addCleanup(manager.stop)
        manager = patch("ops_notifications.TelegramSender", Sender)
        manager.start()
        self.addCleanup(manager.stop)

    def notify(self, item):
        return maybe_notify_ops_failure(item, state_path=self.path)

    def test_ok_partial_repeat_recovery_repeat(self):
        self.assertFalse(self.notify(result("success", {})))
        self.assertTrue(self.notify(result()))
        self.assertFalse(self.notify(result(run_id=99, duration_seconds=99)))
        self.assertTrue(self.notify(result("success", {})))
        self.assertFalse(self.notify(result("success", {})))
        self.assertEqual(len(self.messages), 2)
        self.assertIn("восстановлена", self.messages[1][1])

    def test_substantive_changes_not_dynamic_counts(self):
        self.notify(result())
        self.notify(result(stores={"citilink": {"status": "failed", "error": "sanity: got 1 products, expected >=3"}}))
        self.assertEqual(len(self.messages), 1)
        self.notify(result(stores={"citilink": {"status": "failed", "error": "TimeoutError: 123"}}))
        self.notify(result(stores={"kns": {"status": "failed", "error": "TimeoutError: 456"}}))
        self.notify(result("failed", error_stage="database"))
        self.assertEqual(len(self.messages), 4)

    def test_restart_retains_claim_and_has_no_raw_data(self):
        self.notify(result(error_message="bot_token=SECRET chat USER_DATA"))
        text = self.path.read_text()
        for sensitive in ("SECRET", "USER_DATA", "FAKE_CHAT", "sanity", "citilink"):
            self.assertNotIn(sensitive, text)
        # A fresh module load uses the same durable claim, no in-memory cache.
        reloaded = importlib.reload(ops)
        with patch.object(reloaded, "TelegramSender", self.sender):
            self.assertFalse(self.notify(result(run_id=2)))
        self.assertEqual(len(self.messages), 1)

    def test_uncertain_delivery_never_fabricates_ack_or_resends(self):
        with patch.object(self.sender, "send_message", side_effect=TimeoutError("SECRET")) as send:
            self.assertFalse(self.notify(result()))
            self.assertFalse(self.notify(result(run_id=2)))
        send.assert_called_once()
        self.assertEqual(list(json.loads(self.path.read_text())["recipients"].values()), ["unconfirmed"])

    def test_api_ok_without_message_id_is_unconfirmed(self):
        with patch.object(self.sender, "send_message", return_value=SendResult(True)):
            self.assertFalse(self.notify(result()))
        self.assertEqual(list(json.loads(self.path.read_text())["recipients"].values()), ["unconfirmed"])

    def test_crash_after_claim_and_corruption_fail_closed(self):
        with patch.object(self.sender, "send_message", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.notify(result())
        self.assertFalse(self.notify(result()))
        self.path.write_text("not json", encoding="utf-8")
        self.assertFalse(self.notify(result()))
        self.assertEqual(len(self.messages), 0)

    def test_per_recipient_claim_and_return_to_previous_problem(self):
        with patch("ops_notifications.config.get_telegram_admin_chat_ids", return_value={"A", "B"}):
            self.notify(result())
            self.notify(result())
        self.assertEqual(len(self.messages), 2)
        self.notify(result("success", {}))
        self.notify(result())
        self.assertEqual(len(self.messages), 4)

    def test_running_does_not_recover_problem(self):
        self.notify(result())
        self.assertFalse(self.notify(result("running", {})))
        self.assertFalse(self.notify(result()))
        self.assertEqual(len(self.messages), 1)

    def test_concurrent_owner_cannot_send_or_reset_state(self):
        from pipeline_lock import pipeline_lock
        with pipeline_lock(self.path.with_suffix(".lock")):
            self.assertFalse(self.notify(result()))
        self.assertTrue(self.notify(result()))
        self.assertEqual(len(self.messages), 1)

    @unittest.skipIf(os.name == "nt", "Real POSIX process concurrency is checked in Linux")
    def test_processes_share_one_durable_transition_claim(self):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        destination = self.path.with_suffix(".attempts")
        path = self.path
        class Sender:
            def __init__(self, *_):
                pass
            def send_message(self, _):
                with destination.open("a") as handle:
                    handle.write("attempt\n")
                return SendResult(True, message_id=1)
            def close(self):
                pass
        def worker():
            start.wait(5)
            with patch("ops_notifications.TelegramSender", Sender):
                maybe_notify_ops_failure(result(), state_path=path)
        processes = [context.Process(target=worker) for _ in range(6)]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(destination.read_text().splitlines(), ["attempt"])


class OwnershipTests(unittest.TestCase):
    def test_signed_owner_passed_to_receiver_and_no_second_sender(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
const fs = require('fs');
const crypto = require('crypto');
const doc = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const code = n => doc.nodes.find(x => x.name === n).parameters.jsCode;
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
let calls = 0;
const env = { LAPTOP_MONITOR_WEBHOOK_SECRET: 'FIXTURE', LAPTOP_MONITOR_TELEGRAM_BOT_TOKEN: 'FAKE', LAPTOP_MONITOR_TELEGRAM_CHAT_ID: 'FAKE' };
const payload = {event: 'pipeline.completed', status: 'partial', run_id: 1, ops_notifications_owner: 'application'};
const raw = Buffer.from(JSON.stringify(payload));
const ts = String(Math.floor(Date.now()/1000));
const sig = crypto.createHmac('sha256', env.LAPTOP_MONITOR_WEBHOOK_SECRET).update(Buffer.concat([Buffer.from(ts+'.'),raw])).digest('hex');
const input = { first: () => ({json: {headers: {'x-laptop-monitor-timestamp': ts, 'x-laptop-monitor-signature': sig}}, binary: {data: {}}}) };
const helpers = {getBinaryDataBuffer: async () => raw, httpRequest: async () => {calls++; return {ok: true};}};
(async () => {
 const verified = await new AsyncFunction('$input','$env','require',code('Verify HMAC')).call({helpers},input,env,require);
 if (!verified[0].json.verified || verified[0].json.ops_notifications_owner !== 'application') throw Error('owner lost');
 for (let n=0;n<2;n++) {
   const response = await new AsyncFunction('$input','$env','$getWorkflowStaticData',code('Failure alert')).call({helpers}, {first: () => verified[0]},env,() => ({}));
   if (response[0].json.failure_alert !== 'application_owned') throw Error('owner ignored');
 }
 if(calls !== 0) throw Error('duplicate Telegram sender');
 helpers.getBinaryDataBuffer = async () => Buffer.from(JSON.stringify({...payload, run_id: 999}));
 const rejected = await new AsyncFunction('$input','$env','require',code('Verify HMAC')).call({helpers},input,env,require);
 if(rejected[0].json.verified !== false || rejected[0].json.error !== 'bad_signature') throw Error('unsigned ownership trusted');
 // Legacy payloads remain diagnosed; no global guard or workflow disabled.
 const legacy = {json: {status: 'partial', run_id: 2}};
 await new AsyncFunction('$input','$env','$getWorkflowStaticData',code('Failure alert')).call({helpers}, {first: () => legacy},env,() => ({}));
 if(calls !== 1) throw Error('legacy diagnostics removed');
 console.log('PASS signed owner, zero duplicate sends, legacy retained');
})().catch(e => {console.error(e.message);process.exit(1)});
"""
        completed = subprocess.run(["node", "-e", script, str(root / "deploy/n8n/pipeline-event-receiver.json")],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
