"""The morning RU list uses the existing evidence gate, even without TH/FX."""
import copy
import tempfile
import unittest
from datetime import timedelta
from datetime import datetime, timezone
from unittest.mock import patch

from tests.test_thai_catalog_reuse import NOW, _russian
from thai_catalog import build_thai_catalog, comparison_from_catalog
from thailand.catalog_refresh import _views as refresh_views
from thailand.catalog_reuse import _views as reuse_views
from thailand.market_messages import format_market_messages
from thailand.market_telegram import MarketTextSender
from thailand.models import StoreScanResult
from identity_sync import _needs_structured_capture, _stamp_structured_provenance
from product_identity import ProductIdentity


def empty_th():
    return build_thai_catalog([StoreScanResult(store="jib", ok=True, offers=[])],
                              now=NOW, discovery_checked_at=NOW, instance_id="c22-test")


def russian():
    raw = _russian()
    for row in raw["offers"]:
        row["source_url"] = f"https://example.test/{row['external_id']}"
    return raw


def digest(ru, *, now=NOW, fx=None, th=None, status="completed"):
    comparison = comparison_from_catalog(th or empty_th(), ru, fx, now=NOW)
    result = {"comparison": comparison, "rankings": {}, "fx": fx, "status": status}
    return result, format_market_messages(result, {"trigger_type": "daily"}, now=now)


class RussianMorning(unittest.TestCase):
    def test_interactive_top_uses_same_snapshot_evidence_and_budget(self):
        from control_bot import build_top_parts
        ru=russian()
        current=datetime.now(timezone.utc)
        ru["created_at"]=current.isoformat()
        ru["source_coverage"][0]["latest_attempt_at"]=current.isoformat()
        for row in ru["offers"]:
            row["price_checked_at"]=row["availability_checked_at"]=current.isoformat()
            row["availability_evidence"]["captured_at"]=current.isoformat()
            for entry in row["specs"].values():entry["evidence"]["captured_at"]=current.isoformat()
        with patch("market_snapshot.load_russian_market_snapshot",return_value=ru):
            texts=build_top_parts("/tmp/fixture.db")
        self.assertIn("TOP-10",texts[0])
        self.assertIn("https://example.test/ru-01",texts[0])
        self.assertNotIn("https://example.test/ru-11","\n".join(texts))

    def test_long_links_keep_all_ten_cards_across_bounded_parts(self):
        ru=russian()
        for row in ru["offers"]:row["source_url"]+="/"+"a"*230
        _,parts=digest(ru)
        self.assertGreater(len(parts),1)
        self.assertTrue(all(len(p["text"])<=4000 for p in parts))
        joined="\n".join(p["text"] for p in parts)
        for n in range(1,11):self.assertIn(f"https://example.test/ru-{n:02d}/",joined)

    def test_legacy_regard_fields_need_own_successful_capture(self):
        # Names/prices from the actual 08:00 snapshot; no old cache timestamp
        # is inferred from the fresh listing observation.
        cases = (("Gigabyte Gaming A16 Pro GA6DH", "RTX 5080 LAPTOP", 268740),
                 ("MSI Raider 16 HX AI A2XWHG-814XRU", "RTX 5070 TI LAPTOP", 266030),
                 ("Gigabyte Gaming A18 PRO DXJ", "RTX 5070 TI LAPTOP", 252010),
                 ("Gigabyte Gaming A18 PRO DYJ", "RTX 5080 LAPTOP", 264340))
        for name,gpu,price in cases:
            with self.subTest(name=name):
                identity=ProductIdentity(store="regard",external_id="same",name=name,
                                         sku=None,price=price,available=True,url=None,
                                         gpu=gpu,ram_gb=32,ssd_gb=1024,
                                         source_identifiers={"field_captured_at":{}})
                product={"price":price,"available":True}
                self.assertTrue(_needs_structured_capture(identity,product))
                self.assertFalse(_needs_structured_capture(identity,{"price":290001,"available":True}))
                self.assertFalse(_needs_structured_capture(identity,{"price":price,"available":False}))
                self.assertFalse(_stamp_structured_provenance(identity,"regard.api").source_identifiers["field_captured_at"])

    def test_repeated_daily_event_reuses_durable_content_ack(self):
        class Sink:
            def __init__(self): self.texts=[]
            def send_market_message(self, text, **kwargs):
                self.texts.append(text)
                return type("Ack", (), {"ok": True})()
        with tempfile.TemporaryDirectory() as folder:
            result,_=digest(russian())
            result["result_id"]="same-result"
            sink=Sink();sender=MarketTextSender(sink,jobs_dir=folder,now_fn=lambda:NOW)
            first={"event_id":"one","trigger_type":"daily","recipients":["42"]}
            second={"event_id":"two","trigger_type":"daily","recipients":["42"]}
            sender.send(first,result,current=False,historical=False)
            sender.send(second,result,current=False,historical=False)
            self.assertEqual(len(sink.texts),1)
            self.assertEqual(second["delivered_part_ids"][0]["reused"],True)

    def test_ten_unique_models_all_links_and_units_without_th_or_fx(self):
        ru = russian()
        result, parts = digest(ru)
        self.assertEqual(len(result["comparison"]["ru_eligible_offers"]), 25)
        self.assertEqual(len(parts), 1)
        text = parts[0]["text"]
        self.assertIn("TOP-10", text)
        self.assertIn("RAM 32 ГБ", text)
        self.assertIn("SSD 1024 ГБ", text)
        self.assertIn("12 ГБ", text)
        self.assertIn("МСК", text)
        self.assertLessEqual(len(text), 4000)
        for n in range(1, 11):
            self.assertIn(f"https://example.test/ru-{n:02d}", text)
        self.assertNotIn("https://example.test/ru-11", text)
        self.assertEqual(parts, format_market_messages(result, {"trigger_type": "daily"}, now=NOW))

    def test_comparison_builds_without_fx_in_both_paths(self):
        ru = russian()
        for build in (refresh_views, reuse_views):
            result = build(empty_th(), None, ru, NOW)
            comparison = result[1]
            self.assertEqual(len(comparison["ru_eligible_offers"]), 25)
            self.assertFalse(comparison["data_freshness"]["fx_usable"])

    def test_failed_store_stale_price_stock_budget_and_missing_spec(self):
        base = russian()
        changes = (
            lambda r: r["source_coverage"][0].update(state="failed"),
            lambda r: r["offers"][0].update(price_checked_at=(NOW-timedelta(hours=5)).isoformat()),
            lambda r: r["offers"][0].update(stock_state="out_of_stock"),
            lambda r: r["offers"][0].update(public_price_rub=290001),
            lambda r: r["offers"][0]["specs"]["ram_gb"].update(evidence=None),
        )
        for mutate in changes:
            with self.subTest(mutate=mutate):
                ru=copy.deepcopy(base);mutate(ru)
                result,_=digest(ru)
                excluded={row["best_offer_id"] for row in result["comparison"]["ru_eligible_offers"]}
                self.assertNotIn("ru-01", excluded)

    def test_old_unknown_cache_fields_are_information_only(self):
        ru=russian()
        for row in ru["offers"]:
            for entry in row["specs"].values():
                entry["evidence"]={**entry["evidence"],"captured_at":None,"capture_state":"unknown"}
        result,parts=digest(ru)
        self.assertEqual(result["comparison"]["ru_eligible_offers"],[])
        self.assertEqual(len(result["comparison"]["ru_information_candidates"]),25)
        text="\n".join(p["text"] for p in parts)
        self.assertIn("TOP-0",text)
        self.assertEqual(text.count("Кандидат, не рекомендация"),3)
        self.assertNotIn("1. ASUS",text)

    def test_different_configuration_of_same_family_is_not_merged(self):
        ru=russian()
        ru["offers"][1]["model_id"]=ru["offers"][0]["model_id"]
        ru["offers"][1]["specs"]["gpu"]["value"]="RTX 5080 Laptop"
        result,parts=digest(ru)
        self.assertIn("ru-01",parts[0]["text"])
        self.assertIn("ru-02",parts[0]["text"])


if __name__ == "__main__":
    unittest.main()
