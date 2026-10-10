from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
from buy_thailand_flow import (
    enqueue_model_thailand_job,
    evaluate_buy_opportunity_flow,
    execute_thailand_job,
)
from control_bot import (
    CALLBACK_MODEL_PICK,
    _markets_keyboard,
    friendly_model_label,
    handle_thailand_model_pick,
    model_callback_token,
    model_picker_keyboard,
    picker_button_label,
    process_update,
    unique_russian_top,
)
from deal_ranking import RankedDeal
from thailand.eligibility import offer_user_facing_eligible
from thailand.errors import RATE_LIMITED, classify_store_failure
from thailand.fx import FxRate
from thailand.grouping import dedupe_user_facing_rows, normalize_model_code
from thailand.models import StoreScanResult, ThailandOffer
from thailand.registry import get_thailand_adapters
from thailand.source_health import load_health, record_observation, should_skip, skip_reason
from thailand.formatting import format_target_model_message
from thailand.target_model import (
    ADVICE_EXACT_SEARCH,
    EQUIVALENT,
    EXACT,
    NOT_FOUND,
    SAME_FAMILY,
    build_target_model,
    family_stem,
    match_detail,
    match_level,
    search_collected_offers,
)
from thailand.verification import compute_verification

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _deal(**kwargs) -> RankedDeal:
    offer = {
        "store": "regard",
        "external_id": "1",
        "name": "ASUS ROG Strix G16 G614PR-TS113W",
        "sku": "G614PR-TS113W",
        "mpn": "G614PR-TS113W",
    }
    offer.update(kwargs.pop("offer", {}))
    defaults = dict(
        score=80.0,
        reasons=["x"],
        offer=offer,
        cluster_name="ASUS ROG Strix G16 G614PR-TS113W",
        gpu="RTX 5070 Ti",
        store="regard",
        price=220000,
        url="https://example.test/g614",
        ram_gb=16,
        ssd_gb=1024,
        screen_inch=16.0,
        cpu="AMD Ryzen 9 8940HX",
        confidence=90,
        historical_min=220000,
        history_started_at="2020-01-01T00:00:00+00:00",
    )
    defaults.update(kwargs)
    return RankedDeal(**defaults)


def _fx() -> FxRate:
    return FxRate(
        source="test",
        currency="THB",
        nominal=1,
        official_rate=1.0,
        rub_per_thb=1.0,
        published_date=None,
        fetched_at=T0,
    )


def _offer(store: str, code: str, price: int, *, available: bool = True, gpu: str = "RTX 5070 Ti") -> ThailandOffer:
    offer = ThailandOffer(
        store=store,
        external_id=f"{store}-{code}",
        name=f"Notebook {code}",
        url=f"https://example.test/{code}",
        price_thb=price,
        available=available,
        collected_at=T0,
        sku=code,
        manufacturer_part_number=code,
        gpu=gpu,
        gpu_source="structured_api",
        cpu="AMD Ryzen 9 8940HX",
        ram_gb=16,
        ssd_gb=1024,
        screen_size_inch=16.0,
        availability_status="in_stock" if available else "out_of_stock",
        availability_confirmed=True,
        availability_source="structured_api",
        price_source="structured_api",
        channel="direct",
    )
    return compute_verification(offer)


class TargetMatchTests(unittest.TestCase):
    def test_exact_prefixed_sku(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("speedcom", "ASUS-G614PR-TS113W", 89990)
        self.assertEqual(match_level(target, offer), EXACT)
        self.assertEqual(normalize_model_code("ASUS-G614PR-TS113W"), "G614PR-TS113W")

    def test_same_family_regional_suffix(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("jib", "G614PR-TS999W", 90000)
        self.assertEqual(match_level(target, offer), SAME_FAMILY)

    def test_prefixed_sku_same_family(self) -> None:
        target = build_target_model(_deal(offer={"sku": "ASUS-G614PR-TS113W", "mpn": "ASUS-G614PR-TS113W"}))
        offer = _offer("jib", "G614PR-TS999W", 90000)
        self.assertEqual(match_level(target, offer), SAME_FAMILY)

    def test_different_asus_platforms_are_not_same_family(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("jib", "G614FR-TS235W", 119990)
        self.assertNotEqual(match_level(target, offer), SAME_FAMILY)
        self.assertNotEqual(match_level(target, offer), EXACT)

    def test_msi_9s7_prefix_is_not_a_family(self) -> None:
        target = build_target_model(
            _deal(
                cluster_name="MSI Katana 9S7-15M361-814",
                offer={"sku": "9S7-15M361-814", "mpn": "9S7-15M361-814"},
            )
        )
        offer = _offer("jib", "9S7-99ZZZZ-111", 100000)
        offer.ram_gb = 32
        self.assertNotEqual(match_level(target, offer), SAME_FAMILY)
        self.assertNotEqual(match_level(target, offer), EXACT)

    def test_short_generic_prefix_is_not_a_family(self) -> None:
        from thailand.target_model import family_stem

        self.assertIsNone(family_stem("9S7-15M361-814"))
        self.assertIsNone(family_stem("AB-123456"))
        self.assertEqual(family_stem("G614PR-TS113W"), "G614PR")
        self.assertEqual(family_stem("G614PR"), "G614PR")
        self.assertIsNone(family_stem("TS113W"))
        self.assertIsNone(family_stem("90NR"))
        self.assertIsNone(family_stem("83LU"))

    def test_production_g614pr_mpn_is_same_family(self) -> None:
        target = build_target_model(
            _deal(
                cluster_name="Ноутбук ASUS ROG Strix G16 G614PR-RV027 90NR0NJ7-M001J0",
                offer={"sku": "90NR0NJ7-M001J0", "mpn": "90NR0NJ7-M001J0"},
                price=262990,
                store="citilink",
            )
        )
        self.assertEqual(target["canonical_model_code"], "90NR0NJ7-M001J0")
        self.assertIn("G614PR-RV027", target["model_codes"])
        self.assertEqual(target["family_keys"], ["G614PR"])
        offer = _offer("jib", "G614PR-TS113W", 86990)
        detail = match_detail(target, offer)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["match_level"], SAME_FAMILY)
        self.assertEqual(detail["family_key"], "G614PR")
        self.assertEqual(detail["match_basis"], "alternative_model_code")
        self.assertEqual(detail["matched_identifier"], "G614PR-RV027")
        self.assertEqual(detail["matched_offer_identifier"], "G614PR-TS113W")
        found = search_collected_offers(target, [offer], fx=_fx())
        self.assertEqual(found["match_level"], SAME_FAMILY)
        self.assertEqual(found["family_key"], "G614PR")
        text = format_target_model_message(target, found)
        self.assertIn("Та же серия / семейство:", text)
        self.assertIn("ASUS ROG Strix G16 G614PR", text)
        self.assertIn("G614PR-RV027", text)
        self.assertIn("G614PR-TS113W", text)
        self.assertIn("Модификация и региональный индекс отличаются.", text)
        self.assertNotIn("EXACT", text)

    def test_exact_when_alternative_full_code_matches(self) -> None:
        target = build_target_model(
            _deal(
                cluster_name="ASUS ROG Strix G16 G614PR-TS113W",
                offer={"sku": "90NR0NJ7-M00999", "mpn": "90NR0NJ7-M00999"},
            )
        )
        self.assertEqual(target["canonical_model_code"], "90NR0NJ7-M00999")
        offer = _offer("speedcom", "ASUS-G614PR-TS113W", 89990)
        detail = match_detail(target, offer)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["match_level"], EXACT)
        self.assertEqual(detail["match_basis"], "exact_identifier")
        self.assertEqual(detail["matched_identifier"], "G614PR-TS113W")
        self.assertEqual(detail["matched_offer_identifier"], "G614PR-TS113W")

    def test_msi_9s7_alternative_is_not_same_family(self) -> None:
        target = build_target_model(
            _deal(
                cluster_name="MSI Raider 16 HX 9S7-15M361-814",
                offer={"sku": "9S7-15M361-814", "mpn": "9S7-15M361-814"},
            )
        )
        target["model_codes"] = ["9S7-15M361-814", "9S7"]
        target["family_stem"] = None
        offer = _offer("jib", "9S7-OTHER-999", 100000)
        offer.ram_gb = 32
        self.assertIsNone(family_stem("9S7"))
        self.assertNotEqual(match_level(target, offer), SAME_FAMILY)
        self.assertNotEqual(match_level(target, offer), EXACT)

    def test_conflicting_families_are_not_matched(self) -> None:
        target = build_target_model(_deal())
        target["model_codes"] = ["G614PR-RV027", "G614FR-TS235W", "90NR0NJ7-M001J0"]
        target["canonical_model_code"] = "90NR0NJ7-M001J0"
        target["family_stem"] = None
        offer = _offer("jib", "G614PR-TS113W", 86990)
        offer.ram_gb = 64
        self.assertNotEqual(match_level(target, offer), SAME_FAMILY)
        self.assertNotEqual(match_level(target, offer), EXACT)

    def test_asus_family_pairs(self) -> None:
        rv = build_target_model(
            _deal(
                cluster_name="ASUS ROG Strix G16 G614PR-RV027",
                offer={"sku": "G614PR-RV027", "mpn": "G614PR-RV027"},
            )
        )
        self.assertEqual(match_level(rv, _offer("jib", "G614PR-TS113W", 86990)), SAME_FAMILY)
        self.assertNotEqual(match_level(rv, _offer("jib", "G614FR-TS235W", 119990)), SAME_FAMILY)
        gu = build_target_model(
            _deal(
                cluster_name="ASUS Zephyrus GU606AW-S123",
                offer={"sku": "GU606AW-S123", "mpn": "GU606AW-S123"},
                gpu="RTX 5080",
            )
        )
        same = _offer("jib", "GU606AW-S999", 150000, gpu="RTX 5080")
        exact = _offer("speedcom", "ASUS-GU606AW-S123", 149000, gpu="RTX 5080")
        self.assertEqual(match_level(gu, same), SAME_FAMILY)
        self.assertEqual(match_level(gu, exact), EXACT)

    def test_same_family_reports_spec_differences(self) -> None:
        target = build_target_model(
            _deal(
                cluster_name="ASUS ROG Strix G16 G614PR-RV027",
                offer={"sku": "G614PR-RV027", "mpn": "G614PR-RV027"},
            )
        )
        offer = _offer("jib", "G614PR-TS113W", 86990)
        offer.ram_gb = 32
        offer.gpu = "RTX 5080"
        detail = match_detail(target, offer)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["match_level"], SAME_FAMILY)
        text = " ".join(detail["spec_differences"])
        self.assertIn("RAM", text)
        self.assertIn("GPU", text)

    def test_other_brands_do_not_gain_false_families(self) -> None:
        cases = [
            ("Lenovo Legion 83LU005KTA", "83LU005KTA", "83LU009KTA"),
            ("MSI Vector 9S7-15MM72-033", "9S7-15MM72-033", "9S7-17S372-240"),
            ("GigaByte A18 DYJG3KZBC4SD", "DYJG3KZBC4SD", "DYHG5KZCC4SD"),
            ("Acer Helios PHN16S-71-76AZ", "PHN16S-71-99AZ", "AN515-58-70AZ"),
        ]
        for name, left, right in cases:
            target = build_target_model(
                _deal(cluster_name=name, offer={"sku": left, "mpn": left})
            )
            offer = _offer("jib", right, 100000)
            offer.ram_gb = 64
            self.assertNotEqual(match_level(target, offer), SAME_FAMILY, name)
            self.assertNotEqual(match_level(target, offer), EXACT, name)
        acer = build_target_model(
            _deal(
                cluster_name="Acer Helios PHN16S-71-76AZ",
                offer={"sku": "PHN16S-71-76AZ", "mpn": "PHN16S-71-76AZ"},
            )
        )
        self.assertEqual(
            match_level(acer, _offer("jib", "PHN16S-71-99AZ", 100000)),
            SAME_FAMILY,
        )
        maibenben = build_target_model(
            _deal(
                cluster_name="Ноутбук Maibenben X16F-R98957T0GGRLG4E10",
                offer={"sku": "X16F-R98957T0GGRLG4E10", "mpn": "X16F-R98957T0GGRLG4E10"},
            )
        )
        asus = _offer("jib", "G614PR-TS113W", 86990)
        self.assertEqual(match_level(maibenben, asus), EQUIVALENT)
        self.assertNotEqual(match_level(maibenben, asus), SAME_FAMILY)

    def test_exact_beats_same_family(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("jib", "G614PR-TS113W", 86990)
        self.assertEqual(match_level(target, offer), EXACT)

    def test_same_gpu_only_is_not_same_family(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("jib", "UNRELATED-1", 80000)
        offer.cpu = None
        offer.ram_gb = None
        offer.ssd_gb = None
        offer.manufacturer_part_number = None
        offer.sku = None
        offer.name = "Some RTX laptop"
        self.assertNotEqual(match_level(target, offer), SAME_FAMILY)
        self.assertNotEqual(match_level(target, offer), EXACT)

    def test_equivalent_same_config(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("invadeit", "OTHER-MODEL-99", 91000)
        self.assertEqual(match_level(target, offer), EQUIVALENT)

    def test_different_ram_not_equivalent(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("invadeit", "OTHER-MODEL-99", 91000)
        offer.ram_gb = 32
        self.assertIsNone(match_level(target, offer))

    def test_no_unsafe_prefix_strip(self) -> None:
        self.assertNotEqual(normalize_model_code("FOO-G614PR-TS113W"), "G614PR-TS113W")
        target = build_target_model(_deal())
        offer = _offer("jib", "ZZNOTAMODEL", 80000)
        offer.name = "Gaming laptop"
        self.assertNotEqual(match_level(target, offer), EXACT)

    def test_query_does_not_assign_gpu(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("jib", "NO-GPU-CODE", 1000, gpu=None)
        offer.gpu = None
        offer.gpu_source = None
        offer.cpu = None
        offer.ram_gb = None
        offer.manufacturer_part_number = None
        offer.sku = "PLAIN"
        offer.name = "Notebook"
        match_level(target, offer)
        self.assertIsNone(offer.gpu)

    def test_over_cap_exact_stays_in_comparison_not_top(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("jib", "G614PR-TS113W", 330001)
        found = search_collected_offers(target, [offer], fx=_fx())
        self.assertEqual(found["match_level"], EXACT)
        self.assertTrue(found["exact_matches"][0]["over_cap"])
        self.assertFalse(offer_user_facing_eligible(offer, fx=_fx())[0])

    def test_out_of_stock_exact_not_top(self) -> None:
        target = build_target_model(_deal())
        offer = _offer("invadeit", "G614PR-TS113W", 90000, available=False)
        found = search_collected_offers(target, [offer], fx=_fx())
        self.assertEqual(found["match_level"], EXACT)
        self.assertTrue(found["exact_matches"][0]["out_of_stock"])
        self.assertFalse(offer_user_facing_eligible(offer, fx=_fx())[0])


class BuyTargetJobTests(unittest.TestCase):
    def test_one_buy_job_carries_target_and_scan_once(self) -> None:
        deal = _deal()
        captured = {}

        def _enqueue(job, root=None):
            captured["job"] = job
            return {"ok": True, "job_id": job["job_id"]}

        with tempfile.TemporaryDirectory() as tmp, patch(
            "buy_thailand_flow.enqueue_job", side_effect=_enqueue
        ):
            out = evaluate_buy_opportunity_flow(
                russian_deals=[deal],
                deliver=False,
                state_path=Path(tmp) / "state.json",
                jobs_dir=Path(tmp) / "jobs",
            )
        self.assertIn("thailand_job_id", out)
        self.assertEqual(out.get("thailand_scan_status"), "queued")
        job = captured["job"]
        self.assertEqual(job["trigger_type"], "buy_signal")
        self.assertEqual(job["target_model"]["canonical_model_code"], "G614PR-TS113W")
        calls = {"n": 0}

        def scan_fn(**kwargs):
            calls["n"] += 1
            self.assertEqual(kwargs["target_model"]["canonical_model_code"], "G614PR-TS113W")
            offer = _offer("jib", "G614PR-TS113W", 86990)
            return {
                "status": "ok",
                "snapshot_path": None,
                "_store_results": [StoreScanResult(store="jib", ok=True, offers=[offer])],
                "_fx": _fx(),
                "_best_match": None,
                "_comparison": None,
                "_top": [{"name": "other", "price_rub": 200000, "price_thb": 200000, "store": "jib", "gpu": "RTX 5070 Ti"}],
                "thailand_top": [{"name": "other", "price_rub": 200000, "price_thb": 200000, "store": "jib"}],
                "unverified_candidates": [],
                "price_cap": {"over_cap_count": 0, "max_tracked_price_rub": 300000, "fx_usable": True},
                "fx_usable_for_verdict": True,
                "target_model": kwargs["target_model"],
                "target_search": search_collected_offers(kwargs["target_model"], [offer], fx=_fx()),
            }

        result = execute_thailand_job(job, deliver=False, scan_fn=scan_fn, state_path=Path(tempfile.mkdtemp()) / "s.json")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(result["messages"]), 1)
        self.assertIn("ЧТО ПОКУПАТЬ СЕЙЧАС", result["messages"][0])
        self.assertNotIn("ЛУЧШИЕ ЦЕНЫ", result["messages"][0])
        self.assertNotIn("РАЗОВАЯ ПРОВЕРКА", result["messages"][0])


class PickerTests(unittest.TestCase):
    def test_button_shows_model_gpu_and_price(self) -> None:
        deal = _deal(cluster_name="Ноутбук ASUS ROG Strix G16 G614PR-TS113W", price=257540)
        label = picker_button_label(deal, 1)
        self.assertIn("ASUS ROG Strix", label)
        self.assertIn("5070 Ti", label)
        self.assertIn("257 540", label)
        self.assertNotIn("TS113W", label)
        self.assertLessEqual(len(CALLBACK_MODEL_PICK + model_callback_token(deal.cluster_name)), 64)

    def test_long_name_is_shortened_and_sku_only_falls_back(self) -> None:
        long_name = "Ноутбук " + ("Gigabyte Aorus " * 8) + "DYJG3KZBC4SD"
        label = picker_button_label(_deal(cluster_name=long_name, gpu="RTX 5080", price=253713), 1)
        self.assertLessEqual(len(label), 64)
        self.assertIn("5080", label)
        self.assertIn("253 713", label)
        bare = _deal(cluster_name="DYJG3KZBC4SD", gpu="RTX 5080", price=253713, offer={"sku": "DYJG3KZBC4SD", "mpn": "DYJG3KZBC4SD"})
        self.assertIsNone(friendly_model_label(bare))
        self.assertIn("DYJG3KZBC4SD", picker_button_label(bare, 2))

    def test_duplicate_clusters_collapse(self) -> None:
        first = _deal(cluster_name="ASUS ROG Strix G16")
        second = _deal(cluster_name="ASUS ROG Strix G16", price=240000)
        other = _deal(cluster_name="MSI Vector 16", offer={"sku": "9S7-17S372-240", "mpn": "9S7-17S372-240"})
        with patch("russian_deals.load_russian_ranked_deals", return_value=[first, second, other]):
            deals = unique_russian_top("unused.db", limit=10)
        self.assertEqual([d.cluster_name for d in deals], ["ASUS ROG Strix G16", "MSI Vector 16"])

    def test_markets_button_and_callback_limit(self) -> None:
        text = json.dumps(_markets_keyboard(), ensure_ascii=False)
        self.assertIn("Найти модель из ТОП РФ в Таиланде", text)
        deals = [_deal(), _deal(cluster_name="Other", offer={"sku": "ABCDE-1", "mpn": "ABCDE-1"})]
        kb = model_picker_keyboard(deals)
        self.assertLessEqual(len(kb["inline_keyboard"]) - 1, 10)
        for row in kb["inline_keyboard"][:-1]:
            self.assertLessEqual(len(row[0]["callback_data"]), 64)
            self.assertTrue(row[0]["callback_data"].startswith(CALLBACK_MODEL_PICK))

    def test_non_admin_blocked_and_manual_does_not_touch_buy_state(self) -> None:
        update = {
            "callback_query": {
                "id": "1",
                "data": "ctrl:th_find",
                "message": {"chat": {"id": 5}, "message_id": 1},
            }
        }
        with patch("control_bot._is_admin", return_value=False), patch(
            "control_bot.answer_callback"
        ) as answer, patch("control_bot.handle_thailand_model_picker") as picker:
            process_update(MagicMock(), "token", update)
        answer.assert_called()
        picker.assert_not_called()
        deal = _deal(history_started_at=datetime.now(timezone.utc).isoformat())
        with tempfile.TemporaryDirectory() as tmp, patch(
            "buy_thailand_flow.enqueue_job", return_value={"ok": True, "job_id": "m1"}
        ):
            result = enqueue_model_thailand_job(deal, chat_id=1, jobs_dir=Path(tmp))
        self.assertTrue(result["ok"])
        self.assertEqual(result["trigger_type"], "manual_model_compare")
        self.assertEqual(result["target_model"]["canonical_model_code"], "G614PR-TS113W")
        state = Path(tmp) / "buy_opportunity_state.json"
        self.assertFalse(state.exists())

    def test_pick_enqueues_without_scan(self) -> None:
        deal = _deal()
        with patch("control_bot.unique_russian_top", return_value=[deal]), patch(
            "control_bot.send_message"
        ) as send, patch(
            "buy_thailand_flow.enqueue_model_thailand_job",
            return_value={"ok": True, "thailand_job_id": "abc"},
        ) as enq, patch("thailand.scanner.run_thailand_scan") as scan:
            handle_thailand_model_pick(MagicMock(), "t", 1, model_callback_token(deal.cluster_name))
        enq.assert_called_once()
        scan.assert_not_called()
        text = send.call_args.args[3]
        self.assertIn("Ищу в Таиланде:", text)
        self.assertIn("ASUS ROG Strix", text)
        self.assertIn("5070 Ti", text)
        self.assertIn("220 000", text)
        self.assertNotIn(model_callback_token(deal.cluster_name), text)


class RateLimitTests(unittest.TestCase):
    def test_429_code_and_no_generic_failures(self) -> None:
        code, detail = classify_store_failure(status_code=429)
        self.assertEqual(code, RATE_LIMITED)
        self.assertEqual(detail, "HTTP_429")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            record_observation("speedcom", ok=False, error_code="TIMEOUT", now=T0, path=path)
            before = load_health(path)["stores"]["speedcom"]["consecutive_failures"]
            record_observation(
                "speedcom",
                ok=False,
                error_code=RATE_LIMITED,
                retry_after_seconds=120,
                now=T0,
                path=path,
            )
            entry = load_health(path)["stores"]["speedcom"]
            self.assertEqual(entry["consecutive_failures"], before)
            self.assertEqual(entry["last_error_code"], RATE_LIMITED)
            until = datetime.fromisoformat(entry["rate_limit_until"])
            self.assertAlmostEqual((until - T0).total_seconds(), 120, delta=2)
            self.assertEqual(skip_reason("speedcom", now=T0, path=path), "rate_limit")
            self.assertTrue(should_skip("speedcom", now=T0, path=path))
            self.assertFalse(should_skip("speedcom", now=T0 + timedelta(seconds=121), path=path))

    def test_default_60_minutes_without_retry_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            record_observation("speedcom", ok=False, error_code=RATE_LIMITED, now=T0, path=path)
            until = datetime.fromisoformat(load_health(path)["stores"]["speedcom"]["rate_limit_until"])
            self.assertAlmostEqual((until - T0).total_seconds(), 60 * 60, delta=2)

    def test_cooldown_skips_network_other_stores_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            record_observation("speedcom", ok=False, error_code=RATE_LIMITED, now=T0, path=path)
            called = []

            class Adapter:
                def __init__(self, slug, enabled=True):
                    self.slug = slug
                    self.enabled = enabled
                    self.display_name = slug

                def collect(self, **kwargs):
                    called.append(self.slug)
                    return StoreScanResult(store=self.slug, ok=True, offers=[])

            adapters = [Adapter("jib"), Adapter("speedcom"), Adapter("banana", enabled=False)]
            with patch("thailand.scanner.get_thailand_adapters", return_value=adapters), patch(
                "thailand.source_health.health_file", return_value=path
            ), patch("thailand.source_health.datetime") as clock:
                clock.now.return_value = T0
                clock.fromisoformat = datetime.fromisoformat
                from thailand.scanner import collect_thailand_offers

                results, _offers, _u, _d = collect_thailand_offers(parallel=False)
            self.assertEqual(called, ["jib"])
            speed = next(r for r in results if r.store == "speedcom")
            self.assertEqual(speed.error_code, RATE_LIMITED)
            banana = next(r for r in results if r.store == "banana")
            self.assertTrue(banana.policy_disabled)

    def test_three_real_failures_still_open_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            record_observation("jib", ok=False, error_code=RATE_LIMITED, now=T0, path=path)
            for _ in range(3):
                record_observation("jib", ok=False, error_code="TIMEOUT", now=T0, path=path)
            self.assertEqual(skip_reason("jib", now=T0, path=path), "circuit")

    def test_bare_http_error_is_not_reclassified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "stores": {
                            "speedcom": {
                                "last_success_at": "2026-10-05T08:18:57+00:00",
                                "last_failure_at": T0.isoformat(),
                                "last_error_code": "HTTP_ERROR",
                                "consecutive_failures": 2,
                                "last_offer_count": 0,
                                "disabled_until": None,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            entry = load_health(path)["stores"]["speedcom"]
            self.assertEqual(entry["consecutive_failures"], 2)
            self.assertEqual(entry["last_error_code"], "HTTP_ERROR")
            self.assertFalse(should_skip("speedcom", now=T0, path=path))

    def test_explicit_429_on_speedcom_is_reclassified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "stores": {
                            "speedcom": {
                                "last_failure_at": T0.isoformat(),
                                "last_error_code": "HTTP_ERROR",
                                "technical_detail": "HTTP_429",
                                "consecutive_failures": 2,
                                "disabled_until": None,
                            },
                            "jib": {
                                "last_failure_at": T0.isoformat(),
                                "last_error_code": "HTTP_429",
                                "consecutive_failures": 2,
                                "disabled_until": None,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = load_health(path)["stores"]
            self.assertEqual(state["speedcom"]["last_error_code"], RATE_LIMITED)
            self.assertEqual(state["speedcom"]["consecutive_failures"], 0)
            self.assertEqual(state["jib"]["last_error_code"], "HTTP_429")
            self.assertEqual(state["jib"]["consecutive_failures"], 2)


class SpeedComBudgetTests(unittest.TestCase):
    def test_429_does_not_retry_and_catalog_is_not_fetched_twice(self) -> None:
        from thailand.speedcom import collect

        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "90", "content-type": "text/plain"}
        client = MagicMock()
        client.get.return_value = resp
        result = collect(client=client, target_codes=["G614PR-TS113W"])
        self.assertEqual(result.error_code, RATE_LIMITED)
        self.assertEqual(result.retry_after_seconds, 90)
        self.assertEqual(client.get.call_count, 1)


class SourcePolicyTests(unittest.TestCase):
    def test_banana_and_lazada_stay_disabled(self) -> None:
        enabled = {a.slug: a.enabled for a in get_thailand_adapters()}
        self.assertFalse(enabled["banana"])
        self.assertFalse(enabled["lazada"])
        self.assertFalse(ADVICE_EXACT_SEARCH)

    def test_g614pr_offline_replay(self) -> None:
        rows = [
            {
                "store": "jib",
                "store_label": "JIB",
                "external_id": "jib1",
                "name": "ASUS ROG STRIX G16 G614PR-TS113W",
                "manufacturer_part_number": "G614PR-TS113W",
                "sku": "G614PR-TS113W",
                "price_thb": 86990,
                "price_rub": 215862,
                "marketplace": False,
                "international_score": 70,
                "international_confidence": 90,
            },
            {
                "store": "speedcom",
                "store_label": "SpeedCom",
                "external_id": "sp1",
                "name": "ASUS ROG Strix G16 G614PR-TS113W",
                "manufacturer_part_number": "ASUS-G614PR-TS113W",
                "sku": "ASUS-G614PR-TS113W",
                "price_thb": 89990,
                "price_rub": 223307,
                "marketplace": False,
                "international_score": 70,
                "international_confidence": 90,
            },
        ]
        deduped, removed = dedupe_user_facing_rows(rows)
        self.assertEqual(removed, 1)
        self.assertEqual(deduped[0]["store"], "jib")
        self.assertEqual(deduped[0]["price_thb"], 86990)
        self.assertEqual(deduped[0]["alt_channels"][0]["store"], "speedcom")
        self.assertEqual(deduped[0]["alt_channels"][0]["price_thb"], 89990)


if __name__ == "__main__":
    unittest.main()
