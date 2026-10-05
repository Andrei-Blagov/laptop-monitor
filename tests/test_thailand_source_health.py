from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from thailand.errors import NO_RESULTS
from thailand.models import StoreScanResult
from thailand.scanner import derive_scan_status
from thailand.source_health import (
    circuit_status,
    load_health,
    record_observation,
    should_skip,
)


def _now() -> datetime:
    return datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class SourceHealthTests(unittest.TestCase):
    def test_success_resets_failure_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            record_observation("jib", ok=False, error_code="TIMEOUT", now=_now(), path=path)
            record_observation("jib", ok=True, offer_count=2, now=_now(), path=path)
            entry = load_health(path)["stores"]["jib"]
            self.assertEqual(entry["consecutive_failures"], 0)
            self.assertIsNone(entry["disabled_until"])

    def test_one_failure_does_not_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            record_observation("jib", ok=False, error_code="BLOCKED", now=_now(), path=path)
            self.assertEqual(load_health(path)["stores"]["jib"]["consecutive_failures"], 1)
            self.assertFalse(should_skip("jib", now=_now(), path=path))

    def test_threshold_sets_disabled_until(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            for _ in range(3):
                record_observation("speedcom", ok=False, error_code="BLOCKED", now=_now(), path=path)
            entry = load_health(path)["stores"]["speedcom"]
            self.assertGreaterEqual(entry["consecutive_failures"], 3)
            self.assertIsNotNone(entry["disabled_until"])
            self.assertEqual(circuit_status("speedcom", now=_now(), path=path), "open")

    def test_skipped_while_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            for _ in range(3):
                record_observation("speedcom", ok=False, error_code="TIMEOUT", now=_now(), path=path)
            self.assertTrue(should_skip("speedcom", now=_now() + timedelta(hours=1), path=path))

    def test_probe_after_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            later = _now() + timedelta(hours=25)
            for _ in range(3):
                record_observation("itcity", ok=False, error_code="HTTP_ERROR", now=_now(), path=path)
            self.assertEqual(circuit_status("itcity", now=later, path=path), "half_open")
            self.assertFalse(should_skip("itcity", now=later, path=path))

    def test_success_after_cooldown_resets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            later = _now() + timedelta(hours=25)
            for _ in range(3):
                record_observation("itcity", ok=False, error_code="HTTP_ERROR", now=_now(), path=path)
            record_observation("itcity", ok=True, offer_count=1, now=later, path=path)
            entry = load_health(path)["stores"]["itcity"]
            self.assertEqual(entry["consecutive_failures"], 0)
            self.assertIsNone(entry["disabled_until"])

    def test_manual_force_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            for _ in range(3):
                record_observation("banana", ok=False, error_code="BLOCKED", now=_now(), path=path)
            self.assertTrue(should_skip("banana", now=_now(), path=path))
            self.assertFalse(should_skip("banana", now=_now(), path=path, force=True))

    def test_disabled_store_not_partial(self) -> None:
        status = derive_scan_status(
            [
                StoreScanResult(store="jib", ok=True, offers=[]),
                StoreScanResult(store="banana", ok=True, policy_disabled=True),
                StoreScanResult(store="lazada", ok=True, circuit_breaker_status="open"),
            ]
        )
        self.assertEqual(status, "ok")

    def test_no_results_not_circuit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            record_observation("advice", ok=False, error_code="TIMEOUT", now=_now(), path=path)
            record_observation(
                "advice", ok=True, error_code=NO_RESULTS, offer_count=0, now=_now(), path=path
            )
            entry = load_health(path)["stores"]["advice"]
            self.assertEqual(entry["consecutive_failures"], 0)

    def test_health_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            record_observation("jib", ok=True, offer_count=1, runtime_seconds=1.2, now=_now(), path=path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["stores"]["jib"]["last_offer_count"], 1)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_corrupt_health_state_fail_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            path.write_text("{not-json", encoding="utf-8")
            self.assertEqual(load_health(path)["stores"], {})
            record_observation("jib", ok=True, now=_now(), path=path)
            self.assertIn("jib", load_health(path)["stores"])

    def test_open_breaker_skips_collect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            for _ in range(int(config.THAILAND_SOURCE_FAILURE_THRESHOLD)):
                record_observation("jib", ok=False, error_code="BLOCKED", now=_now(), path=path)
            called = {"n": 0}

            class Adapter:
                slug = "jib"
                enabled = True

                def collect(self, **kwargs):
                    called["n"] += 1
                    return StoreScanResult(store="jib", ok=True)

            with patch("thailand.scanner.get_thailand_adapters", return_value=[Adapter()]), patch(
                "thailand.source_health.health_file", return_value=path
            ):
                from thailand.scanner import collect_thailand_offers

                results, offers, _unverified, _dur = collect_thailand_offers(parallel=False)
            self.assertEqual(called["n"], 0)
            self.assertEqual(offers, [])
            self.assertEqual(results[0].collection_mode, "skipped")
            self.assertEqual(results[0].circuit_breaker_status, "open")


if __name__ == "__main__":
    unittest.main()
