"""Offline reuse decisions for a saved Thai catalog. No store HTTP."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config

from thai_catalog import SNAPSHOT_DIRNAME, discovery_scope_hash, export_thai_catalog, load_thai_catalog
from thailand.catalog_reuse import (
    CATALOG_HOURS,
    DAILY_MIN_HOURS,
    DEFERRED,
    DISCOVER,
    QUOTE_HOURS,
    REUSE,
    REVALIDATE,
    evaluate_catalog_reuse,
)
from thailand.discovery import GPU_SEARCH_QUERIES
from thailand.models import StoreScanResult, ThailandOffer
from thailand.scanner import run_thailand_scan
from thailand.storage import atomic_write_json

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _evidence(when: datetime) -> dict:
    entry = {"method": "structured_api", "captured_at": when.isoformat()}
    return {name: dict(entry) for name in ("cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")}


def _offer(external_id: str, sku: str, price: int, when: datetime, *, store: str = "jib") -> ThailandOffer:
    return ThailandOffer(
        store=store,
        external_id=external_id,
        name=f"ASUS ROG Strix {sku}",
        url=f"https://example.test/{external_id}",
        price_thb=price,
        available=True,
        collected_at=when,
        sku=sku,
        manufacturer_part_number=sku,
        brand="ASUS",
        cpu="Intel Core Ultra 9 275HX",
        gpu="RTX 5070 Ti",
        gpu_source="structured_api",
        ram_gb=32,
        ssd_gb=1024,
        screen_size_inch=16.0,
        screen_resolution="2560x1600",
        availability_status="in_stock",
        availability_confirmed=True,
        availability_source="structured_api",
        price_source="structured_api",
        price_verified_for_variant=True,
        channel="direct",
        metadata={"field_evidence": _evidence(when)},
    )


def _result(offers: list[ThailandOffer], *, store: str = "jib", rate_limited: bool = False) -> StoreScanResult:
    return StoreScanResult(
        store=store,
        ok=not rate_limited,
        offers=offers,
        queries_finished=["notebook rtx 5070 ti"] if rate_limited else list(GPU_SEARCH_QUERIES),
        discovery_queries=list(GPU_SEARCH_QUERIES),
        request_count=1 if rate_limited else len(GPU_SEARCH_QUERIES),
        error_code="RATE_LIMITED" if rate_limited else None,
        discovery_rate_limited=rate_limited,
        collection_mode="rate_limited" if rate_limited else "http",
        retry_after_seconds=3600 if rate_limited else None,
    )


def _fx(rate: str = "2.5") -> dict:
    return {
        "fx_id": f"cbr-{rate}",
        "source": "CBR",
        "currency": "THB",
        "nominal": 1,
        "rub_per_thb": rate,
        "fetched_at": NOW.isoformat(),
        "published_date": "2026-10-14",
        "error": None,
    }


def _publish(
    root: Path,
    offers: list[ThailandOffer],
    *,
    observed: datetime,
    discovered: datetime | None = None,
    extra: list[StoreScanResult] | None = None,
    retry_after_seconds: int | None = None,
) -> dict:
    results = [_result(offers)]
    if retry_after_seconds is not None:
        results[0].retry_after_seconds = retry_after_seconds
    if extra:
        results.extend(extra)
    checked = discovered or observed
    return export_thai_catalog(
        results,
        snapshot_root=root,
        now=max(checked, observed, key=lambda item: item),
        discovery_checked_at=checked,
        instance_id="c22-test",
        started_at=checked,
    )


def _russian(top: list[str] | None = None) -> dict:
    observed = (NOW - timedelta(hours=1)).isoformat()
    evidence = {
        "source": "regard",
        "external_id": "ru-17",
        "captured_at": observed,
        "method": "specs_cache",
        "capture_state": "known",
    }
    specs = {
        "cpu": "Intel Core Ultra 9 275HX",
        "gpu": "RTX 5070 Ti",
        "ram_gb": 32,
        "ssd_gb": 1024,
        "screen_inch": 16,
        "screen_resolution": "2560x1600",
    }
    offers = []
    for index in range(1, 26):
        code = "SKU-MATCH17" if index == 17 else f"SKU-RU-{index:02d}"
        eid = f"ru-{index:02d}"
        row_evidence = {**evidence, "external_id": eid}
        offers.append({
            "offer_id": eid,
            "external_id": eid,
            "store": "regard",
            "model_id": f"model-{eid}",
            "sku_candidates": [code],
            "mpn_candidates": [],
            "public_price_rub": 250_000,
            "price_checked_at": observed,
            "availability_checked_at": observed,
            "observed_at": observed,
            "stock_state": "in_stock",
            "availability_evidence": {**row_evidence, "method": "store_listing", "available": True},
            "product_name": f"ASUS ROG Strix {code}",
            "specs": {name: {"value": value, "evidence": row_evidence} for name, value in specs.items()},
        })
    return {
        "schema_version": 1,
        "snapshot_id": "ru-" + "-".join(top or ["default"]),
        "instance_id": "c22-test",
        "created_at": NOW.isoformat(),
        "offers": offers,
        "source_coverage": [{"store": "regard", "state": "ok", "latest_attempt_at": observed}],
        "top_model_ids": top or [f"model-ru-{index:02d}" for index in range(1, 11)],
        "personal_profiles": [],
    }


class CatalogReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._health_dir = tempfile.TemporaryDirectory()
        self.health_path = Path(self._health_dir.name) / "health.json"
        atomic_write_json(self.health_path, {"version": 1, "stores": {}})
        self._health_patch = patch.object(config, "THAILAND_SOURCE_HEALTH_PATH", str(self.health_path))
        self._health_patch.start()

    def tearDown(self) -> None:
        self._health_patch.stop()
        self._health_dir.cleanup()

    def test_fresh_catalog_recomputes_without_http(self) -> None:
        observed = NOW - timedelta(hours=5)
        offers = [_offer(f"th-{index}", f"SKU-TH-{index}", 70000 + index * 1000, observed) for index in range(1, 7)]
        offers.append(_offer("th-match", "SKU-MATCH17", 80_000, observed))
        with tempfile.TemporaryDirectory() as tmp, patch("thailand.jib.collect") as stores:
            root = Path(tmp)
            _publish(root, offers, observed=observed)
            before = (root / "latest.json").read_bytes()
            snapshot = (root / "snapshots").glob("*.json")
            body = next(snapshot).read_bytes()
            decision = evaluate_catalog_reuse(
                root,
                now=NOW,
                instance_id="c22-test",
                expected_scope_hash=discovery_scope_hash(),
                russian_snapshot=_russian(),
                fx=_fx(),
            )
            self.assertEqual(decision["action"], REUSE)
            self.assertEqual(decision["http_calls"], 0)
            self.assertFalse(decision["refresh_allowed"])
            self.assertEqual(decision["top_count"] if "top_count" in decision else len(decision["rankings"]["thai_top_value"]), 5)
            self.assertGreaterEqual(len(decision["rankings"]["eligible_models"]), 6)
            self.assertEqual(decision["comparison"]["ru_snapshot_id"], _russian()["snapshot_id"])
            confirmed = [pair["ru_offer_id"] for pair in decision["comparison"]["pairs"] if pair["comparison_status"] == "confirmed"]
            self.assertEqual(confirmed, ["ru-17"])
            self.assertEqual((root / "latest.json").read_bytes(), before)
            self.assertEqual(next((root / "snapshots").glob("*.json")).read_bytes(), body)
            stores.assert_not_called()

    def test_nine_hours_revalidates_without_a_full_scan(self) -> None:
        observed = NOW - timedelta(hours=9)
        offers = [_offer("th-old", "SKU-OLD", 80_000, observed)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, offers, observed=observed)
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test",
                expected_scope_hash=discovery_scope_hash(), trigger="daily", fx=_fx(),
            )
        self.assertEqual(decision["action"], REVALIDATE)
        self.assertEqual(decision["discover_reasons"], [])
        self.assertEqual(decision["revalidate_offers"][0]["external_id"], "th-old")
        self.assertEqual(decision["revalidate_offers"][0]["source"], "jib")
        self.assertIn("price_not_current", decision["revalidate_offers"][0]["reason"])
        self.assertNotIn("th-old", {row["external_id"] for row in _ids(decision["rankings"])})

    def test_twenty_seven_hours_discovers(self) -> None:
        observed = NOW - timedelta(hours=27)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-1", "SKU-1", 80_000, observed)], observed=observed)
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
            )
        self.assertEqual(decision["action"], DISCOVER)
        self.assertIn("discovery_checked_at_outside_26h", decision["discover_reasons"])
        self.assertIsNone(decision["rankings"])

    def test_russian_top_change_does_not_rescan(self) -> None:
        observed = NOW - timedelta(hours=2)
        with tempfile.TemporaryDirectory() as tmp, patch("thailand.jib.collect") as stores:
            root = Path(tmp)
            _publish(root, [_offer("th-match", "SKU-MATCH17", 80_000, observed)], observed=observed)
            first = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
                russian_snapshot=_russian(["model-ru-01"]), fx=_fx(),
            )
            second = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
                russian_snapshot=_russian(["model-ru-17"]), fx=_fx("2.8"),
            )
        self.assertEqual(first["action"], REUSE)
        self.assertEqual(second["action"], REUSE)
        self.assertNotEqual(first["rankings"]["thai_top_value"][0]["price_rub"], second["rankings"]["thai_top_value"][0]["price_rub"])
        self.assertEqual(
            first["rankings"]["all_offers"][0]["price_checked_at"],
            second["rankings"]["all_offers"][0]["price_checked_at"],
        )
        stores.assert_not_called()

    def test_bad_hash_instance_and_scope_are_not_reused(self) -> None:
        observed = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-1", "SKU-1", 80_000, observed)], observed=observed)
            wrong_scope = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash="other-scope",
            )
            wrong_instance = evaluate_catalog_reuse(
                root, now=NOW, instance_id="other-instance", expected_scope_hash=discovery_scope_hash(),
            )
            latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            latest["sha256"] = "0" * 64
            atomic_write_json(root / "latest.json", latest)
            bad_hash = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
            )
        self.assertEqual(wrong_scope["action"], DISCOVER)
        self.assertIn("discovery_scope_incompatible", wrong_scope["reasons"])
        self.assertEqual(wrong_instance["action"], DISCOVER)
        self.assertEqual(bad_hash["action"], DISCOVER)
        self.assertNotEqual(wrong_scope["action"], REUSE)
        self.assertNotEqual(wrong_instance["action"], REUSE)
        self.assertNotEqual(bad_hash["action"], REUSE)

    def test_partial_source_is_not_reported_as_absence(self) -> None:
        observed = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(
                root,
                [_offer("th-1", "SKU-1", 80_000, observed)],
                observed=observed,
                extra=[_result([], store="advice", rate_limited=True)],
            )
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(), fx=_fx(),
            )
        self.assertEqual(decision["action"], REUSE)
        self.assertTrue(any("429" in item for item in decision["warnings"]))
        self.assertNotIn("market_empty", " ".join(decision["warnings"]))
        self.assertTrue(decision["rankings"]["all_offers"])

    def test_circuit_blocks_early_refresh(self) -> None:
        observed = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            atomic_write_json(health, {"version": 1, "stores": {"jib": {
                "disabled_until": (NOW + timedelta(hours=2)).isoformat(),
                "consecutive_failures": 3,
            }}})
            _publish(root, [_offer("th-old", "SKU-OLD", 80_000, observed)], observed=observed)
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
                health_path=health,
            )
        self.assertEqual(decision["action"], DEFERRED)
        self.assertEqual(decision["revalidate_offers"], [])
        self.assertEqual(decision["deferred_offers"][0]["block_reason"], "circuit")
        self.assertFalse(decision["refresh_allowed"])

    def test_thai_top_works_without_russia(self) -> None:
        observed = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer(f"th-{index}", f"SKU-{index}", 70000 + index * 1000, observed) for index in range(1, 7)], observed=observed)
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(), fx=_fx(),
            )
        self.assertEqual(decision["action"], REUSE)
        self.assertIsNone(decision["comparison"])
        self.assertEqual(len(decision["rankings"]["thai_top_value"]), 5)
        self.assertGreaterEqual(len(decision["rankings"]["eligible_models"]), 6)

    def test_fresh_export_does_not_make_an_old_price_purchasable(self) -> None:
        offer_time = NOW - timedelta(hours=9)
        discovered = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD17", 80_000, offer_time)], observed=offer_time, discovered=discovered)
            before = next((root / "snapshots").glob("*.json")).read_text(encoding="utf-8")
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(), fx=_fx(),
            )
            after = next((root / "snapshots").glob("*.json")).read_text(encoding="utf-8")
        self.assertEqual(decision["action"], REVALIDATE)
        self.assertEqual(decision["revalidate_offers"][0]["price_checked_at"], offer_time.isoformat())
        self.assertEqual(_ids(decision["rankings"]), [])
        self.assertEqual(after, before)

    def test_retry_after_defers_refresh(self) -> None:
        offer_time = NOW - timedelta(hours=9)
        discovered = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(
                root,
                [_offer("th-old", "SKU-OLD17", 80_000, offer_time)],
                observed=offer_time,
                discovered=discovered,
                retry_after_seconds=12 * 3600,
            )
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
            )
        self.assertEqual(decision["action"], DEFERRED)
        self.assertEqual(decision["revalidate_offers"], [])
        self.assertEqual(decision["deferred_offers"][0]["block_reason"], "retry_after")
        self.assertFalse(decision["refresh_allowed"])

    def test_omitted_health_path_still_honors_the_standard_store(self) -> None:
        until = (NOW + timedelta(hours=3)).isoformat()
        atomic_write_json(self.health_path, {"version": 1, "stores": {"jib": {
            "disabled_until": until,
            "consecutive_failures": 3,
        }}})
        observed = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD17", 80_000, observed)], observed=observed)
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
            )
        self.assertIsNone(decision.get("health_path"))
        self.assertEqual(decision["action"], DEFERRED)
        self.assertEqual(decision["revalidate_offers"], [])
        self.assertEqual(decision["allowed_sources"], [])
        self.assertFalse(decision["refresh_allowed"])
        self.assertEqual(decision["deferred_offers"][0]["block_reason"], "circuit")
        self.assertEqual(decision["deferred_offers"][0]["next_allowed_at"], until)

    def test_disabled_source_is_not_an_allowed_request(self) -> None:
        observed = NOW - timedelta(hours=9)
        banana = _offer("bn-old", "SKU-BAN17", 80_000, observed, store="banana")
        advice = _offer("adv-old", "SKU-ADV17", 81_000, observed, store="advice")
        disabled = _result([banana], store="banana")
        disabled.policy_disabled = True
        disabled.collection_mode = "disabled"
        disabled.ok = True
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [advice], observed=observed, extra=[disabled])
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
            )
        self.assertEqual(decision["action"], REVALIDATE)
        self.assertEqual(decision["allowed_sources"], ["advice"])
        self.assertEqual([row["source"] for row in decision["revalidate_offers"]], ["advice"])
        blocked = {row["source"]: row["block_reason"] for row in decision["deferred_offers"]}
        self.assertEqual(blocked["banana"], "policy_disabled")
        self.assertNotIn("banana", decision["allowed_sources"])

    def test_partial_block_keeps_only_open_sources(self) -> None:
        until = (NOW + timedelta(hours=4)).isoformat()
        atomic_write_json(self.health_path, {"version": 1, "stores": {
            "jib": {"disabled_until": until, "consecutive_failures": 3},
            "advice": {"rate_limit_until": (NOW - timedelta(minutes=5)).isoformat()},
        }})
        observed = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(
                root,
                [_offer("th-old", "SKU-OLD17", 80_000, observed)],
                observed=observed,
                extra=[_result([_offer("adv-old", "SKU-ADV17", 81_000, observed, store="advice")], store="advice")],
            )
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
                health_path=self.health_path,
            )
        self.assertEqual(decision["action"], REVALIDATE)
        self.assertEqual(decision["allowed_sources"], ["advice"])
        self.assertTrue(decision["refresh_allowed"])
        deferred = decision["deferred_offers"][0]
        self.assertEqual(deferred["source"], "jib")
        self.assertEqual(deferred["block_reason"], "circuit")
        self.assertEqual(deferred["next_allowed_at"], until)

    def test_every_enabled_source_blocked_forbids_network(self) -> None:
        until = (NOW + timedelta(hours=2)).isoformat()
        atomic_write_json(self.health_path, {"version": 1, "stores": {
            slug: {"disabled_until": until, "consecutive_failures": 3}
            for slug in ("jib", "advice", "speedcom", "invadeit", "itcity")
        }})
        observed = NOW - timedelta(hours=27)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-1", "SKU-OLD17", 80_000, observed)], observed=observed)
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
                health_path=self.health_path,
            )
        self.assertEqual(decision["action"], DEFERRED)
        self.assertEqual(decision["allowed_sources"], [])
        self.assertFalse(decision["refresh_allowed"])

    def test_expired_snapshot_circuit_does_not_block_forever(self) -> None:
        observed = NOW - timedelta(hours=9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-old", "SKU-OLD17", 80_000, observed)], observed=observed)
            document = load_thai_catalog(root, expected_instance_id="c22-test")
            for row in document["source_coverage"]:
                if row["source"] == "jib":
                    row["state"] = "deferred"
                    row["status"] = "deferred"
            import hashlib
            snapshot_path = root / SNAPSHOT_DIRNAME / f"{document['snapshot_id']}.json"
            atomic_write_json(snapshot_path, document)
            latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            latest["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            atomic_write_json(root / "latest.json", latest)
            atomic_write_json(self.health_path, {"version": 1, "stores": {"jib": {
                "disabled_until": (NOW - timedelta(hours=1)).isoformat(),
                "consecutive_failures": 0,
            }}})
            decision = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(),
                health_path=self.health_path,
            )
        self.assertEqual(decision["action"], REVALIDATE)
        self.assertEqual(decision["allowed_sources"], ["jib"])
        self.assertTrue(decision["refresh_allowed"])

    def test_legacy_scan_path_stays_available(self) -> None:
        import inspect
        self.assertIn("catalog_root", inspect.signature(run_thailand_scan).parameters)
        self.assertIsNone(inspect.signature(run_thailand_scan).parameters["catalog_root"].default)
        self.assertLess(QUOTE_HOURS, DAILY_MIN_HOURS)
        self.assertLess(DAILY_MIN_HOURS, CATALOG_HOURS)

    def test_daily_under_18h_does_not_discover(self) -> None:
        observed = NOW - timedelta(hours=10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_offer("th-1", "SKU-1", 80_000, observed)], observed=observed)
            daily = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(), trigger="daily",
            )
            manual = evaluate_catalog_reuse(
                root, now=NOW, instance_id="c22-test", expected_scope_hash=discovery_scope_hash(), trigger="manual",
            )
        self.assertEqual(daily["action"], REVALIDATE)
        self.assertEqual(manual["action"], REVALIDATE)
        self.assertNotEqual(daily["action"], DISCOVER)


def _ids(rankings: dict) -> list[dict]:
    rows = []
    for row in rankings.get("thai_top_value") or []:
        for offer in row.get("offers") or []:
            rows.append(offer)
    return rows


if __name__ == "__main__":
    unittest.main()
