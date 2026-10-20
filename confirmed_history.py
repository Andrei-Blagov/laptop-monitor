"""Configuration-bound observations in the existing price_history table.

Legacy rows are retained verbatim. They cannot prove which hardware was sold.
Only the common snapshot evidence gate can attach a configuration to a price.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from product_identity import normalize_cpu, normalize_identifier, normalize_resolution
from deal_ranking import canonical_gpu


def configuration_key(sku, specs):
    code = normalize_identifier(sku)
    if not code:
        return None
    try:
        gpu = canonical_gpu(specs.get('gpu'))
        ram, ssd = int(specs['ram_gb']), int(specs['ssd_gb'])
        screen = round(float(specs['screen_inch']), 2)
        resolution = normalize_resolution(specs.get('screen_resolution'))
        if not gpu or not resolution or min(ram, ssd, screen) <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    # The existing nominal 1 TB convention, not an arbitrary tolerance.
    ssd = 1024 if ssd == 1000 else ssd
    payload = [code, gpu, normalize_cpu(specs.get('cpu')), ram, ssd, screen, resolution]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()


def record_key(record):
    codes = {normalize_identifier(v) for v in record.get('sku_candidates') or ()} - {None}
    if len(codes) != 1:
        return None
    specs = {k: v.get('value') for k, v in (record.get('specs') or {}).items()}
    cpu_evidence = ((record.get('specs') or {}).get('cpu') or {}).get('evidence') or {}
    if not cpu_evidence.get('captured_at') or cpu_evidence.get('capture_state') == 'unknown':
        specs['cpu'] = None
    return configuration_key(next(iter(codes)), specs)


def select_history(rows, key):
    return [r for r in rows if key and r.get('configuration_key') == key]


def price_summary(rows, key, price, checked_at):
    from model_price_history import historical_min_for_history, parse_iso, price_at_or_before
    history = select_history(rows, key)
    now = parse_iso(checked_at)
    if not key or now is None:
        return {}
    history = [r for r in history if parse_iso(r.get('checked_at')) and parse_iso(r['checked_at']) <= now]
    history.append({'price': price, 'checked_at': checked_at, 'available': True})
    history.sort(key=lambda r: (parse_iso(r['checked_at']), r.get('id', 0)))
    minimum = historical_min_for_history(history)
    return {'minimum': minimum[0] if minimum else None,
            'minimum_at': minimum[1] if minimum else None,
            'price_7d': price_at_or_before(history, now - timedelta(days=7)),
            'price_30d': price_at_or_before(history, now - timedelta(days=30))}


def summary_lines(summary, price):
    from model_price_history import format_delta_line, format_rub
    return (f"7 дней: {format_delta_line(price, summary.get('price_7d'))}",
            f"30 дней: {format_delta_line(price, summary.get('price_30d'))}",
            f"Минимум этой комплектации в магазине: {format_rub(summary.get('minimum'))}")


def confirmed_records(document, *, now=None):
    from thai_catalog import russian_offer_views
    accepted, _ = russian_offer_views(document, now=now or datetime.now(timezone.utc))
    ids = {r['best_offer_id'] for r in accepted}
    records = {}
    ambiguous = set()
    for row in document.get('offers', ()):
        if row.get('offer_id') not in ids or not record_key(row):
            continue
        listing = (row['store'], row['external_id'])
        if listing in records:
            ambiguous.add(listing)
        records[listing] = row
    return {k: v for k, v in records.items() if k not in ambiguous}


def save_confirmed_prices(db_path, document, *, now=None):
    """Idempotent, additive recording; never backfill legacy evidence or clocks."""
    from storage import open_db
    records = confirmed_records(document, now=now)
    with open_db(db_path) as conn:
        for (store, external_id), record in records.items():
            product = conn.execute('SELECT * FROM products WHERE store=? AND external_id=?',
                                   (store, external_id)).fetchone()
            if (not product or product['price'] != record['public_price_rub'] or
                    product['price_checked_at'] != record['price_checked_at'] or
                    normalize_identifier(product['sku']) not in
                    {normalize_identifier(v) for v in record.get('sku_candidates', ())}):
                continue
            key = record_key(record)
            last = conn.execute('SELECT * FROM price_history WHERE product_id=? AND configuration_key=? '
                                'ORDER BY checked_at DESC, id DESC LIMIT 1', (product['id'], key)).fetchone()
            if last and last['price'] == product['price'] and last['available'] == product['available']:
                continue
            conn.execute('INSERT INTO price_history(product_id,price,available,checked_at,configuration_key) '
                         'VALUES(?,?,?,?,?)', (product['id'], product['price'], product['available'],
                                             record['price_checked_at'], key))
    return records


def load_model_history(db_path, product_id):
    """One confirmed configuration, shared by /history and its chart."""
    from control_bot import unique_russian_top
    from comparison import Offer, ProductMatch
    from storage import get_all_products_readonly, get_price_history_for_products_readonly
    products = {(p['store'], p['external_id']): p for p in get_all_products_readonly(db_path)}
    deals = unique_russian_top(db_path, limit=0)
    candidates = []
    selected = None
    for deal in deals:
        product = products.get((deal.store, str(deal.offer.get('external_id') or '')))
        if not product:
            continue
        key = configuration_key(deal.offer.get('sku'), vars(deal))
        if not key:
            continue
        candidates.append((deal, product, key))
        if int(product['id']) == int(product_id):
            selected = deal, product, key
    if selected is None:
        return None, {}
    deal, product, key = selected
    # TOP may deduplicate stores. Retain only offers proven in that same read.
    offers = [Offer(store=d.store, external_id=p['external_id'], name=d.cluster_name,
                    sku=d.offer.get('sku'), price=d.price, available=True, url=d.url,
                    product_id=int(p['id'])) for d, p, k in candidates if k == key]
    match = ProductMatch(normalized_sku=key, display_sku=deal.offer['sku'],
                         name=deal.cluster_name, offers=offers, match_method='confirmed_configuration')
    histories = get_price_history_for_products_readonly(db_path, [o.product_id for o in offers])
    return match, {pid: select_history(rows, key) for pid, rows in histories.items()}
