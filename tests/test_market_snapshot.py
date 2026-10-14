"""Offline acceptance for the full Russian market snapshot (stage 2.2-B)."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from buy_opportunity import evaluate_buy_rules
from identity_sync import save_specs_cache
from market_snapshot import (
    RussianSnapshotError,
    export_russian_market_snapshot,
    load_russian_market_snapshot,
    retain_russian_market_snapshots,
)
from models import Product
from product_identity import ProductIdentity
from run_pipeline import EXIT_SUCCESS, run_pipeline
from russian_deals import load_russian_ranked_deals
from storage import (
    PIPELINE_STATUS_SUCCESS,
    count_alert_events,
    count_price_history,
    count_products,
    create_pipeline_run,
    init_db,
    insert_store_run,
    open_db,
    save_products,
    upsert_product_identifier,
)
from telegram_sender import FakeTelegramSender

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
OBSERVED = NOW - timedelta(minutes=5)
BOUNDARY = NOW - timedelta(minutes=180)
STALE = BOUNDARY - timedelta(seconds=1)
FUTURE = NOW + timedelta(minutes=1)
MODEL17_AT = NOW - timedelta(minutes=30)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


def _identity(store: str, external_id: str, sku: str, price: int, name: str, **extra) -> ProductIdentity:
    return ProductIdentity(
        store=store,
        external_id=external_id,
        sku=sku,
        name=name,
        price=price,
        available=extra.get("available", True),
        url=f"https://example.test/{store}/{external_id}",
        cpu=extra.get("cpu", "Core Ultra 9 275HX"),
        gpu="RTX 5070 Ti",
        gpu_vram_gb=12,
        ram_gb=extra.get("ram_gb", 32),
        ssd_gb=1024,
        screen_size_inch=16.0,
        screen_resolution="2560x1600",
        screen_refresh_hz=extra.get("screen_refresh_hz", 240),
        manufacturer_part_number=extra.get("mpn"),
    )


class RussianMarketSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "market.db"
        self.specs_path = self.root / "product_specs.json"
        self.snapshot_root = self.root / "russian_market"
        self._write_catalog()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_catalog(self) -> None:
        products: list[Product] = []
        specs: dict[str, dict] = {}

        def add(store: str, external_id: str, sku: str, price: int, name: str, *, checked_at: datetime = OBSERVED, available: bool = True, **extra) -> None:
            products.append(Product(
                store=store,
                external_id=external_id,
                url=f"https://example.test/{store}/{external_id}",
                name=name,
                sku=sku,
                price=price,
                available=available,
                checked_at=checked_at,
            ))
            specs[f"{store}:{external_id}"] = _identity(
                store, external_id, sku, price, name, available=available, **extra
            ).to_dict()

        for index in range(1, 26):
            checked = BOUNDARY if index == 25 else MODEL17_AT if index == 17 else OBSERVED
            add(
                "regard",
                f"base-{index:02d}",
                f"RU-BASE-{index:02d}",
                180_000 + (index - 1) * 500,
                f"Baseline {index:02d}",
                checked_at=checked,
                mpn="RU17-MPN-5070TI" if index == 17 else None,
            )
        add("regard", "multi-a", "RU-MULTI-01", 301_000, "Multi config Regard", screen_refresh_hz=240)
        add("kns", "multi-b", "RU-MULTI-01", 309_000, "Multi config KNS", screen_refresh_hz=165)
        add("kns", "only-1", "RU-KNS-ONLY-01", 311_000, "KNS only laptop")
        add("regard", "conflict-a", "LEGION-16AFR10H", 313_000, "Lenovo Legion Pro 7 16AFR10H variant A", cpu="Core Ultra 9 275HX")
        add("kns", "conflict-b", "LEGION-16AFR10H", 315_000, "Lenovo Legion Pro 7 16AFR10H variant B", cpu="Core Ultra 9 285HX")
        add("regard", "msi-1", "A2XWHG-814XRU", 321_000, "MSI Raider 16 HX AI A2XWHG-814XRU")
        add("regard", "stale-1", "RU-STALE-01", 325_000, "Stale laptop", checked_at=STALE)
        add("regard", "future-1", "RU-FUTURE-01", 326_000, "Future laptop", checked_at=FUTURE)
        add("regard", "unavailable-1", "RU-UNAVAIL-01", 100_000, "Unavailable laptop", available=False)
        add("regard", "over-1", "RU-OVER-01", 330_001, "Over cap laptop")
        add("regard", "small-ram", "RU-SMALL-RAM-1", 190_000, "Small RAM laptop", ram_gb=16)
        add("dns", "failed-1", "RU-DNS-FAIL-01", 150_000, "Failed store laptop")

        save_products(products, self.db_path)
        save_specs_cache(specs, self.specs_path)
        with open_db(self.db_path) as conn:
            init_db(conn)
            run_id = create_pipeline_run(conn, started_at=OBSERVED.isoformat(), status="success")
            for store, status in (("regard", "ok"), ("kns", "ok"), ("dns", "failed")):
                insert_store_run(
                    conn,
                    pipeline_run_id=run_id,
                    store=store,
                    attempted_at=OBSERVED.isoformat(),
                    finished_at=OBSERVED.isoformat(),
                    status=status,
                    products_count=1 if status == "ok" else None,
                    error_message=None if status == "ok" else "synthetic failure",
                )
            product_id = conn.execute(
                "SELECT id FROM products WHERE store=? AND external_id=?",
                ("regard", "base-17"),
            ).fetchone()["id"]
            upsert_product_identifier(
                conn,
                product_id=int(product_id),
                identifier_type="manufacturer_part_number",
                value="RU17-MPN-5070TI",
                normalized_value="RU17-MPN-5070TI",
                source="fixture",
                seen_at=MODEL17_AT.isoformat(),
            )

    def _export(self, **kwargs):
        return export_russian_market_snapshot(
            self.db_path,
            specs_path=self.specs_path,
            snapshot_root=self.snapshot_root,
            pipeline_run_id=42,
            instance_id="snapshot-test",
            now=NOW,
            pipeline_lock_held=True,
            **kwargs,
        )

    def _load(self) -> dict:
        return load_russian_market_snapshot(
            self.snapshot_root,
            expected_instance_id="snapshot-test",
        )

    def _rewrite_base17(self, *, price: int, ram_gb: int) -> None:
        with open_db(self.db_path) as conn:
            conn.execute(
                "UPDATE products SET price=? WHERE store=? AND external_id=?",
                (price, "regard", "base-17"),
            )
        cache = json.loads(self.specs_path.read_text(encoding="utf-8"))
        cache["regard:base-17"]["price"] = price
        cache["regard:base-17"]["ram_gb"] = ram_gb
        self.specs_path.write_text(json.dumps(cache), encoding="utf-8")

    def test_exports_every_eligible_model_beyond_top10(self) -> None:
        self._export()
        document = self._load()
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["instance_id"], "snapshot-test")
        self.assertEqual(document["pipeline_run_id"], "42")
        self.assertNotEqual(document["captured_at"], document["created_at"])
        self.assertEqual(document["captured_at"], OBSERVED.isoformat())
        self.assertEqual(document["created_at"], NOW.isoformat())
        self.assertTrue(document["eligibility_policy_id"].startswith("sha256:"))
        self.assertTrue(document["ranking_policy_id"].startswith("sha256:"))
        self.assertTrue(document["database_generation"])
        self.assertTrue(document["specs_generation"])

        baselines = [
            model for model in document["models"]
            if any(str(item).startswith("RU-BASE-") for item in model["strong_identifiers"])
        ]
        self.assertEqual(len(baselines), 25)
        offer_ids = {offer["external_id"] for offer in document["offers"]}
        for index in range(1, 26):
            self.assertIn(f"base-{index:02d}", offer_ids)
        self.assertEqual(len(document["top_model_ids"]), 10)
        scored = [model for model in document["models"] if model.get("score") is not None]
        scored.sort(key=lambda model: model["rank"])
        top_skus = []
        for model_id in document["top_model_ids"]:
            model = next(item for item in document["models"] if item["model_id"] == model_id)
            top_skus.append(next(item for item in model["strong_identifiers"] if str(item).startswith("RU-BASE-")))
        self.assertEqual(top_skus, [f"RU-BASE-{index:02d}" for index in range(1, 11)])
        model17 = scored[16]
        self.assertEqual(model17["rank"], 17)
        self.assertIn("RU-BASE-17", model17["strong_identifiers"])
        self.assertNotIn(model17["model_id"], document["top_model_ids"])

        coverage = {row["store"]: row for row in document["source_coverage"]}
        self.assertEqual(coverage["regard"]["state"], "ok")
        self.assertEqual(coverage["kns"]["state"], "ok")
        self.assertEqual(coverage["dns"]["state"], "failed")
        self.assertIn("latest_attempt_failed", coverage["dns"]["limitations"])
        self.assertGreaterEqual(document["excluded_count"], 1)
        self.assertEqual(document["excluded_count"], len(document["exclusions"]))

    def test_model_17_keeps_comparison_identity_and_own_observation_time(self) -> None:
        self._export()
        document = self._load()
        offer = next(row for row in document["offers"] if row["external_id"] == "base-17")
        model = next(row for row in document["models"] if row["model_id"] == offer["model_id"])
        self.assertEqual(offer["public_price_rub"], 188_000)
        self.assertEqual(offer["store"], "regard")
        self.assertEqual(offer["quote_state"], "current")
        self.assertEqual(offer["sku_candidates"], ["RU-BASE-17"])
        self.assertIn("RU17-MPN-5070TI", offer["mpn_candidates"])
        self.assertIn("RU17-MPN-5070TI", model["strong_identifiers"])
        self.assertNotIn("188000", offer["offer_id"])
        self.assertNotIn(str(offer["public_price_rub"]), model["model_id"])
        self.assertEqual(offer["observed_at"], MODEL17_AT.isoformat())
        self.assertEqual(offer["price_checked_at"], MODEL17_AT.isoformat())
        self.assertEqual(offer["availability_checked_at"], MODEL17_AT.isoformat())
        self.assertNotEqual(offer["observed_at"], document["created_at"])
        for field, expected in (
            ("cpu", "275HX"),
            ("gpu", "5070"),
            ("ram_gb", 32),
            ("ssd_gb", 1024),
            ("screen_resolution", "2560x1600"),
        ):
            slot = offer["specs"][field]
            if isinstance(expected, str):
                self.assertIn(expected.upper(), str(slot["value"]).upper().replace(" ", ""))
            else:
                self.assertEqual(slot["value"], expected)
            self.assertEqual(slot["evidence"]["source"], "regard")
            self.assertEqual(slot["evidence"]["external_id"], "base-17")
            self.assertEqual(slot["evidence"]["method"], "specs_cache")
            self.assertEqual(slot["evidence"]["captured_at"], MODEL17_AT.isoformat())
        self.assertEqual(offer["specs"]["screen_inch"]["value"], 16.0)
        self.assertEqual(offer["specs"]["screen_inch"]["evidence"]["external_id"], "base-17")
        self.assertEqual(model["state"], "proven")
        self.assertEqual(model["member_offer_ids"], [offer["offer_id"]])

    def test_single_store_and_multi_offer_prices_stay_on_their_offers(self) -> None:
        self._export()
        document = self._load()
        kns_only = [row for row in document["offers"] if row["external_id"] == "only-1"]
        self.assertEqual(len(kns_only), 1)
        self.assertEqual(kns_only[0]["store"], "kns")
        self.assertEqual(kns_only[0]["public_price_rub"], 311_000)

        multi = [row for row in document["offers"] if row["external_id"] in {"multi-a", "multi-b"}]
        self.assertEqual(sorted(row["public_price_rub"] for row in multi), [301_000, 309_000])
        self.assertEqual({row["model_id"] for row in multi}, {multi[0]["model_id"]})
        model = next(row for row in document["models"] if row["model_id"] == multi[0]["model_id"])
        self.assertEqual(model["state"], "proven")
        self.assertEqual(len(model["member_offer_ids"]), 2)
        for row in multi:
            self.assertEqual(row["specs"]["cpu"]["evidence"]["external_id"], row["external_id"])
            self.assertEqual(row["specs"]["ram_gb"]["evidence"]["external_id"], row["external_id"])
            self.assertEqual(row["known_extras"]["screen_refresh_hz"]["evidence"]["external_id"], row["external_id"])
        refresh = {row["external_id"]: row["known_extras"]["screen_refresh_hz"]["value"] for row in multi}
        self.assertEqual(refresh["multi-a"], 240)
        self.assertEqual(refresh["multi-b"], 165)
        self.assertNotEqual(multi[0]["public_price_rub"], multi[1]["public_price_rub"])

    def test_conflicting_sku_is_not_one_proven_configuration(self) -> None:
        self._export()
        document = self._load()
        conflict = [row for row in document["offers"] if row["external_id"] in {"conflict-a", "conflict-b"}]
        self.assertEqual(len(conflict), 2)
        self.assertNotEqual(conflict[0]["model_id"], conflict[1]["model_id"])
        prices = {row["external_id"]: row["public_price_rub"] for row in conflict}
        self.assertEqual(prices, {"conflict-a": 313_000, "conflict-b": 315_000})
        cpus = {row["specs"]["cpu"]["value"] for row in conflict}
        self.assertEqual(len(cpus), 2)
        for row in conflict:
            model = next(item for item in document["models"] if item["model_id"] == row["model_id"])
            self.assertNotEqual(model["state"], "proven")
            self.assertEqual(model["member_offer_ids"], [row["offer_id"]])
            self.assertTrue(model["conflicts"])
            self.assertEqual(row["specs"]["cpu"]["evidence"]["external_id"], row["external_id"])
        for model in document["models"]:
            member_cpus = {
                row["specs"]["cpu"]["value"]
                for row in document["offers"]
                if row["offer_id"] in model["member_offer_ids"]
            }
            self.assertLessEqual(len(member_cpus), 1)
        profiles = {row["key"]: row for row in document["personal_profiles"]}
        legion = profiles["lenovo-legion-pro-7-16afr10h"]
        self.assertEqual(legion["match_kind"], "series")
        self.assertTrue({row["model_id"] for row in conflict} <= set(legion["model_ids"]))
        msi = next(row for row in document["offers"] if row["external_id"] == "msi-1")
        self.assertIn(msi["model_id"], profiles["msi-raider-16-a2xwhg-814xru"]["model_ids"])
        self.assertNotIn(msi["model_id"], legion["model_ids"])

    def test_stale_and_unverified_offers_are_not_current(self) -> None:
        self._export()
        document = self._load()
        current = {row["external_id"] for row in document["offers"]}
        for external_id in ("stale-1", "future-1", "unavailable-1", "failed-1", "small-ram", "over-1"):
            self.assertNotIn(external_id, current)
        self.assertIn("base-25", current)
        boundary = next(row for row in document["offers"] if row["external_id"] == "base-25")
        self.assertEqual(boundary["quote_state"], "current")
        self.assertEqual(boundary["observed_at"], BOUNDARY.isoformat())
        self.assertNotEqual(boundary["observed_at"], document["created_at"])
        excluded = {row["external_id"]: row for row in document["exclusions"]}
        self.assertEqual(excluded["stale-1"]["quote_state"], "stale")
        self.assertFalse(excluded["stale-1"]["current"])
        self.assertEqual(excluded["future-1"]["quote_state"], "unverified")
        self.assertFalse(excluded["future-1"]["current"])
        self.assertNotEqual(excluded["failed-1"]["quote_state"], "current")
        self.assertNotEqual(excluded["unavailable-1"]["quote_state"], "current")

    def test_parallel_source_write_does_not_publish_a_mixed_generation(self) -> None:
        flipped = {"done": False}

        def barrier(point: str) -> None:
            if point == "after_db" and not flipped["done"]:
                flipped["done"] = True
                self._rewrite_base17(price=188_777, ram_gb=64)

        self._export(consistency_barrier=barrier)
        self.assertTrue(flipped["done"])
        document = self._load()
        offer = next(row for row in document["offers"] if row["external_id"] == "base-17")
        pair = (offer["public_price_rub"], offer["specs"]["ram_gb"]["value"])
        self.assertIn(pair, {(188_000, 32), (188_777, 64)})
        self.assertNotEqual(pair, (188_000, 64))
        self.assertNotEqual(pair, (188_777, 32))
        self.assertEqual(pair, (188_777, 64))

    def test_failed_export_keeps_previous_latest_and_referenced_files(self) -> None:
        self._export()
        original = self._load()
        original_path = self.snapshot_root / "snapshots" / f"{original['snapshot_id']}.json"
        original_bytes = original_path.read_bytes()
        (self.snapshot_root / "references.json").write_text(
            json.dumps({"snapshot_ids": [original["snapshot_id"]]}),
            encoding="utf-8",
        )

        def fail_publish(point: str) -> None:
            if point == "before_latest":
                raise RuntimeError("synthetic publish failure")

        with self.assertRaises(RuntimeError):
            self._export(consistency_barrier=fail_publish)
        kept = self._load()
        self.assertEqual(kept["snapshot_id"], original["snapshot_id"])
        self.assertEqual(original_path.read_bytes(), original_bytes)

        orphan = self.snapshot_root / "snapshots" / "orphan-snapshot.json"
        orphan.write_text("{}\n", encoding="utf-8")
        self._rewrite_base17(price=188_500, ram_gb=32)
        second = self._export()
        deleted = retain_russian_market_snapshots(self.snapshot_root)
        self.assertIn("orphan-snapshot", deleted)
        self.assertTrue(original_path.is_file())
        self.assertTrue((self.snapshot_root / "snapshots" / f"{second['snapshot_id']}.json").is_file())
        self.assertFalse(orphan.exists())
        reloaded = self._load()
        offer = next(row for row in reloaded["offers"] if row["external_id"] == "base-17")
        self.assertEqual(offer["public_price_rub"], 188_500)
        previous_offer = next(row for row in original["offers"] if row["external_id"] == "base-17")
        self.assertEqual(previous_offer["public_price_rub"], 188_000)
        self.assertEqual(previous_offer["offer_id"], offer["offer_id"])
        self.assertEqual(
            next(row["model_id"] for row in original["models"] if row["model_id"] == previous_offer["model_id"]),
            offer["model_id"],
        )

    def test_unstable_generation_does_not_replace_latest(self) -> None:
        self._export()
        original = self._load()
        original_bytes = (self.snapshot_root / "snapshots" / f"{original['snapshot_id']}.json").read_bytes()
        counter = {"n": 0}

        def always_change(point: str) -> None:
            if point == "after_db":
                counter["n"] += 1
                self._rewrite_base17(price=188_000 + counter["n"], ram_gb=32 + counter["n"])

        with self.assertRaises(RussianSnapshotError):
            self._export(consistency_barrier=always_change, publish_attempts=3)
        self.assertGreaterEqual(counter["n"], 3)
        kept = self._load()
        self.assertEqual(kept["snapshot_id"], original["snapshot_id"])
        self.assertEqual(
            (self.snapshot_root / "snapshots" / f"{original['snapshot_id']}.json").read_bytes(),
            original_bytes,
        )

    def test_russian_ranking_buy_history_and_alerts_stay_unchanged(self) -> None:
        with patch("store_freshness.datetime", _FrozenDateTime):
            before_top = load_russian_ranked_deals(self.db_path, specs_path=self.specs_path)
            before_all = load_russian_ranked_deals(self.db_path, specs_path=self.specs_path, limit=0)
            before_buy = [None if evaluate_buy_rules(deal) is None else evaluate_buy_rules(deal).to_dict() for deal in before_top]
            from alerts import run_monitor

            run_monitor(self.db_path, specs_path=self.specs_path)
            with open_db(self.db_path) as conn:
                before_counts = (
                    count_products(conn),
                    count_price_history(conn),
                    count_alert_events(conn),
                )
                before_history = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT product_id, price, available, checked_at FROM price_history ORDER BY id"
                    )
                ]
                before_alerts = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT event_type, product_id, dedupe_key FROM alert_events ORDER BY id"
                    )
                ]
            self._export()
            after_top = load_russian_ranked_deals(self.db_path, specs_path=self.specs_path)
            after_all = load_russian_ranked_deals(self.db_path, specs_path=self.specs_path, limit=0)
            after_buy = [None if evaluate_buy_rules(deal) is None else evaluate_buy_rules(deal).to_dict() for deal in after_top]
            with open_db(self.db_path) as conn:
                after_counts = (
                    count_products(conn),
                    count_price_history(conn),
                    count_alert_events(conn),
                )
                after_history = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT product_id, price, available, checked_at FROM price_history ORDER BY id"
                    )
                ]
                after_alerts = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT event_type, product_id, dedupe_key FROM alert_events ORDER BY id"
                    )
                ]
        self.assertEqual(_deal_signature(before_top), _deal_signature(after_top))
        self.assertEqual(_deal_signature(before_all), _deal_signature(after_all))
        self.assertEqual(_deal_signature(before_top), _deal_signature(before_all[:10]))
        self.assertEqual(len(before_top), 10)
        self.assertGreaterEqual(len(before_all), 25)
        self.assertEqual(before_all[16].offer["external_id"], "base-17")
        self.assertEqual(before_buy, after_buy)
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_history, after_history)
        self.assertEqual(before_alerts, after_alerts)
        document = self._load()
        self.assertEqual(len(document["top_model_ids"]), 10)
        self.assertGreater(len(document["models"]), 10)

    def test_snapshot_file_is_immutable_and_hash_checked(self) -> None:
        first = self._export()
        path = self.snapshot_root / "snapshots" / f"{first['snapshot_id']}.json"
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), first["sha256"])
        self._rewrite_base17(price=189_000, ram_gb=32)
        second = self._export()
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(path.read_bytes(), raw)
        manifest = json.loads((self.snapshot_root / "latest.json").read_text(encoding="utf-8"))
        manifest["sha256"] = "0" * 64
        (self.snapshot_root / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(RussianSnapshotError):
            self._load()


class PipelineSnapshotIsolationTests(unittest.TestCase):
    def test_export_failure_does_not_fail_russian_collection(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "pipeline.db"
            specs_path = root / "specs.json"
            specs_path.write_text("{}", encoding="utf-8")
            snapshot_root = root / "russian_market"
            export_russian_market_snapshot(
                _seed_empty_market(db_path),
                specs_path=specs_path,
                snapshot_root=snapshot_root,
                pipeline_run_id=1,
                instance_id="pipeline-test",
                now=NOW,
                pipeline_lock_held=True,
            )
            before = (snapshot_root / "latest.json").read_bytes()

            def regard():
                return [_pipeline_product("regard", "1", 250_000)]

            def andpro():
                return [_pipeline_product("andpro", "a1", 240_000)]

            with patch(
                "market_snapshot.export_russian_market_snapshot",
                side_effect=RuntimeError("synthetic export failure"),
            ):
                result = run_pipeline(
                    db_path,
                    lock_path=root / "pipeline.lock",
                    fetch_regard=regard,
                    fetch_andpro=andpro,
                    sender=FakeTelegramSender(),
                    deliver=False,
                    enrich_identities=False,
                    specs_path=specs_path,
                    use_lock=True,
                    buy_state_path=root / "buy.json",
                    thailand_jobs_dir=root / "jobs",
                    russian_snapshot_root=snapshot_root,
                )
            self.assertEqual(result.status, PIPELINE_STATUS_SUCCESS)
            self.assertEqual(result.exit_code, EXIT_SUCCESS)
            self.assertEqual(result.russian_snapshot_status, "export_failed")
            self.assertIsNone(result.russian_snapshot_id)
            self.assertEqual((snapshot_root / "latest.json").read_bytes(), before)
            self.assertFalse(any((root / "jobs").glob("*.json")))

    def test_successful_pipeline_publishes_snapshot_without_job_schema_change(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "pipeline.db"
            specs_path = root / "specs.json"
            specs_path.write_text("{}", encoding="utf-8")
            snapshot_root = root / "russian_market"

            def regard():
                return [_pipeline_product("regard", "1", 250_000)]

            def andpro():
                return [_pipeline_product("andpro", "a1", 240_000)]

            result = run_pipeline(
                db_path,
                lock_path=root / "pipeline.lock",
                fetch_regard=regard,
                fetch_andpro=andpro,
                sender=FakeTelegramSender(),
                deliver=False,
                enrich_identities=False,
                specs_path=specs_path,
                use_lock=True,
                buy_state_path=root / "buy.json",
                thailand_jobs_dir=root / "jobs",
                russian_snapshot_root=snapshot_root,
            )
            self.assertEqual(result.status, PIPELINE_STATUS_SUCCESS)
            self.assertEqual(result.exit_code, EXIT_SUCCESS)
            self.assertEqual(result.russian_snapshot_status, "published")
            document = load_russian_market_snapshot(snapshot_root)
            self.assertEqual(document["pipeline_run_id"], str(result.run_id))
            self.assertEqual(document["schema_version"], 1)
            self.assertFalse(any((root / "jobs").glob("*.json")))


def _deal_signature(deals) -> list[tuple]:
    return [
        (
            deal.score,
            deal.price,
            deal.store,
            deal.cluster_name,
            deal.cluster_key,
            deal.cpu,
            deal.gpu,
            deal.ram_gb,
            deal.ssd_gb,
            deal.screen_inch,
            deal.screen_resolution,
            deal.saving_vs_next,
            deal.saving_verified,
            tuple(deal.reasons),
            tuple(sorted((deal.score_breakdown or {}).items())),
            (deal.offer or {}).get("external_id"),
        )
        for deal in deals
    ]


def _pipeline_product(store: str, external_id: str, price: int) -> Product:
    return Product(
        store=store,
        external_id=external_id,
        url=f"https://example.test/{store}/{external_id}",
        name="Pipeline laptop RTX 5070 Ti 32GB 1TB 2560x1600",
        sku="PIPE-SKU-01",
        price=price,
        available=True,
        checked_at=NOW,
    )


def _seed_empty_market(db_path: Path) -> Path:
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = create_pipeline_run(conn, started_at=OBSERVED.isoformat(), status="success")
        insert_store_run(
            conn,
            pipeline_run_id=run_id,
            store="regard",
            attempted_at=OBSERVED.isoformat(),
            finished_at=OBSERVED.isoformat(),
            status="ok",
            products_count=0,
        )
    return db_path


if __name__ == "__main__":
    unittest.main()
