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

## Pseudo-code (n8n Code node) — raw body only

Wire the Webhook node so the Code node receives **exact raw body**
(binary/string of the request). Field names vary by n8n version — see checklist below.

```javascript
const crypto = require('crypto'); // only if this n8n version allows it
const secret = $env.LAPTOP_MONITOR_WEBHOOK_SECRET;
const headers = $input.item.json.headers || {};
const ts = headers['x-laptop-monitor-timestamp']
        || headers['X-Laptop-Monitor-Timestamp'];
const sig = headers['x-laptop-monitor-signature']
         || headers['X-Laptop-Monitor-Signature'];

// MUST be exact request bytes/string from the Webhook node.
// Adjust the path after checking your n8n version (see checklist).
const rawBody = $input.item.json.rawBody; // example — verify on your n8n

if (!ts || !sig || !secret) {
  throw new Error('Missing HMAC headers or secret');
}
if (rawBody === undefined || rawBody === null || rawBody === '') {
  throw new Error('Missing exact raw body — fail closed');
}
const skew = Math.abs(Math.floor(Date.now() / 1000) - Number(ts));
if (Number.isNaN(Number(ts)) || skew > 300) {
  throw new Error('Timestamp skew too large');
}
const bodyBytes = Buffer.isBuffer(rawBody)
  ? rawBody
  : Buffer.from(String(rawBody), 'utf8');
const expected = crypto
  .createHmac('sha256', secret)
  .update(Buffer.concat([Buffer.from(String(ts), 'utf8'), Buffer.from('.', 'utf8'), bodyBytes]))
  .digest('hex');
if (expected !== sig) {
  throw new Error('Invalid signature');
}
return $input.all();
```

Notes:

- Store the secret in n8n credentials/env — never hardcode in workflow JSON.
- Sender (laptop-monitor) remains best-effort: timeout / 4xx / 5xx / DNS
  **do not** change pipeline status; no retry storm.
- Do **not** mount Docker socket into n8n. Do **not** give n8n shell on the VPS.

## Verify on production n8n before enabling webhook

Complete this on the **real** n8n instance during cutover (do not change n8n now):

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

Template `pipeline-event-receiver.json` is a skeleton: replace the
Verify HMAC stub with the version-correct fail-closed implementation above
**before** enabling the production webhook URL.
