#!/usr/bin/env python3
"""Local/copy diagnostic: prove pipeline enqueue vs worker scan separation."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from buy_thailand_flow import evaluate_buy_opportunity_flow
from deal_ranking import RankedDeal
from russian_deals import load_russian_ranked_deals
from thailand.job_queue import pending_count
from thailand.worker import drain


def _sample_deal() -> RankedDeal:
    return RankedDeal(
        score=91.0,
        reasons=["diag"],
        offer={"store": "kns", "external_id": "diag1", "name": "Diag MSI"},
        cluster_name="Diag MSI Vector",
        gpu="RTX 5070 Ti",
        store="kns",
        price=229990,
        url="https://example.test/diag",
        ram_gb=32,
        ssd_gb=1024,
        screen_inch=17.0,
        cpu="Intel Core Ultra 9 275HX",
        confidence=100,
        historical_min=224990,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None, help="Optional prod-copy DB")
    parser.add_argument("--jobs-dir", type=Path, default=None)
    parser.add_argument("--run-worker", action="store_true")
    parser.add_argument("--live-scan", action="store_true", help="Allow real Thailand HTTP")
    args = parser.parse_args(argv)

    tmp_owned = None
    if args.jobs_dir is None:
        tmp_owned = tempfile.TemporaryDirectory()
        jobs_dir = Path(tmp_owned.name) / "jobs"
        state_path = Path(tmp_owned.name) / "state.json"
    else:
        jobs_dir = args.jobs_dir
        state_path = jobs_dir.parent / "buy_opportunity_state.diag.json"

    deals: list[RankedDeal]
    if args.db and args.db.exists():
        deals = load_russian_ranked_deals(args.db)
        print(f"loaded_deals={len(deals)} from {args.db}")
        # DB counts before worker
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        before = {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "products",
                "price_history",
                "pipeline_runs",
                "store_runs",
            )
        }
        con.close()
        print("db_counts_before=", before)
    else:
        deals = [_sample_deal()]
        before = None
        print("using synthetic BUY deal")

    t0 = time.perf_counter()
    out = evaluate_buy_opportunity_flow(
        russian_deals=deals,
        deliver=False,
        state_path=state_path,
        jobs_dir=jobs_dir,
        source_pipeline_run_id=999,
    )
    enqueue_s = time.perf_counter() - t0
    print("enqueue_result=", {
        k: out.get(k)
        for k in (
            "buy_actionable",
            "thailand_scan_triggered",
            "thailand_scan_status",
            "thailand_job_id",
            "messages_sent",
        )
    })
    print(f"russian_enqueue_seconds={enqueue_s:.4f}")
    print(f"pending_jobs={pending_count(jobs_dir)}")

    if not args.run_worker:
        print("worker_skipped (pass --run-worker)")
        if tmp_owned:
            tmp_owned.cleanup()
        return 0

    def fake_scan(**kwargs):
        time.sleep(0.05)
        return {
            "status": "partial",
            "snapshot_path": str(jobs_dir / "_diag_snap.json"),
            "stores": [
                {"store": "jib", "ok": True, "count": 1},
                {"store": "advice", "ok": False, "error": "HTTP_403"},
                {"store": "banana", "ok": False, "error": "HTTP_403"},
            ],
            "unverified_candidates": [],
            "thailand_top": [],
            "_store_results": [],
            "_fx": None,
            "_best_match": None,
            "_comparison": None,
            "_top": [],
        }

    scan_fn = None if args.live_scan else fake_scan
    t1 = time.perf_counter()
    n = drain(
        jobs_dir=jobs_dir,
        state_path=state_path,
        deliver=False,
        scan_fn=scan_fn,
    )
    worker_s = time.perf_counter() - t1
    print(f"worker_processed={n}")
    print(f"worker_seconds={worker_s:.4f}")
    print(f"pending_after={pending_count(jobs_dir)}")
    archives = list((jobs_dir / "archive").glob("*.json"))
    failed = list((jobs_dir / "failed").glob("*.json"))
    print(f"archive={len(archives)} failed={len(failed)}")
    if archives:
        print("archive_sample=", json.loads(archives[-1].read_text(encoding="utf-8")).get("status"))

    if before and args.db:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        after = {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in before
        }
        con.close()
        print("db_counts_after=", after)
        print("db_unchanged=", before == after)

    if tmp_owned:
        tmp_owned.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
