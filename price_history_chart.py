from __future__ import annotations

"""Telegram Price History v2 — PNG charts (read-only, in-memory)."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from comparison import Offer, ProductMatch, match_products
from model_price_history import (
    CALLBACK_DATA_LIMIT,
    CALLBACK_MENU,
    CALLBACK_PERIODS_PREFIX,
    MSK,
    _valid_price,
    build_model_history_report,
    find_cluster_by_product_id,
    format_rub,
    make_model_callback,
    parse_iso,
    store_label,
)
from storage import (
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_price_history_for_products_readonly,
    get_product_by_id_readonly,
)

logger = logging.getLogger(__name__)

PeriodKey = Literal["30", "90", "all"]

CALLBACK_CHART_PREFIX = "hist:chart:"

CHART_WIDTH_PX = 1200
CHART_HEIGHT_PX = 700
CHART_DPI = 100
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

PERIOD_LABELS = {
    "30": "30 дней",
    "90": "90 дней",
    "all": "Всё время",
}


@dataclass(frozen=True)
class StatePoint:
    checked_at: datetime
    price: int | None
    available: bool


@dataclass
class StoreSeries:
    store: str
    segments: list[list[tuple[datetime, float]]] = field(default_factory=list)


@dataclass
class ChartResult:
    ok: bool
    png: bytes | None = None
    caption: str = ""
    error: str | None = None
    period: PeriodKey | None = None
    product_id: int | None = None


def make_chart_callback(period: PeriodKey, product_id: int) -> str:
    if period not in ("30", "90", "all"):
        raise ValueError(f"invalid period: {period}")
    data = f"{CALLBACK_CHART_PREFIX}{period}:{int(product_id)}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data too long: {data!r}")
    return data


def parse_periods_callback(data: str) -> int | None:
    text = str(data or "")
    if not text.startswith(CALLBACK_PERIODS_PREFIX):
        return None
    raw = text[len(CALLBACK_PERIODS_PREFIX) :]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_chart_callback(data: str) -> tuple[PeriodKey, int] | None:
    text = str(data or "")
    if not text.startswith(CALLBACK_CHART_PREFIX):
        return None
    rest = text[len(CALLBACK_CHART_PREFIX) :]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        return None
    period_raw, pid_raw = parts
    if period_raw not in ("30", "90", "all"):
        return None
    try:
        return period_raw, int(pid_raw)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def chart_period_keyboard(product_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "30 дней",
                    "callback_data": make_chart_callback("30", product_id),
                },
                {
                    "text": "90 дней",
                    "callback_data": make_chart_callback("90", product_id),
                },
            ],
            [
                {
                    "text": "Всё время",
                    "callback_data": make_chart_callback("all", product_id),
                },
            ],
            [
                {
                    "text": "⬅️ Карточка",
                    "callback_data": make_model_callback(product_id),
                },
                {"text": "🏠 Меню", "callback_data": CALLBACK_MENU},
            ],
        ]
    }


def chart_result_keyboard(product_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "30 дней",
                    "callback_data": make_chart_callback("30", product_id),
                },
                {
                    "text": "90 дней",
                    "callback_data": make_chart_callback("90", product_id),
                },
                {
                    "text": "Всё время",
                    "callback_data": make_chart_callback("all", product_id),
                },
            ],
            [
                {
                    "text": "⬅️ Карточка",
                    "callback_data": make_model_callback(product_id),
                },
                {"text": "🏠 Меню", "callback_data": CALLBACK_MENU},
            ],
        ]
    }


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def period_start(
    period: PeriodKey,
    *,
    now: datetime,
    earliest: datetime | None,
) -> datetime:
    now_u = _as_utc(now)
    if period == "30":
        return now_u - timedelta(days=30)
    if period == "90":
        return now_u - timedelta(days=90)
    if earliest is None:
        return now_u
    return _as_utc(earliest)


def history_events(
    history: Sequence[Mapping[str, Any]],
) -> list[StatePoint]:
    events: list[StatePoint] = []
    for row in history:
        ts = parse_iso(row.get("checked_at"))
        if ts is None:
            continue
        available = bool(row.get("available"))
        price = _valid_price(row.get("price"))
        events.append(StatePoint(checked_at=ts, price=price, available=available))
    events.sort(key=lambda e: e.checked_at)
    return events


def seed_state_at_or_before(
    events: Sequence[StatePoint],
    start: datetime,
) -> StatePoint | None:
    start_u = _as_utc(start)
    seed: StatePoint | None = None
    for event in events:
        if event.checked_at <= start_u:
            seed = event
        else:
            break
    return seed


def build_step_segments(
    events: Sequence[StatePoint],
    *,
    start: datetime,
    now: datetime,
    current_price: int | None,
    current_available: bool,
) -> list[list[tuple[datetime, float]]]:
    """
    Build drawable step segments for one store.

    available=false creates a gap. Current unavailable does not extend to now.
    """
    start_u = _as_utc(start)
    now_u = _as_utc(now)
    seed = seed_state_at_or_before(events, start_u)
    future = [e for e in events if start_u < e.checked_at <= now_u]

    # Current product state appended if newer than last history in window.
    last_hist = events[-1] if events else None
    apply_current = True
    if last_hist is not None and last_hist.checked_at >= now_u:
        apply_current = False

    segments: list[list[tuple[datetime, float]]] = []
    seg: list[tuple[datetime, float]] = []

    def close_seg_at(t: datetime) -> None:
        nonlocal seg
        if not seg:
            return
        if seg[-1][0] < t:
            seg.append((t, seg[-1][1]))
        segments.append(seg)
        seg = []

    def open_or_update(t: datetime, price: int) -> None:
        nonlocal seg
        if not seg:
            seg = [(t, float(price))]
        else:
            seg.append((t, float(price)))

    # Initial state at period start.
    if (
        seed is not None
        and seed.available
        and seed.price is not None
    ):
        open_or_update(start_u, seed.price)

    for event in future:
        if event.available and event.price is not None:
            open_or_update(event.checked_at, event.price)
        else:
            # unavailable / invalid price → gap
            close_seg_at(event.checked_at)

    if apply_current:
        if current_available and current_price is not None:
            open_or_update(now_u, current_price)
            if seg:
                segments.append(seg)
                seg = []
        else:
            # Current unavailable: do not extend as available to now.
            if seg:
                segments.append(seg)
                seg = []
    else:
        if seg:
            # Hold last known drawable price to now if still available in last event.
            if last_hist is not None and last_hist.available and last_hist.price is not None:
                if seg[-1][0] < now_u:
                    seg.append((now_u, float(last_hist.price)))
            segments.append(seg)
            seg = []

    return segments


def earliest_valid_timestamp(
    histories: Mapping[int, Sequence[Mapping[str, Any]]],
) -> datetime | None:
    earliest: datetime | None = None
    for rows in histories.values():
        for row in rows:
            price = _valid_price(row.get("price"))
            if price is None:
                continue
            ts = parse_iso(row.get("checked_at"))
            if ts is None:
                continue
            if earliest is None or ts < earliest:
                earliest = ts
    return earliest


def build_store_series_for_cluster(
    match: ProductMatch,
    histories: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    start: datetime,
    now: datetime,
) -> list[StoreSeries]:
    # One series per store (first offer wins if duplicates).
    by_store: dict[str, Offer] = {}
    for offer in match.offers:
        if offer.store not in by_store:
            by_store[offer.store] = offer

    series_list: list[StoreSeries] = []
    for store in sorted(by_store.keys(), key=lambda s: store_label(s).lower()):
        offer = by_store[store]
        pid = int(offer.product_id) if offer.product_id is not None else None
        hist = histories.get(pid, []) if pid is not None else []
        events = history_events(hist)
        segments = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=_valid_price(offer.price),
            current_available=bool(offer.available),
        )
        series_list.append(StoreSeries(store=store, segments=segments))
    return series_list


def _short_title(name: str, max_len: int = 48) -> str:
    text = " ".join(str(name or "Модель").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _ruble_formatter(value: float, _pos: int) -> str:
    try:
        amount = int(round(value))
    except (TypeError, ValueError):
        return str(value)
    return f"{amount:,}".replace(",", " ")


def render_price_history_png(
    series: Sequence[StoreSeries],
    *,
    title: str,
    period_label: str,
    best_now: int | None = None,
    best_now_store: str | None = None,
    annotation_fresh_best: bool = False,
) -> bytes:
    """Render chart to PNG bytes. Always closes the figure."""
    fig = None
    try:
        fig_w = CHART_WIDTH_PX / CHART_DPI
        fig_h = CHART_HEIGHT_PX / CHART_DPI
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=CHART_DPI)
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#FAFAFA")

        plt.rcParams["font.family"] = "DejaVu Sans"
        for item in ax.get_xticklabels() + ax.get_yticklabels():
            item.set_fontfamily("DejaVu Sans")

        colors = plt.rcParams["axes.prop_cycle"].by_key().get("color") or [
            "#1f77b4",
            "#ff7f0e",
            "#2ca02c",
            "#d62728",
        ]
        total_points = 0
        for idx, store_series in enumerate(series):
            color = colors[idx % len(colors)]
            label = store_label(store_series.store)
            first = True
            for seg in store_series.segments:
                if len(seg) < 1:
                    continue
                xs = [p[0].astimezone(MSK).replace(tzinfo=None) for p in seg]
                ys = [p[1] for p in seg]
                total_points += len(seg)
                ax.plot(
                    xs,
                    ys,
                    drawstyle="steps-post",
                    color=color,
                    linewidth=2.0,
                    label=label if first else None,
                    marker="o" if len(seg) <= 8 else None,
                    markersize=3.5 if len(seg) <= 8 else None,
                )
                first = False

        ax.set_title(
            f"{_short_title(title)}\nПериод: {period_label}",
            fontsize=13,
            fontname="DejaVu Sans",
            pad=12,
        )
        ax.set_ylabel("₽", fontname="DejaVu Sans")
        ax.yaxis.set_major_formatter(FuncFormatter(_ruble_formatter))
        locator = AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        ax.grid(True, linestyle="--", alpha=0.35)
        if any(s.segments for s in series):
            ax.legend(loc="best", fontsize=9, framealpha=0.9)
        if (
            annotation_fresh_best
            and best_now is not None
            and best_now_store
        ):
            ax.annotate(
                f"Лучшая сейчас:\n{format_rub(best_now)} — {store_label(best_now_store)}",
                xy=(0.98, 0.02),
                xycoords="axes fraction",
                ha="right",
                va="bottom",
                fontsize=9,
                fontname="DejaVu Sans",
                bbox={
                    "boxstyle": "round,pad=0.35",
                    "facecolor": "white",
                    "edgecolor": "#CCCCCC",
                    "alpha": 0.9,
                },
            )

        fig.autofmt_xdate()
        fig.tight_layout()
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=CHART_DPI)
        data = buf.getvalue()
        buf.close()
        if not data.startswith(PNG_SIGNATURE):
            raise RuntimeError("invalid PNG output")
        return data
    finally:
        if fig is not None:
            plt.close(fig)


def format_chart_caption(
    *,
    name: str,
    period: PeriodKey,
    best_now: int | None,
    best_now_store: str | None,
    all_time_min: int | None,
    all_time_min_store: str | None,
    show_fresh_best: bool,
) -> str:
    lines = [
        f"📊 {_short_title(name, 60)}",
        f"Период: {PERIOD_LABELS.get(period, period)}",
    ]
    if show_fresh_best and best_now is not None and best_now_store:
        lines.append("")
        lines.append("Лучшая сейчас:")
        lines.append(f"{format_rub(best_now)} — {store_label(best_now_store)}")
    if all_time_min is not None and all_time_min_store:
        lines.append("")
        lines.append("Исторический минимум:")
        lines.append(f"{format_rub(all_time_min)} — {store_label(all_time_min_store)}")
    caption = "\n".join(lines)
    # Telegram caption soft limit ~1024
    return caption[:1000]


def _load_match_and_histories(
    db_path: Path | str,
    product_id: int,
) -> tuple[ProductMatch | None, dict[int, list[dict]], dict | None]:
    product = get_product_by_id_readonly(db_path, product_id)
    if product is None:
        return None, {}, None
    products = get_all_products_readonly(db_path)
    identifiers = get_all_identifiers_readonly(db_path)
    matches = match_products(products, identifiers).matches
    match = find_cluster_by_product_id(matches, int(product_id))
    if match is None:
        offer = Offer(
            store=str(product["store"]),
            external_id=str(product["external_id"]),
            name=str(product.get("name") or "?"),
            sku=product.get("sku"),
            price=product.get("price"),
            available=bool(product.get("available")),
            url=product.get("url"),
            product_id=int(product["id"]),
        )
        match = ProductMatch(
            normalized_sku=str(product.get("sku") or product["external_id"]),
            display_sku=str(product.get("sku") or product["external_id"]),
            name=str(product.get("name") or "?"),
            offers=[offer],
            match_method="singleton",
        )
    pids = [int(o.product_id) for o in match.offers if o.product_id is not None]
    histories = get_price_history_for_products_readonly(db_path, pids)
    return match, histories, product


def build_price_history_chart(
    db_path: Path | str,
    product_id: int,
    period: PeriodKey,
    *,
    now: datetime | None = None,
) -> ChartResult:
    """Read-only chart builder. Never writes DB / never raises to caller."""
    now_u = _as_utc(now or datetime.now(timezone.utc))
    try:
        if period not in ("30", "90", "all"):
            return ChartResult(
                ok=False,
                error="Некорректный период.",
                product_id=int(product_id),
            )
        match, histories, product = _load_match_and_histories(db_path, product_id)
        if product is None or match is None:
            return ChartResult(
                ok=False,
                error="Модель не найдена (product удалён или неизвестен).",
                product_id=int(product_id),
                period=period,
            )

        earliest = earliest_valid_timestamp(histories)
        start = period_start(period, now=now_u, earliest=earliest)
        series = build_store_series_for_cluster(
            match, histories, start=start, now=now_u
        )
        has_points = any(seg for s in series for seg in s.segments)
        if not has_points:
            return ChartResult(
                ok=False,
                error="Недостаточно данных для построения графика.",
                product_id=int(product_id),
                period=period,
            )

        report = build_model_history_report(db_path, product_id, now=now_u)
        show_fresh = (
            not report.stale_current
            and report.best_now is not None
            and report.best_now_store is not None
        )
        png = render_price_history_png(
            series,
            title=match.name,
            period_label=PERIOD_LABELS[period],
            best_now=report.best_now,
            best_now_store=report.best_now_store,
            annotation_fresh_best=show_fresh,
        )
        caption = format_chart_caption(
            name=match.name,
            period=period,
            best_now=report.best_now,
            best_now_store=report.best_now_store,
            all_time_min=report.all_time_min,
            all_time_min_store=report.all_time_min_store,
            show_fresh_best=show_fresh,
        )
        return ChartResult(
            ok=True,
            png=png,
            caption=caption,
            period=period,
            product_id=int(product_id),
        )
    except Exception as exc:
        logger.exception("price history chart failed product_id=%s", product_id)
        return ChartResult(
            ok=False,
            error="Не удалось построить график. Текстовая история доступна.",
            product_id=int(product_id),
            period=period,
        )


def open_figure_count() -> int:
    return len(plt.get_fignums())
