# n8n OPS layer for laptop-monitor

Laptop Monitor keeps **systemd timer** as the primary scheduler and
**Telegram control bot** for manual runs.

n8n is an optional automation / analytics layer:

- receive `pipeline.completed` webhooks
- branch SUCCESS / PARTIAL / FAILED
- daily digest (skeleton)
- failure alert workflows

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

On the n8n side (not in Git): set `LAPTOP_MONITOR_WEBHOOK_SECRET` to the same
value, plus `NODE_FUNCTION_ALLOW_BUILTIN=crypto` and
`N8N_BLOCK_ENV_ACCESS_IN_NODE=false` so the Code verifier can HMAC exact
raw body bytes (`getBinaryDataBuffer`). See [`HMAC.md`](HMAC.md).

Webhook failures (timeout / 5xx / DNS) are logged and **do not** change
pipeline SUCCESS/PARTIAL/FAILED.

## Templates

JSON skeletons in this directory are for import into a **separate** n8n
instance during cutover. Do not import into production n8n until the
controlled cutover stage.

1. `pipeline-event-receiver.json` — webhook + HMAC check + status switch
2. `daily-digest.json` — skeleton
3. `failure-alert.json` — operational notification skeleton

**HMAC setup:** see [`HMAC.md`](HMAC.md) for the exact verification recipe
to wire into the receiver before import.

## Payload sketch

See `integrations/n8n.py` → `build_pipeline_completed_payload`.
