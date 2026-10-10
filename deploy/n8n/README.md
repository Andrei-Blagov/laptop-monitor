# n8n OPS layer for laptop-monitor

Laptop Monitor keeps **systemd timer** as the primary scheduler and
**Telegram control bot** for manual runs.

n8n is an optional automation / analytics layer:

| Component | Status |
|-----------|--------|
| `pipeline.completed` receiver (HMAC) | **active** |
| Failure alert (PARTIAL / FAILED) | **active** (inside receiver workflow) |
| Host n8n watchdog (systemd) | **active** |
| Daily Digest v1 (09:00 Europe/Moscow) | **active** |

## Production n8n image pin

| Item | Value |
|------|--------|
| App version | **2.42.1** |
| Image reference | `docker.n8n.io/n8nio/n8n@sha256:e7634e62f766044dc770460db8defdcd84034eb6d767dd6a1101d49fe98f814f` |

Reason: the moving tag `:beta` caused automatic version drift
**2.40.2 → 2.42.1** during container recreate. Production compose must keep
this immutable digest (VPS `/opt/n8n/docker-compose.yml`, not in this repo).

## Security

- Do **not** mount `/var/run/docker.sock` into n8n
- Do **not** give n8n shell / SQLite access to laptop-monitor
- Verify webhook HMAC:

Headers:

- `X-Laptop-Monitor-Timestamp`
- `X-Laptop-Monitor-Signature`

Signature = `HMAC_SHA256(secret, timestamp + "." + raw_body)` hex digest.

Env on laptop-monitor:

```
N8N_WEBHOOK_URL=https://n8n.example/webhook/laptop-monitor-pipeline
N8N_WEBHOOK_SECRET=...
N8N_WEBHOOK_TIMEOUT_SECONDS=8
```

Empty `N8N_WEBHOOK_URL` disables the integration.

On the n8n side (not in Git):

- `LAPTOP_MONITOR_WEBHOOK_SECRET` (same secret)
- `NODE_FUNCTION_ALLOW_BUILTIN=crypto`
- `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`
- `LAPTOP_MONITOR_TELEGRAM_BOT_TOKEN` / `LAPTOP_MONITOR_TELEGRAM_CHAT_ID`
  for Failure Alert + Daily Digest (not stored in workflow JSON)

See [`HMAC.md`](HMAC.md).

Webhook failures (timeout / 4xx / 5xx / DNS) are logged and **do not** change
pipeline SUCCESS/PARTIAL/FAILED.

## Failure Alert

Integrated into **Laptop Monitor — Pipeline Events** after HMAC verification.
No separate public webhook.

- Triggers only when `status` is `partial` or `failed`
- SUCCESS → no failure notification
- Dedupe key: `run_id` (workflow static data; one Telegram message per run)
- Message text: see `integrations.n8n.format_failure_alert_message`

## Daily Digest v1

Workflow: **Laptop Monitor — Daily Digest** (`LmDailyDigest01`)

- Schedule: `0 9 * * *` with workflow timezone **Europe/Moscow**
- Source: rolling `pipelineHistory` archived in Pipeline Events static data
  after HMAC (no SQLite access)
- Retention: 48h or max 100 summaries
- Window: last 24h, `instance_id=vps-prod` only
- TOP: from latest success/partial in window, max 5, fail-safe `<= 330000`
- Alerts section: `alerts_created` count only (no detailed drops / historical lows)
- Dedupe: one send per MSK calendar date (`lastDigestSentDate`); Telegram failure
  does **not** mark the date sent
- Manual test path sends `🧪 ... TEST` and does not write production dedupe key
- Future import: open workflow `LmDailyDigest01` and import the file there.
  A second workflow would keep the old execution history and `lastDigestSentDate`
  on the previous id, so the new copy could send again the same day.
  The file has the schedule (`0 9 * * *`, `Europe/Moscow`), no `staticData`,
  and no credential ids (Telegram stays in `LAPTOP_MONITOR_TELEGRAM_*`).

Logic reference: `integrations.n8n.format_daily_digest_message` (and helpers).

**v1 note:** uses `pipeline.completed` summaries only. Detailed price changes /
historical lows are **not** included (planned for Daily Digest v2).

## Templates

1. `pipeline-event-receiver.json` — webhook + HMAC + archive + status route + failure alert + history export
2. `failure-alert.json` — pointer/docs stub (logic lives in the receiver)
3. `daily-digest.json` — schedule + Execute Workflow + Telegram digest

## Payload sketch

See `integrations/n8n.py` → `build_pipeline_completed_payload`
(includes optional `error_summary` and per-store `error`).
