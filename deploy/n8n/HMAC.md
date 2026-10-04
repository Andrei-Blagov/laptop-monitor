# n8n HMAC verification (cutover setup)

Laptop-monitor signs webhooks as:

```
HMAC_SHA256(secret, timestamp + "." + raw_body_bytes)
```

Headers:

| Header | Meaning |
|--------|---------|
| `X-Laptop-Monitor-Timestamp` | Unix epoch seconds (string) |
| `X-Laptop-Monitor-Signature` | hex digest |

`raw_body_bytes` = the **exact** HTTP request body as received (UTF-8 bytes),
byte-for-byte identical to what `integrations/n8n.py` posted.

## Forbidden

**Do not** reconstruct the body for HMAC from a parsed JSON object.

In particular, **never** use:

```javascript
JSON.stringify(parsedBody)   // FORBIDDEN — not equivalent to raw_body
```

Key order, spacing, and Unicode escaping differ from the original payload.
A reconstructed body will produce a wrong signature or a false accept.

If exact raw body bytes are **not** available in the verification node →
**FAIL CLOSED** (reject). Do not fall back to any re-serialization.

## Fail-closed rules

Reject (do not route SUCCESS / PARTIAL / FAILED) when any of:

| Condition | Action |
|-----------|--------|
| missing timestamp | reject |
| missing signature | reject |
| missing secret | reject |
| missing **exact raw body** | reject |
| timestamp skew > 300 seconds | reject |
| signature mismatch | reject |

Only after successful verification: branch on `status`
(`success` | `partial` | `failed`).

## Production recipe (verified on n8n 2.42.1)

| Item | Value |
|------|--------|
| Image (pinned) | `docker.n8n.io/n8nio/n8n@sha256:e7634e62f766044dc770460db8defdcd84034eb6d767dd6a1101d49fe98f814f` → app **2.42.1** |
| Exact raw body | Webhook node **Options → Raw Body = true** |
| Binary property | `data` (`item.binary.data`) |
| Read bytes in Code | `await this.helpers.getBinaryDataBuffer(0, 'data')` |
| Crypto | `require('crypto')` in Code node |
| Required n8n env | `NODE_FUNCTION_ALLOW_BUILTIN=crypto` |
| Secret env | `LAPTOP_MONITOR_WEBHOOK_SECRET` (also set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`) |
| Production path | `/webhook/laptop-monitor-pipeline` |

**Pinning:** do **not** use the moving tag `docker.n8n.io/n8nio/n8n:beta`.
That tag drifted production from **2.40.2 → 2.42.1** on recreate.
Keep the immutable digest above so `docker compose up -d` / recreate
cannot pull a newer n8n unexpectedly.

Do **not** read `$binary.data.data` base64 manually when filesystem/binary-mode
storage is enabled — prefer `getBinaryDataBuffer`.

If `require('crypto')` is blocked and cannot be enabled, use the built-in
**Crypto** node HMAC-SHA256 over `timestamp + "." + exact_raw_utf8` — still
**fail closed** when raw body/binary is missing. Prefer Code + `crypto` when
allowed (single buffer HMAC matches the sender byte-for-byte).

## Pseudo-code (n8n Code node) — raw body only

```javascript
const crypto = require('crypto'); // needs NODE_FUNCTION_ALLOW_BUILTIN=crypto
const secret = $env.LAPTOP_MONITOR_WEBHOOK_SECRET;
const headers = $input.first().json.headers || {};
const ts = headers['x-laptop-monitor-timestamp']
        || headers['X-Laptop-Monitor-Timestamp'];
const sig = headers['x-laptop-monitor-signature']
        || headers['X-Laptop-Monitor-Signature'];

if (!ts || !sig || !secret) {
  throw new Error('Missing HMAC headers or secret');
}
if (!$input.first().binary?.data) {
  throw new Error('Missing exact raw body — fail closed');
}
const bodyBytes = await this.helpers.getBinaryDataBuffer(0, 'data');
if (!bodyBytes || bodyBytes.length === 0) {
  throw new Error('Missing exact raw body — fail closed');
}
const skew = Math.abs(Math.floor(Date.now() / 1000) - Number(ts));
if (Number.isNaN(Number(ts)) || skew > 300) {
  throw new Error('Timestamp skew too large');
}
const expected = crypto
  .createHmac('sha256', secret)
  .update(Buffer.concat([
    Buffer.from(String(ts), 'utf8'),
    Buffer.from('.', 'utf8'),
    bodyBytes,
  ]))
  .digest('hex');
if (expected !== sig) {
  throw new Error('Invalid signature');
}
return $input.all();
```

Notes:

- Store the secret in n8n env/credentials — never hardcode in workflow JSON.
- Sender (laptop-monitor) remains best-effort: timeout / 4xx / 5xx / DNS
  **do not** change pipeline status; no retry storm.
- Do **not** mount Docker socket into n8n. Do **not** give n8n shell on the VPS.

## Verify on production n8n before enabling webhook

Complete this on the **real** n8n instance during cutover:

1. Record **n8n version** (`Settings` / about / image tag).
2. Confirm how **this** version’s Webhook node exposes the request:
   - exact raw body bytes / binary / string property name;
   - whether “raw body” / “binary data” options must be enabled.
3. Confirm the verification node can read those **exact** bytes
   (Code node input path).
4. Confirm crypto availability:
   - is `require('crypto')` allowed in Code nodes?
   - if not, use this version’s built-in **Crypto** node (or other
     supported HMAC-SHA256) over `timestamp + "." + raw_body` —
     still **fail closed** if raw body is missing.
5. Send a test POST with a **valid** signature → must accept.
6. Send a POST with a **deliberately bad** signature → must reject.
7. Send POSTs missing timestamp / signature / raw body → must reject.
8. Only then set `N8N_WEBHOOK_URL` + `N8N_WEBHOOK_SECRET` in
   laptop-monitor `.env`. Until then keep `N8N_WEBHOOK_URL=` empty.

Template `pipeline-event-receiver.json` implements this recipe for n8n 2.42.x
(also verified earlier on 2.40.2).
**NEVER use JSON.stringify(parsedBody)** as the HMAC input.
