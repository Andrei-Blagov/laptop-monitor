#!/usr/bin/env python3
"""Generate n8n workflow JSON for Daily Digest v1."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPE = ROOT / "deploy" / "n8n" / "pipeline-event-receiver.json"
DIGEST = ROOT / "deploy" / "n8n" / "daily-digest.json"

ARCHIVE_JS = r"""// Archive verified pipeline.completed summary into workflow static data.
// Retention: 48h or max 100. No secrets / raw body / headers.
const item = $input.first().json || {};
const staticData = $getWorkflowStaticData('global');
staticData.pipelineHistory = Array.isArray(staticData.pipelineHistory) ? staticData.pipelineHistory : [];

function parseIso(v) {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

const stores = (Array.isArray(item.stores) ? item.stores : []).map((s) => ({
  slug: s && s.slug,
  display_name: (s && (s.display_name || s.slug)) || 'store',
  status: s && s.status,
}));
const top_deals = (Array.isArray(item.top_deals) ? item.top_deals : []).map((d) => ({
  rank: d && d.rank,
  name: d && d.name,
  store: d && d.store,
  price: d && d.price,
  score: d && d.score,
  url: d && d.url,
  saving: d && (d.saving || d.cross_store_saving),
}));
const summary = {
  run_id: item.run_id,
  status: item.status,
  started_at: item.started_at,
  finished_at: item.finished_at,
  instance_id: item.instance_id,
  alerts_created: item.alerts_created,
  messages_sent: item.messages_sent,
  messages_failed: item.messages_failed,
  stores,
  top_deals,
};

const runKey = summary.run_id === undefined || summary.run_id === null ? '' : String(summary.run_id);
let next = [];
let replaced = false;
for (const prev of staticData.pipelineHistory) {
  if (runKey && String(prev.run_id) === runKey) {
    next.push(summary);
    replaced = true;
  } else {
    next.push(prev);
  }
}
if (!replaced) next.push(summary);

const now = Date.now();
const cutoff = now - 48 * 3600 * 1000;
next = next
  .map((row) => ({ row, ts: parseIso(row.finished_at || row.started_at) }))
  .filter((x) => x.ts && x.ts.getTime() >= cutoff)
  .sort((a, b) => b.ts - a.ts)
  .slice(0, 100)
  .map((x) => x.row);

staticData.pipelineHistory = next;
return [{ json: { ...item, archived: true, history_size: next.length } }];
"""

EXPORT_JS = r"""// Called by Daily Digest via Execute Workflow — return rolling history only.
const staticData = $getWorkflowStaticData('global');
const history = Array.isArray(staticData.pipelineHistory) ? staticData.pipelineHistory : [];
return [{ json: { history, history_size: history.length } }];
"""

DIGEST_JS = r"""// Daily Digest v1 — builds Telegram text from Pipeline Events history.
// Production dedupe: MSK YYYY-MM-DD in this workflow static data.
// test_mode=true => TEST header, do not mark production date sent.
const input = $input.first().json || {};
const staticData = $getWorkflowStaticData('global');
const testMode = staticData._digestTestMode === true || input.test_mode === true;
delete staticData._digestTestMode;
const history = Array.isArray(input.history) ? input.history : [];
const MAX_PRICE = 300000;

function parseIso(v) {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}
function mskParts(d) {
  const fmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/Moscow',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  });
  return Object.fromEntries(fmt.formatToParts(d).filter((p) => p.type !== 'literal').map((p) => [p.type, p.value]));
}
function formatMsk(iso) {
  const d = parseIso(iso);
  if (!d) return '—';
  const p = mskParts(d);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} MSK`;
}
function mskDateKey(d = new Date()) {
  const p = mskParts(d);
  return `${p.year}-${p.month}-${p.day}`;
}
function formatPrice(price) {
  const n = Math.round(Number(price));
  if (!Number.isFinite(n)) return String(price);
  return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' ₽';
}

const now = new Date();
const cutoff = now.getTime() - 24 * 3600 * 1000;
const window = history
  .filter((r) => String(r.instance_id || '') === 'vps-prod')
  .map((r) => ({ r, ts: parseIso(r.finished_at || r.started_at) }))
  .filter((x) => x.ts && x.ts.getTime() >= cutoff)
  .sort((a, b) => b.ts - a.ts)
  .map((x) => x.r);

const title = testMode
  ? '🧪 Laptop Monitor — Daily Digest TEST'
  : '📊 Laptop Monitor — Daily Digest';

const dateKey = mskDateKey(now);
if (!testMode && staticData.lastDigestSentDate === dateKey) {
  return [{ json: { digest_result: 'deduped', date_key: dateKey, text: null } }];
}

let text;
if (!window.length) {
  text = title + '\n\nЗа последние 24 часа запусков не было.';
} else {
  let success = 0, partial = 0, failed = 0, alerts = 0, sent = 0, msgFailed = 0;
  for (const r of window) {
    const st = String(r.status || '').toLowerCase();
    if (st === 'success') success += 1;
    else if (st === 'partial') partial += 1;
    else if (st === 'failed') failed += 1;
    alerts += Number(r.alerts_created || 0) || 0;
    sent += Number(r.messages_sent || 0) || 0;
    msgFailed += Number(r.messages_failed || 0) || 0;
  }
  const latest = window[0];
  const storeLines = (Array.isArray(latest.stores) ? latest.stores : []).map((s) => {
    const name = String(s.display_name || s.slug || 'store');
    const st = String(s.status || '').toLowerCase();
    return (st === 'ok' || st === 'success') ? `✅ ${name}` : `⚠️ ${name} — FAILED`;
  });
  let source = null;
  for (const r of window) {
    const st = String(r.status || '').toLowerCase();
    if (st === 'success' || st === 'partial') { source = r; break; }
  }
  const tops = [];
  for (const d of (source && Array.isArray(source.top_deals) ? source.top_deals : [])) {
    const price = Number(d.price);
    if (!Number.isFinite(price) || price > MAX_PRICE) continue;
    tops.push(d);
    if (tops.length >= 5) break;
  }
  const topLines = [];
  tops.forEach((d, idx) => {
    const i = idx + 1;
    let line = `${i}. ${d.name || '—'} — ${formatPrice(d.price)} — ${d.store || '—'}`;
    const saving = Number(d.saving);
    if (Number.isFinite(saving) && saving > 0) line += ` (экономия ${formatPrice(saving)})`;
    topLines.push(line);
    if (i <= 3 && d.url) topLines.push(`   ${d.url}`);
  });
  text = [
    title,
    'Период: последние 24 часа',
    '',
    `Запуски: ${window.length}`,
    `✅ SUCCESS: ${success}`,
    `⚠️ PARTIAL: ${partial}`,
    `🔴 FAILED: ${failed}`,
    '',
    `Ценовых событий: ${alerts}`,
    `Telegram: ${sent} sent / ${msgFailed} failed`,
    '',
    'Магазины:',
    ...(storeLines.length ? storeLines : ['• (нет данных)']),
    '',
    'ТОП ДО 300 000 ₽:',
    ...(topLines.length ? topLines : ['• (нет предложений в окне)']),
    '',
    'Последний запуск:',
    `${formatMsk(latest.finished_at || latest.started_at)} — ${String(latest.status || 'unknown').toUpperCase()}`,
  ].join('\n');
  if (text.length > 3900) {
    // shrink: drop URLs / keep TOP3
    const shortTops = [];
    tops.slice(0, 3).forEach((d, idx) => {
      shortTops.push(`${idx + 1}. ${d.name || '—'} — ${formatPrice(d.price)} — ${d.store || '—'}`);
    });
    text = [
      title,
      'Период: последние 24 часа',
      '',
      `Запуски: ${window.length}`,
      `✅ SUCCESS: ${success}`,
      `⚠️ PARTIAL: ${partial}`,
      `🔴 FAILED: ${failed}`,
      '',
      `Ценовых событий: ${alerts}`,
      `Telegram: ${sent} sent / ${msgFailed} failed`,
      '',
      'Магазины:',
      ...(storeLines.length ? storeLines : ['• (нет данных)']),
      '',
      'ТОП ДО 300 000 ₽:',
      ...(shortTops.length ? shortTops : ['• (нет предложений в окне)']),
      '',
      'Последний запуск:',
      `${formatMsk(latest.finished_at || latest.started_at)} — ${String(latest.status || 'unknown').toUpperCase()}`,
    ].join('\n');
  }
}

const token = $env.LAPTOP_MONITOR_TELEGRAM_BOT_TOKEN;
const chatId = String($env.LAPTOP_MONITOR_TELEGRAM_CHAT_ID || '').split(',')[0].trim();
if (!token || !chatId) {
  return [{ json: { digest_result: 'telegram_env_missing', date_key: dateKey, text } }];
}

try {
  const resp = await this.helpers.httpRequest({
    method: 'POST',
    url: 'https://api.telegram.org/bot' + token + '/sendMessage',
    headers: { 'content-type': 'application/json' },
    body: { chat_id: chatId, text, disable_web_page_preview: true },
    json: true,
    timeout: 10000,
  });
  if (!resp || resp.ok !== true) {
    return [{ json: { digest_result: 'telegram_rejected', date_key: dateKey, text } }];
  }
} catch (e) {
  return [{ json: { digest_result: 'telegram_error', date_key: dateKey, text } }];
}

if (!testMode) {
  staticData.lastDigestSentDate = dateKey;
}
return [{ json: { digest_result: 'sent', date_key: dateKey, test_mode: testMode, runs: window.length } }];
"""


def main() -> None:
    pipe = json.loads(PIPE.read_text(encoding="utf-8"))

    # Insert Archive node after HMAC OK
    archive_node = {
        "parameters": {"mode": "runOnceForAllItems", "jsCode": ARCHIVE_JS},
        "id": "lm-arch-001",
        "name": "Archive run summary",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [650, -40],
    }
    export_trigger = {
        "parameters": {},
        "id": "lm-ewt-001",
        "name": "When called by Digest",
        "type": "n8n-nodes-base.executeWorkflowTrigger",
        "typeVersion": 1.1,
        "position": [0, 420],
    }
    export_node = {
        "parameters": {"mode": "runOnceForAllItems", "jsCode": EXPORT_JS},
        "id": "lm-exp-001",
        "name": "Export history",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [260, 420],
    }

    # Remove old archive/export if re-running
    names_drop = {"Archive run summary", "When called by Digest", "Export history"}
    pipe["nodes"] = [n for n in pipe["nodes"] if n.get("name") not in names_drop]
    pipe["nodes"].extend([archive_node, export_trigger, export_node])

    conns = pipe["connections"]
    # HMAC OK true -> Archive -> Status route
    conns["HMAC OK?"] = {
        "main": [
            [{"node": "Archive run summary", "type": "main", "index": 0}],
            [{"node": "Reject 401", "type": "main", "index": 0}],
        ]
    }
    conns["Archive run summary"] = {
        "main": [[{"node": "Status route", "type": "main", "index": 0}]]
    }
    conns["When called by Digest"] = {
        "main": [[{"node": "Export history", "type": "main", "index": 0}]]
    }
    pipe["meta"]["templateNote"] = (
        "HMAC fail-closed with exact raw body + archive summaries to static data + "
        "Failure Alert + ExecuteWorkflow export for Daily Digest. "
        "NEVER use JSON.stringify(parsedBody) as exact raw body."
    )
    pipe["versionId"] = "lm-pipe-v3-daily-digest-archive"
    pipe["active"] = False
    PIPE.write_text(json.dumps(pipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    digest = {
        "name": "Laptop Monitor — Daily Digest",
        "id": "LmDailyDigest01",
        "active": False,
        "isArchived": False,
        "versionId": "lm-digest-v1",
        "settings": {
            "executionOrder": "v1",
            "timezone": "Europe/Moscow",
        },
        "meta": {
            "templateNote": (
                "Daily Digest v1 at 09:00 Europe/Moscow. Reads pipelineHistory via "
                "Execute Workflow from Pipeline Events. Telegram via LAPTOP_MONITOR_TELEGRAM_* env. "
                "v1 uses pipeline.completed summaries only (no detailed price changes / historical lows)."
            )
        },
        "nodes": [
            {
                "parameters": {
                    "rule": {
                        "interval": [
                            {
                                "field": "cronExpression",
                                "expression": "0 9 * * *",
                            }
                        ]
                    }
                },
                "id": "lm-dd-sched",
                "name": "Schedule 09:00 MSK",
                "type": "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.2,
                "position": [0, 0],
            },
            {
                "parameters": {},
                "id": "lm-dd-manual",
                "name": "Manual test",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 220],
            },
            {
                "parameters": {
                    "includeOtherFields": True,
                    "assignments": {
                        "assignments": [
                            {
                                "id": "m1",
                                "name": "test_mode",
                                "value": False,
                                "type": "boolean",
                            }
                        ]
                    },
                    "options": {},
                },
                "id": "lm-dd-prod",
                "name": "Mode production",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [240, 0],
            },
            {
                "parameters": {
                    "includeOtherFields": True,
                    "assignments": {
                        "assignments": [
                            {
                                "id": "m2",
                                "name": "test_mode",
                                "value": True,
                                "type": "boolean",
                            }
                        ]
                    },
                    "options": {},
                },
                "id": "lm-dd-test",
                "name": "Mode test",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [240, 220],
            },
            {
                "parameters": {
                    "mode": "runOnceForAllItems",
                    "jsCode": (
                        "const staticData = $getWorkflowStaticData('global');\n"
                        "staticData._digestTestMode = $json.test_mode === true;\n"
                        "return [$input.first()];\n"
                    ),
                },
                "id": "lm-dd-remember",
                "name": "Remember mode",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [420, 100],
            },
            {
                "parameters": {
                    "source": "database",
                    "workflowId": {
                        "__rl": True,
                        "value": "LmPipeEvntRecv01",
                        "mode": "id",
                        "cachedResultName": "Laptop Monitor — Pipeline Events",
                    },
                    "mode": "once",
                    "options": {},
                },
                "id": "lm-dd-exec",
                "name": "Load run history",
                "type": "n8n-nodes-base.executeWorkflow",
                "typeVersion": 1.2,
                "position": [640, 100],
            },
            {
                "parameters": {
                    "mode": "runOnceForAllItems",
                    "jsCode": DIGEST_JS,
                },
                "id": "lm-dd-send",
                "name": "Build and send digest",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [880, 100],
            },
        ],
        "connections": {
            "Schedule 09:00 MSK": {
                "main": [[{"node": "Mode production", "type": "main", "index": 0}]]
            },
            "Manual test": {
                "main": [[{"node": "Mode test", "type": "main", "index": 0}]]
            },
            "Mode production": {
                "main": [[{"node": "Remember mode", "type": "main", "index": 0}]]
            },
            "Mode test": {
                "main": [[{"node": "Remember mode", "type": "main", "index": 0}]]
            },
            "Remember mode": {
                "main": [[{"node": "Load run history", "type": "main", "index": 0}]]
            },
            "Load run history": {
                "main": [[{"node": "Build and send digest", "type": "main", "index": 0}]]
            },
        },
    }
    DIGEST.write_text(json.dumps(digest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", PIPE)
    print("wrote", DIGEST)


if __name__ == "__main__":
    main()
