"""Offline acceptance for the unified Thai catalog (stage 2.2-C1)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thai_catalog import (
    ThaiCatalogError,
    build_thai_catalog,
    comparison_from_catalog,
    discovery_scope_hash,
    export_thai_catalog,
    load_thai_catalog,
    rankings_from_catalog,
)
from thailand.discovery import GPU_SEARCH_QUERIES
from thailand.invadeit import enrich_offer_from_specifications, parse_invadeit_item
from thailand.jib import apply_detail_verification, parse_read_product_html, parse_suggestion_rec
from thailand.models import StoreScanResult, ThailandOffer

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
OBSERVED = NOW - timedelta(minutes=30)
STALE = NOW - timedelta(hours=7)
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "invadeit"
JIB_DETAIL = """
<html><title>NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W</title>
<h1>NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W</h1>
<p>• ซีพียู : AMD Ryzen 9 8940HX</p>
<p>• กราฟิก : Nvidia GeForce RTX 5070 Ti 12GB GDDR7</p>
<p>RAM 16GB DDR5 และ SSD</p>
<p>• เอสเอสดี : 1TB PCIe 4/NVMe M.2 SSD</p>
<p>• จอแสดงผล : 16&quot; 2.5K (2560 x 1600, WQXGA)</p>
<script>qty = 2;</script>
</html>
"""


def _field_evidence(collected: datetime) -> dict:
    entry = {"method": "structured_api", "captured_at": collected.isoformat()}
    return {name: dict(entry) for name in ("cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")}


def _full(external_id: str, price: int, sku: str, *, collected: datetime = OBSERVED, status: str = "in_stock", available: bool = True, cpu: str | None = "Intel Core Ultra 9 275HX", gpu: str | None = "RTX 5070 Ti", gpu_source: str = "structured_api", resolution: str | None = "2560x1600", ram: int | None = 32) -> ThailandOffer:
    return ThailandOffer(
        store="jib",
        external_id=external_id,
        name=f"ASUS ROG Strix {sku}",
        url=f"https://example.test/jib/{external_id}",
        price_thb=price,
        available=available,
        collected_at=collected,
        sku=sku,
        manufacturer_part_number=sku,
        brand="ASUS",
        cpu=cpu,
        gpu=gpu,
        gpu_source=gpu_source,
        ram_gb=ram,
        ssd_gb=1024,
        screen_size_inch=16.0,
        screen_resolution=resolution,
        availability_status=status,
        availability_confirmed=status != "unknown",
        availability_source="structured_api" if status != "unknown" else "search_suggestion",
        price_source="structured_api",
        price_verified_for_variant=True,
        channel="direct",
        metadata={"field_evidence": _field_evidence(collected)},
    )


def _finished(store: str, offers: list[ThailandOffer], *, candidates: list[ThailandOffer] | None = None, finished: list[str] | None = None, rate_limited: bool = False) -> StoreScanResult:
    return StoreScanResult(
        store=store,
        ok=not rate_limited,
        offers=offers,
        unverified_candidates=candidates or [],
        queries_finished=list(GPU_SEARCH_QUERIES) if finished is None and not rate_limited else list(finished or []),
        discovery_queries=list(GPU_SEARCH_QUERIES),
        request_count=len(finished or GPU_SEARCH_QUERIES),
        error_code="RATE_LIMITED" if rate_limited else None,
        discovery_rate_limited=rate_limited,
        collection_mode="rate_limited" if rate_limited else "http",
        retry_after_seconds=60 if rate_limited else None,
    )


def _invadeit(product_id: int, sku: str, fixture: str) -> ThailandOffer:
    offer = parse_invadeit_item(
        {
            "id": product_id,
            "name": f"ROG Strix G16 {sku}",
            "productNumber": sku,
            "urlName": sku.lower(),
            "price": 99990,
            "inStock": True,
            "keySpecs": '16" 2.5K / AMD Ryzen 9 8940HX\n16GB DDR5 / 1TB SSD\nGeForce RTX 5070 Ti 12GB',
            "manufacturerName": "ASUS",
            "categoryName": "Notebooks / Laptops",
            "categoryUrlName": "notebooks-laptops",
            "manufacturerUrlName": "asus",
        },
        collected_at=OBSERVED,
    )
    assert offer is not None
    payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    return enrich_offer_from_specifications(offer, payload, fetched_at=OBSERVED)


def _russian_gate() -> dict:
    observed = OBSERVED.isoformat()
    evidence = {
        "source": "regard",
        "external_id": "ru-gate",
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
    return {
        "schema_version": 1,
        "snapshot_id": "ru-gate-snapshot",
        "instance_id": "thai-test",
        "created_at": NOW.isoformat(),
        "offers": [{
            "offer_id": "ru-gate",
            "external_id": "ru-gate",
            "store": "regard",
            "model_id": "ru-gate-model",
            "sku_candidates": ["SKU-GATE-1"],
            "mpn_candidates": [],
            "public_price_rub": 250_000,
            "price_checked_at": observed,
            "availability_checked_at": observed,
            "observed_at": observed,
            "stock_state": "in_stock",
            "availability_evidence": {**evidence, "method": "store_listing", "available": True},
            "product_name": "ASUS ROG Strix SKU-GATE-1",
            "specs": {name: {"value": value, "evidence": evidence} for name, value in specs.items()},
        }],
        "source_coverage": [{
            "store": "regard",
            "state": "ok",
            "latest_attempt_at": observed,
        }],
        "top_model_ids": ["ru-gate-model"],
        "personal_profiles": [],
    }


def _fx() -> dict:
    return {
        "fx_id": "cbr-c1",
        "source": "CBR",
        "currency": "THB",
        "nominal": 1,
        "rub_per_thb": "2.5",
        "fetched_at": OBSERVED.isoformat(),
        "published_date": "2026-10-14",
        "error": None,
    }


class ThaiCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.eligible = [
            _full(f"eligible-{index}", 70000 + index * 2000, f"SKU-ELIGIBLE-{index}")
            for index in range(1, 7)
        ]
        self.oos = _full("oos-1", 69000, "SKU-OOS-1", status="out_of_stock", available=False)
        self.stale = _full("stale-1", 68000, "SKU-STALE-1", collected=STALE)
        self.query_only = ThailandOffer(
            store="jib",
            external_id="query-only",
            name="NOTEBOOK ASUS TUF GAMING A16",
            url="https://example.test/jib/query-only",
            price_thb=90000,
            available=False,
            collected_at=OBSERVED,
            sku="SKU-QUERY-ONLY",
            gpu=None,
            gpu_source="unknown",
            candidate_gpu="RTX 5070 Ti",
            availability_status="unknown",
            availability_confirmed=False,
            availability_source="search_suggestion",
            price_source="search_suggestion",
            price_verified_for_variant=True,
            channel="direct",
            metadata={"source": "search_suggestion", "candidate_gpu_source": "search_query"},
        )
        detail_offers = parse_suggestion_rec(
            [{
                "id": "0000085696",
                "title": "NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W RTX 5080",
                "price": "90000.00",
                "salePrice": "84990.00",
            }],
            catalog_gpu="RTX 5080",
            collected_at=OBSERVED,
        )
        self.jib_detail = apply_detail_verification(
            detail_offers[0],
            parse_read_product_html(JIB_DETAIL, external_id="0000085696"),
        )
        self.line_a = _invadeit(81984, "G614PR-TS113W", "81984.json")
        self.line_b = _invadeit(81991, "G835LWG-TEST", "81991.json")
        self.advice_kept = _full("advice-kept", 71000, "SKU-ADVICE-KEPT", collected=STALE)
        self.advice_kept.store = "advice"
        self.candidate = _full("candidate-1", 72000, "SKU-CANDIDATE-1")
        self.candidate.cpu = None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _results(self) -> list[StoreScanResult]:
        return [
            _finished("jib", [*self.eligible, self.oos, self.stale, self.query_only, self.jib_detail]),
            _finished("invadeit", [self.line_a, self.line_b]),
            _finished("advice", [self.advice_kept], candidates=[self.candidate], finished=["notebook rtx 5070 ti"], rate_limited=True),
        ]

    def _catalog(self) -> dict:
        return build_thai_catalog(
            self._results(),
            now=NOW,
            discovery_checked_at=OBSERVED,
            instance_id="thai-test",
            started_at=OBSERVED,
        )

    def test_catalog_is_independent_of_russian_top_and_feeds_core(self) -> None:
        self.assertEqual(discovery_scope_hash(), discovery_scope_hash())
        document = self._catalog()
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["discovery_scope_hash"], discovery_scope_hash())
        self.assertNotIn("RU-BASE", document["discovery_scope_hash"])
        self.assertEqual(document["top_model_ids"], [])
        self.assertEqual(document["discovery_checked_at"], OBSERVED.isoformat())
        self.assertEqual(document["created_at"], NOW.isoformat())
        self.assertNotEqual(document["created_at"], document["offers"][0]["price_checked_at"])
        rankings = rankings_from_catalog(document, _fx(), now=NOW)
        compared = comparison_from_catalog(document, None, _fx(), now=NOW)
        self.assertGreater(len(document["offers"]), 5)
        self.assertGreaterEqual(len(rankings["eligible_models"]), 6)
        self.assertEqual(len(rankings["thai_top_value"]), 5)
        self.assertEqual(len(rankings["all_offers"]), len(document["offers"]))
        self.assertIsNone(compared["ru_snapshot_id"])
        self.assertEqual(compared["pairs"], [])
        self.assertEqual(len(compared["thai_top_value"]), 5)
        eligible_ids = {row["best_offer_id"] for row in rankings["eligible_models"]}
        self.assertGreater(len(rankings["all_offers"]), len(eligible_ids))

    def test_jib_and_invadeit_fixtures_keep_their_own_facts(self) -> None:
        document = self._catalog()
        by_id = {row["external_id"]: row for row in document["offers"]}
        jib = by_id["0000085696"]
        self.assertEqual(jib["specs"]["gpu"]["value"], "RTX 5070 Ti")
        self.assertEqual(jib["specs"]["gpu"]["evidence"]["method"], "structured_product_page")
        self.assertEqual(jib["specs"]["gpu"]["evidence"]["external_id"], "0000085696")
        self.assertEqual(jib["price_thb"], 84990)
        self.assertNotEqual(jib["specs"]["gpu"]["evidence"]["provenance"], "search_query")
        query_only = by_id["query-only"]
        self.assertIsNone(query_only["specs"]["gpu"]["value"])
        self.assertEqual(query_only["specs"]["gpu"]["evidence"]["capture_state"], "unknown")
        self.assertEqual(query_only["specs"]["gpu"]["evidence"]["candidate_value"], "RTX 5070 Ti")
        first = by_id["81984"]
        second = by_id["81991"]
        self.assertEqual(first["specs"]["screen_resolution"]["value"], "2560x1600")
        self.assertEqual(second["specs"]["screen_resolution"]["value"], "3840x2400")
        self.assertEqual(first["sku"], "G614PR-TS113W")
        self.assertEqual(second["sku"], "G835LWG-TEST")
        for variant in document["variants"]:
            members = {
                row["external_id"]
                for row in document["offers"]
                if row["offer_id"] in variant["member_offer_ids"]
            }
            self.assertFalse({"81984", "81991"} <= members)
            if variant["state"] == "proven":
                self.assertEqual(len({row.get("sku") for row in document["offers"] if row["offer_id"] in variant["member_offer_ids"]}), 1)
        self.assertEqual(first["specs"]["screen_resolution"]["evidence"]["external_id"], "81984")
        self.assertEqual(second["specs"]["screen_resolution"]["evidence"]["external_id"], "81991")
        self.assertEqual(first["specs"]["screen_resolution"]["evidence"]["captured_at"], OBSERVED.isoformat())
        self.assertNotEqual(first["specs"]["screen_resolution"]["evidence"]["captured_at"], document["created_at"])

    def test_unverified_candidate_is_not_a_purchase_or_a_confirmed_saving(self) -> None:
        confirmed = _full("confirmed-1", 80_000, "SKU-GATE-1")
        candidate = _full("candidate-gate", 50_000, "SKU-GATE-1")
        document = build_thai_catalog(
            [_finished("jib", [confirmed], candidates=[candidate])],
            now=NOW,
            discovery_checked_at=OBSERVED,
            instance_id="thai-test",
        )
        rows = {row["external_id"]: row for row in document["offers"]}
        withheld = rows["candidate-gate"]
        self.assertEqual(withheld["record_role"], "candidate")
        self.assertEqual(withheld["purchase_excluded_reason"], "unverified_candidate")
        self.assertTrue(withheld["candidate_observation"]["availability_confirmed"])
        self.assertTrue(withheld["candidate_observation"]["price_verified_for_variant"])
        self.assertEqual(withheld["candidate_observation"]["specs"]["cpu"]["evidence"]["capture_state"], "known")
        self.assertFalse(withheld["availability_confirmed"])
        self.assertFalse(withheld["price_verified_for_variant"])
        self.assertEqual(withheld["specs"]["cpu"]["evidence"]["capture_state"], "unknown")
        self.assertIsNone(withheld["specs"]["cpu"]["evidence"]["captured_at"])
        self.assertIn(
            withheld["offer_id"],
            {row["offer_id"] for row in document["exclusions"] if row["reason"] == "unverified_candidate"},
        )
        rankings = rankings_from_catalog(document, _fx(), now=NOW)
        ranked = {offer_id for row in rankings["eligible_models"] for offer_id in row["member_offer_ids"]}
        top = {offer_id for row in rankings["thai_top_value"] for offer_id in row["member_offer_ids"]}
        self.assertIn(rows["confirmed-1"]["offer_id"], ranked)
        self.assertIn(rows["confirmed-1"]["offer_id"], top)
        self.assertNotIn(withheld["offer_id"], ranked)
        self.assertNotIn(withheld["offer_id"], top)
        compared = comparison_from_catalog(document, _russian_gate(), _fx(), now=NOW)
        by_th = {pair["th_offer_id"]: pair for pair in compared["pairs"]}
        self.assertEqual(by_th[rows["confirmed-1"]["offer_id"]]["comparison_status"], "confirmed")
        self.assertIsNotNone(by_th[rows["confirmed-1"]["offer_id"]]["saving_rub"])
        self.assertNotEqual(by_th[withheld["offer_id"]]["comparison_status"], "confirmed")
        self.assertIsNone(by_th[withheld["offer_id"]]["saving_rub"])

    def test_gpu_evidence_is_not_copied_onto_other_fields(self) -> None:
        offer = ThailandOffer(
            store="jib",
            external_id="gpu-basis",
            name="ASUS ROG Strix GPU basis",
            url="https://example.test/jib/gpu-basis",
            price_thb=80_000,
            available=True,
            collected_at=OBSERVED,
            sku="SKU-GPU-BASIS",
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
        )
        document = build_thai_catalog(
            [_finished("jib", [offer])],
            now=NOW,
            discovery_checked_at=OBSERVED,
            instance_id="thai-test",
        )
        row = next(item for item in document["offers"] if item["external_id"] == "gpu-basis")
        self.assertEqual(row["specs"]["gpu"]["evidence"]["method"], "structured_api")
        self.assertEqual(row["specs"]["gpu"]["evidence"]["capture_state"], "known")
        self.assertEqual(row["specs"]["gpu"]["evidence"]["captured_at"], OBSERVED.isoformat())
        for field in ("cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution"):
            evidence = row["specs"][field]["evidence"]
            self.assertEqual(evidence["capture_state"], "unknown")
            self.assertIsNone(evidence["captured_at"])
            self.assertNotEqual(evidence["method"], "structured_api")
        self.assertEqual(row["specs"]["cpu"]["value"], "Intel Core Ultra 9 275HX")

    def test_stock_candidates_partial_and_timestamps(self) -> None:
        document = self._catalog()
        by_id = {row["external_id"]: row for row in document["offers"]}
        self.assertEqual(by_id["oos-1"]["availability_state"], "out_of_stock")
        self.assertIn("oos-1", {row["external_id"] for row in document["exclusions"]})
        self.assertEqual(by_id["candidate-1"]["record_role"], "candidate")
        self.assertIsNone(by_id["candidate-1"]["specs"]["cpu"]["value"])
        self.assertEqual(by_id["stale-1"]["price_checked_at"], STALE.isoformat())
        self.assertEqual(by_id["stale-1"]["observed_at"], STALE.isoformat())
        self.assertNotEqual(by_id["stale-1"]["price_checked_at"], document["created_at"])
        self.assertEqual(by_id["advice-kept"]["price_checked_at"], STALE.isoformat())
        coverage = {row["source"]: row for row in document["source_coverage"]}
        self.assertEqual(coverage["advice"]["state"], "429")
        self.assertIn("notebook rtx 5070 ti", coverage["advice"]["completed_keys"])
        self.assertNotIn("notebook rtx 5070 ti", coverage["advice"]["not_completed_keys"])
        self.assertTrue(coverage["advice"]["not_completed_keys"])
        self.assertEqual(coverage["speedcom"]["state"], "failed")
        self.assertIn("not_attempted", coverage["speedcom"]["limitations"])
        self.assertIsNone(coverage["speedcom"]["checked_at"])
        self.assertTrue(coverage["banana"]["policy_disabled"])
        self.assertFalse(coverage["banana"]["enabled"])
        rankings = rankings_from_catalog(document, _fx(), now=NOW)
        ranked_ids = {offer_id for row in rankings["eligible_models"] for offer_id in row["member_offer_ids"]}
        catalog_ids = {row["offer_id"]: row["external_id"] for row in document["offers"]}
        self.assertNotIn("oos-1", {catalog_ids[item] for item in ranked_ids})
        self.assertNotIn("query-only", {catalog_ids[item] for item in ranked_ids})
        self.assertNotIn("stale-1", {catalog_ids[item] for item in ranked_ids})

    def test_failed_publication_keeps_previous_latest(self) -> None:
        first = export_thai_catalog(
            self._results(),
            snapshot_root=self.root,
            now=NOW,
            discovery_checked_at=OBSERVED,
            instance_id="thai-test",
        )
        original = load_thai_catalog(self.root)
        raw = (self.root / "snapshots" / f"{first['snapshot_id']}.json").read_bytes()

        def fail(point: str) -> None:
            if point == "before_latest":
                raise RuntimeError("synthetic publish failure")

        with self.assertRaises(RuntimeError):
            export_thai_catalog(
                self._results(),
                snapshot_root=self.root,
                now=NOW + timedelta(hours=1),
                discovery_checked_at=OBSERVED,
                instance_id="thai-test",
                publish_barrier=fail,
            )
        kept = load_thai_catalog(self.root)
        self.assertEqual(kept["snapshot_id"], original["snapshot_id"])
        self.assertEqual((self.root / "snapshots" / f"{first['snapshot_id']}.json").read_bytes(), raw)
        later = export_thai_catalog(
            self._results(),
            snapshot_root=self.root,
            now=NOW + timedelta(hours=1),
            discovery_checked_at=OBSERVED,
            instance_id="thai-test",
        )
        refreshed = load_thai_catalog(self.root)
        self.assertEqual(refreshed["snapshot_id"], later["snapshot_id"])
        self.assertEqual(refreshed["created_at"], (NOW + timedelta(hours=1)).isoformat())
        self.assertEqual(
            next(row["price_checked_at"] for row in refreshed["offers"] if row["external_id"] == "stale-1"),
            STALE.isoformat(),
        )
        self.assertEqual((self.root / "snapshots" / f"{first['snapshot_id']}.json").read_bytes(), raw)
        with self.assertRaises(ThaiCatalogError):
            broken = json.loads((self.root / "latest.json").read_text(encoding="utf-8"))
            broken["sha256"] = "0" * 64
            (self.root / "latest.json").write_text(json.dumps(broken), encoding="utf-8")
            load_thai_catalog(self.root)


if __name__ == "__main__":
    unittest.main()
