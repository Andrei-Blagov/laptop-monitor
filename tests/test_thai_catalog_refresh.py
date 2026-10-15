"""Offline execution of Thai catalog reuse decisions. No live store, CBR, or Telegram."""
from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import config
from thai_catalog import discovery_scope_hash, load_thai_catalog
from thailand.catalog_refresh import (
    BOUNDED_SOURCE_REFRESH_SOURCES,
    DEFERRED,
    FAILED,
    PARTIAL,
    POINT_CHECK_SOURCES,
    POLICY_DISABLED_SOURCES,
    COMPLETED,
    LiveCatalogTransport,
    OfferRefresh,
    execute_catalog_plan,
)
from thailand.catalog_reuse import DISCOVER, REUSE, REVALIDATE, evaluate_catalog_reuse
from thailand.discovery import GPU_SEARCH_QUERIES
from thailand.scanner import run_thailand_scan
from thailand.storage import atomic_write_json
from tests.test_thai_catalog_reuse import NOW, _fx, _offer, _publish, _result, _russian

PRODUCTION_HEALTH = Path("data/thailand_source_health.json")


class BombTransport:
    def refresh_offer(self, item, *, mode, now):
        raise AssertionError(f"unexpected refresh {mode} {item.get('external_id')}")

    def discover_source(self, source, *, now):
        raise AssertionError(f"unexpected discover {source}")


class RecordingTransport:
    def __init__(self, refreshes=None, discoveries=None):
        self.calls = []
        self.refreshes = refreshes or {}
        self.discoveries = discoveries or {}
        self.discover_calls = []

    def refresh_offer(self, item, *, mode, now):
        external_id = item["external_id"]
        self.calls.append((mode, item["source"], external_id))
        refresh = self.refreshes.get(external_id)
        if refresh is None:
            refresh = OfferRefresh(
                source=item["source"], external_id=external_id, variant_id=item.get("variant_id"),
                offer_id=item.get("offer_id"), status="updated", price_thb=75_000,
                availability_state="in_stock", availability_confirmed=True, observed_at=now,
                http_calls=1, coverage=f"{mode}:{item['source']}",
            )
        refresh.http_calls = 1
        return refresh

    def discover_source(self, source, *, now):
        self.discover_calls.append(source)
        if source in self.discoveries:
            return self.discoveries[source]
        result = _result([_offer(f"{source}-1", f"SKU-{source[:4].upper()}17", 70_000, now, store=source)], store=source)
        result.request_count = len(GPU_SEARCH_QUERIES)
        return result


class Response:
    def __init__(self, status, body, *, json_body=None, headers=None):
        self.status_code = status
        self.text = body
        self.headers = headers or {"content-type": "application/json"}
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class OneShotClient:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(str(url))
        if len(self.urls) > 1:
            raise AssertionError(url)
        return self.response

    def close(self):
        return None


class CatalogRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._health_dir = tempfile.TemporaryDirectory()
        self.health_path = Path(self._health_dir.name) / "health.json"
        atomic_write_json(self.health_path, {"version": 1, "stores": {}})
        self._health_patch = patch.object(config, "THAILAND_SOURCE_HEALTH_PATH", str(self.health_path))
        self._health_patch.start()
        self._production_before = PRODUCTION_HEALTH.read_bytes() if PRODUCTION_HEALTH.exists() else None

    def tearDown(self) -> None:
        self._health_patch.stop()
        self._health_dir.cleanup()
        after = PRODUCTION_HEALTH.read_bytes() if PRODUCTION_HEALTH.exists() else None
        self.assertEqual(after, self._production_before)

    def _decision(self, root: Path, **kwargs):
        values = {
            "now": NOW,
            "instance_id": "c22-test",
            "expected_scope_hash": discovery_scope_hash(),
            "health_path": self.health_path,
        }
        values.update(kwargs)
        return evaluate_catalog_reuse(root, **values)

    def test_fresh_catalog_makes_no_http_call(self) -> None:
        observed = NOW - timedelta(hours=2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-fresh", "SKU-FRESH17", 80_000, observed)], observed=observed)
            before = (root / "latest.json").read_bytes()
            snapshot_id = load_thai_catalog(root, expected_instance_id="c22-test")["snapshot_id"]
            decision = self._decision(root, fx=_fx(), russian_snapshot=_russian())
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, fx=_fx("2.8"), russian_snapshot=_russian(),
                transport=BombTransport(),
            )
            self.assertEqual(decision["action"], REUSE)
            self.assertEqual(outcome["status"], COMPLETED)
            self.assertEqual(outcome["http_calls"], 0)
            self.assertEqual(outcome["snapshot_id"], snapshot_id)
            self.assertEqual((root / "latest.json").read_bytes(), before)
            self.assertEqual(len(list((root / "snapshots").glob("*.json"))), 1)
            self.assertIsNotNone(outcome["views"]["general_top"])
            self.assertNotEqual(
                decision["rankings"]["thai_top_value"][0]["price_rub"],
                outcome["rankings"]["thai_top_value"][0]["price_rub"],
            )

    def test_revalidate_checks_only_named_offers(self) -> None:
        stale = NOW - timedelta(hours=9)
        fresh = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [
                _offer("th-a", "SKU-AAA017", 80_000, stale),
                _offer("th-b", "SKU-BBB017", 81_000, stale),
                _offer("th-c", "SKU-CCC017", 82_000, fresh),
            ], observed=stale, discovered=fresh)
            decision = self._decision(root)
            transport = RecordingTransport()
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, fx=_fx(), transport=transport,
            )
        self.assertEqual(decision["action"], REVALIDATE)
        self.assertEqual(sorted(item[2] for item in transport.calls), ["th-a", "th-b"])
        self.assertTrue(all(item[0] == "point" for item in transport.calls))
        self.assertEqual(transport.discover_calls, [])
        self.assertEqual(outcome["http_calls"], 2)
        self.assertEqual(outcome["status"], COMPLETED)

    def test_one_refresh_keeps_other_timestamps_and_spec_evidence(self) -> None:
        stale = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [
                _offer("th-a", "SKU-AAA017", 80_000, stale),
                _offer("th-b", "SKU-BBB017", 81_000, stale),
            ], observed=stale)
            original = load_thai_catalog(root, expected_instance_id="c22-test")
            original_discovery = original["discovery_checked_at"]
            spec_time = original["offers"][0]["specs"]["cpu"]["evidence"]["captured_at"]
            kept = next(row for row in original["offers"] if row["external_id"] == "th-b")
            decision = self._decision(root)
            transport = RecordingTransport(refreshes={
                "th-a": OfferRefresh(
                    source="jib", external_id="th-a", status="updated", price_thb=75_000,
                    availability_state="in_stock", availability_confirmed=True, observed_at=NOW,
                    http_calls=1, observed_specs={"cpu": "Intel Core Ultra 9 285HX"},
                ),
                "th-b": OfferRefresh(source="jib", external_id="th-b", status="missing", reason="not_observed_on_recheck", http_calls=1),
            })
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, fx=_fx(), transport=transport,
            )
            saved = load_thai_catalog(root, expected_instance_id="c22-test")
            changed = next(row for row in saved["offers"] if row["external_id"] == "th-a")
            untouched = next(row for row in saved["offers"] if row["external_id"] == "th-b")
            snapshot_count = len(list((root / "snapshots").glob("*.json")))
            stored_cpu = next(row for row in original["offers"] if row["external_id"] == "th-a")["specs"]["cpu"]["value"]
        self.assertEqual(outcome["status"], PARTIAL)
        self.assertEqual(saved["parent_snapshot_id"], original["snapshot_id"])
        self.assertNotEqual(saved["snapshot_id"], original["snapshot_id"])
        self.assertEqual(saved["discovery_checked_at"], original_discovery)
        self.assertEqual(changed["price_thb"], 75_000)
        self.assertEqual(changed["price_checked_at"], NOW.isoformat())
        self.assertEqual(changed["specs"]["cpu"]["evidence"]["captured_at"], spec_time)
        self.assertEqual(changed["specs"]["cpu"]["value"], stored_cpu)
        self.assertTrue(any(row.get("spec_conflict") for row in saved["refresh_diagnostics"]))
        self.assertEqual(untouched["price_checked_at"], kept["price_checked_at"])
        self.assertEqual(untouched["availability_checked_at"], kept["availability_checked_at"])
        self.assertGreaterEqual(snapshot_count, 2)

    def test_missing_point_check_does_not_start_broad_search(self) -> None:
        stale = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("adv-1", "SKU-ADV017", 80_000, stale, store="advice")], observed=stale)
            decision = self._decision(root)
            client = OneShotClient(Response(200, "{}", json_body={}))
            with patch("thailand.advice.collect", side_effect=AssertionError("broad advice scan")), \
                 patch("thailand.jib.collect", side_effect=AssertionError("broad jib scan")):
                outcome = execute_catalog_plan(
                    decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                    health_path=self.health_path, transport=LiveCatalogTransport(client=client),
                )
        self.assertEqual(decision["allowed_sources"], ["advice"])
        self.assertEqual(len(client.urls), 1)
        self.assertIn("adv-1", client.urls[0])
        self.assertNotIn("notebook+rtx", client.urls[0].lower())
        self.assertEqual(outcome["http_calls"], 1)
        self.assertNotIn("th-broad", outcome["errors"])

    def test_itcity_without_a_single_request_is_not_a_hidden_scan(self) -> None:
        stale = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("it-1", "SKU-ITC017", 80_000, stale, store="itcity")], observed=stale)
            decision = self._decision(root)
            with patch("thailand.itcity.collect", side_effect=AssertionError("broad itcity scan")):
                outcome = execute_catalog_plan(
                    decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                    health_path=self.health_path, transport=LiveCatalogTransport(client=OneShotClient(Response(200, "{}"))),
                )
        self.assertEqual(outcome["http_calls"], 0)
        self.assertEqual(outcome["status"], DEFERRED)
        self.assertIn("itcity", BOUNDED_SOURCE_REFRESH_SOURCES)
        self.assertNotIn("itcity", POINT_CHECK_SOURCES)

    def test_discover_is_one_broad_pass_without_the_russian_top(self) -> None:
        observed = NOW - timedelta(hours=27)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD017", 80_000, observed)], observed=observed)
            decision = self._decision(root, russian_snapshot=_russian())
            transport = RecordingTransport()
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, russian_snapshot=_russian(), fx=_fx(),
                transport=transport,
            )
            saved = load_thai_catalog(root, expected_instance_id="c22-test")
        self.assertEqual(decision["action"], DISCOVER)
        self.assertEqual(transport.discover_calls, decision["allowed_sources"])
        self.assertEqual(set(transport.discover_calls), {"jib", "advice", "speedcom", "invadeit", "itcity"})
        self.assertTrue(POLICY_DISABLED_SOURCES.isdisjoint(transport.discover_calls))
        self.assertTrue(any(row["external_id"].endswith("-1") and row["external_id"] != "ru-17" for row in saved["offers"]))
        self.assertGreater(outcome["http_calls"], 0)
        self.assertEqual(saved["parent_snapshot_id"], decision["snapshot_id"])
        self.assertIn("notebook rtx 5070 ti", saved["source_coverage"][0]["completed_keys"] or GPU_SEARCH_QUERIES)

    def test_rate_limit_stops_further_calls_to_that_source(self) -> None:
        observed = NOW - timedelta(hours=27)
        limited = _result([], store="jib", rate_limited=True)
        limited.request_count = 1

        class Stop:
            def __init__(self):
                self.jib = 0
                self.calls = []

            def discover_source(self, source, *, now):
                self.calls.append(source)
                if source == "jib":
                    self.jib += 1
                    if self.jib > 1:
                        raise AssertionError("jib was called after 429")
                    return limited
                result = _result([_offer(f"{source}-1", f"SKU-{source[:4].upper()}17", 70_000, now, store=source)], store=source)
                result.request_count = 1
                return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD017", 80_000, observed)], observed=observed)
            decision = self._decision(root)
            transport = Stop()
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, transport=transport,
            )
        self.assertEqual(transport.jib, 1)
        self.assertEqual(outcome["status"], PARTIAL)
        self.assertEqual(transport.calls.count("jib"), 1)

    def test_circuit_and_retry_are_rechecked_before_http(self) -> None:
        stale = NOW - timedelta(hours=9)
        until = (NOW + timedelta(hours=2)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [
                _offer("th-a", "SKU-AAA017", 80_000, stale),
                _offer("th-b", "SKU-BBB017", 81_000, stale),
            ], observed=stale)
            decision = self._decision(root)
            self.assertEqual(decision["action"], REVALIDATE)
            atomic_write_json(self.health_path, {"version": 1, "stores": {"jib": {
                "disabled_until": until, "consecutive_failures": 3,
            }}})
            blocked = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, transport=BombTransport(),
            )
            atomic_write_json(self.health_path, {"version": 1, "stores": {"jib": {
                "rate_limit_until": until,
            }}})
            limited = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, transport=BombTransport(),
            )
        self.assertEqual(blocked["status"], DEFERRED)
        self.assertEqual(blocked["http_calls"], 0)
        self.assertEqual(blocked["next_allowed_at"], until)
        self.assertEqual(limited["status"], DEFERRED)
        self.assertEqual(limited["http_calls"], 0)
        self.assertEqual(limited["next_allowed_at"], until)

    def test_disabled_stores_receive_no_requests(self) -> None:
        observed = NOW - timedelta(hours=27)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD017", 80_000, observed)], observed=observed)
            decision = self._decision(root)
            decision["allowed_sources"] = ["banana", "lazada", *decision["allowed_sources"]]
            transport = RecordingTransport()
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, transport=transport,
            )
        self.assertEqual(decision["action"], DISCOVER)
        self.assertTrue(POLICY_DISABLED_SOURCES.isdisjoint(transport.discover_calls))
        self.assertGreater(outcome["http_calls"], 0)
        self.assertTrue(all(row["source"] not in POLICY_DISABLED_SOURCES for row in outcome["skipped_offers"] if row.get("reason") != "source_disabled"))

    def test_partial_429_keeps_previous_good_observations(self) -> None:
        stale = NOW - timedelta(hours=9)
        fresh = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(
                root,
                [_offer("th-old", "SKU-OLD017", 80_000, stale)],
                observed=stale,
                discovered=fresh,
                extra=[_result([_offer("adv-fresh", "SKU-ADV017", 70_000, fresh, store="advice")], store="advice")],
            )
            original = load_thai_catalog(root, expected_instance_id="c22-test")
            advice = next(row for row in original["offers"] if row["external_id"] == "adv-fresh")
            decision = self._decision(root)
            transport = RecordingTransport(refreshes={
                "th-old": OfferRefresh(source="jib", external_id="th-old", status="rate_limited", reason="retry_after", http_calls=1, retry_after_seconds=120),
            })
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, transport=transport,
            )
            saved = load_thai_catalog(root, expected_instance_id="c22-test")
            kept = next(row for row in saved["offers"] if row["external_id"] == "adv-fresh")
            old = next(row for row in saved["offers"] if row["external_id"] == "th-old")
        self.assertEqual(outcome["status"], PARTIAL)
        self.assertEqual(kept["price_checked_at"], advice["price_checked_at"])
        self.assertEqual(old["price_checked_at"], next(row["price_checked_at"] for row in original["offers"] if row["external_id"] == "th-old"))
        self.assertEqual(saved["discovery_checked_at"], original["discovery_checked_at"])

    def test_publish_failure_keeps_latest(self) -> None:
        stale = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD017", 80_000, stale)], observed=stale)
            before = (root / "latest.json").read_bytes()
            previous = load_thai_catalog(root, expected_instance_id="c22-test")["snapshot_id"]
            decision = self._decision(root)

            def boom(_point):
                raise OSError("disk")

            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, transport=RecordingTransport(), publish_barrier=boom,
            )
            self.assertEqual((root / "latest.json").read_bytes(), before)
            self.assertEqual(load_thai_catalog(root, expected_instance_id="c22-test")["snapshot_id"], previous)
        self.assertEqual(outcome["status"], FAILED)
        self.assertIn("publish_failed:OSError", outcome["errors"])

    def test_oos_and_unverified_are_not_purchase_quotes(self) -> None:
        stale = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [
                _offer("th-oos", "SKU-OOS017", 80_000, stale),
                _offer("th-unk", "SKU-UNK017", 81_000, stale),
            ], observed=stale)
            decision = self._decision(root)
            transport = RecordingTransport(refreshes={
                "th-oos": OfferRefresh(
                    source="jib", external_id="th-oos", status="updated", price_thb=70_000,
                    availability_state="out_of_stock", availability_confirmed=True, observed_at=NOW, http_calls=1,
                ),
                "th-unk": OfferRefresh(source="jib", external_id="th-unk", status="unverified", reason="unconfirmed", http_calls=1),
            })
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, fx=_fx(), transport=transport,
            )
        identifiers = []
        for row in outcome["rankings"]["thai_top_value"]:
            identifiers.extend(offer["external_id"] for offer in row.get("offers") or [])
        self.assertNotIn("th-oos", identifiers)
        self.assertNotIn("th-unk", identifiers)

    def test_jib_point_check_uses_the_product_page(self) -> None:
        stale = NOW - timedelta(hours=9)
        html = (
            "<html><head><title>ASUS ROG Strix SKU-OLD17</title></head><body>"
            "<h1>ASUS ROG Strix SKU-OLD17</h1> salePrice: 75000 qty = 4 "
            + (" " * 40)
            + "</body></html>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD17", 80_000, stale)], observed=stale)
            original_spec = load_thai_catalog(root, expected_instance_id="c22-test")["offers"][0]["specs"]["cpu"]["evidence"]["captured_at"]
            decision = self._decision(root)
            client = OneShotClient(Response(200, html, headers={"content-type": "text/html"}))
            with patch("thailand.jib.collect", side_effect=AssertionError("broad jib scan")):
                outcome = execute_catalog_plan(
                    decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                    health_path=self.health_path, fx=_fx(), transport=LiveCatalogTransport(client=client),
                )
            saved = load_thai_catalog(root, expected_instance_id="c22-test")
        self.assertIn("/web/product/readProduct/th-old", client.urls[0])
        self.assertNotIn("search_suggestion", client.urls[0])
        self.assertEqual(saved["offers"][0]["price_thb"], 75_000)
        self.assertEqual(saved["offers"][0]["specs"]["cpu"]["evidence"]["captured_at"], original_spec)
        self.assertEqual(outcome["http_calls"], 1)
        self.assertTrue(outcome["rankings"]["thai_top_value"])

    def test_bounded_urls_name_one_saved_identity(self) -> None:
        from thailand.catalog_refresh import _bounded_url

        urls = {
            "advice": _bounded_url("advice", {"external_id": "adv-1"}),
            "speedcom": _bounded_url("speedcom", {"external_id": "sp-1"}),
            "invadeit": _bounded_url("invadeit", {"external_id": "iv-1"}),
        }
        self.assertIsNone(_bounded_url("itcity", {"external_id": "it-1"}))
        self.assertIsNone(_bounded_url("jib", {"external_id": "th-1"}))
        for url in urls.values():
            self.assertNotIn("notebook", url.lower())
            self.assertEqual(url.count("?"), 1 if "?" in url else 0)
        self.assertIn("/search?keyword=adv-1", urls["advice"])
        self.assertIn("/search/suggest.json?q=sp-1", urls["speedcom"])
        self.assertIn("/api/products/iv-1/specifications", urls["invadeit"])

    def test_legacy_scan_and_russian_ranking_are_untouched(self) -> None:
        self.assertIsNone(inspect.signature(run_thailand_scan).parameters["catalog_root"].default)
        scanner = Path("thailand/scanner.py").read_text(encoding="utf-8")
        ranking = Path("deal_ranking.py").read_text(encoding="utf-8")
        version = Path("VERSION").read_text(encoding="utf-8").strip()
        self.assertNotIn("catalog_refresh", scanner)
        self.assertNotIn("execute_catalog_plan", scanner)
        self.assertNotIn("catalog_refresh", ranking)
        self.assertEqual(version, "0.10.2")

    def test_second_offer_is_blocked_when_health_changes_mid_pass(self) -> None:
        stale = NOW - timedelta(hours=9)
        until = (NOW + timedelta(hours=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [
                _offer("th-a", "SKU-AAA017", 80_000, stale),
                _offer("th-b", "SKU-BBB017", 81_000, stale),
            ], observed=stale)
            decision = self._decision(root)

            class Flip(RecordingTransport):
                def refresh_offer(self, item, *, mode, now):
                    if item["external_id"] == "th-b":
                        raise AssertionError("second offer was requested after the breaker opened")
                    atomic_write_json(self_path, {"version": 1, "stores": {"jib": {
                        "disabled_until": until, "consecutive_failures": 3,
                    }}})
                    return super().refresh_offer(item, mode=mode, now=now)

            self_path = self.health_path
            outcome = execute_catalog_plan(
                decision, snapshot_root=root, now=NOW, instance_id="c22-test",
                health_path=self.health_path, transport=Flip(),
            )
        self.assertEqual(outcome["http_calls"], 1)
        self.assertEqual([row["external_id"] for row in outcome["skipped_offers"]], ["th-b"])
        self.assertEqual(outcome["skipped_offers"][0]["next_allowed_at"], until)


if __name__ == "__main__":
    unittest.main()
