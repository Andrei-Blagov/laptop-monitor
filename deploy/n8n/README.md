# n8n OPS layer for laptop-monitor

Laptop Monitor keeps **systemd timer** as the primary scheduler and
**Telegram control bot** for manual runs.

n8n is an optional automation / analytics layer:

| Component | Status |
|-----------|--------|
| `pipeline.completed` receiver (HMAC) | **active** |
| Failure alert (PARTIAL / FAILED) | **active** (inside receiver workflow) |
| Host n8n watchdog (systemd) | **active** |
| Daily digest | **disabled** (post-v0.2.0) |

## Security

- Do **not** mount `/var/run/docker.sock` into n8n
- Do **not** give n8n shell access to the VPS
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
  for Failure Alert (direct Telegram; not stored in workflow JSON)

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

## Templates

1. `pipeline-event-receiver.json` — webhook + HMAC + status route + failure alert
2. `failure-alert.json` — pointer/docs stub (logic lives in the receiver)
3. `daily-digest.json` — skeleton (still disabled)

## Payload sketch

See `integrations/n8n.py` → `build_pipeline_completed_payload`
(includes optional `error_summary` and per-store `error`).
