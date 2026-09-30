# n8n HMAC verification (cutover setup)

When importing `pipeline-event-receiver.json`, add a Code/Function node
**immediately after** the Webhook node that verifies authenticity.

## Headers sent by laptop-monitor

| Header | Meaning |
|--------|---------|
| `X-Laptop-Monitor-Timestamp` | Unix epoch seconds (string) |
| `X-Laptop-Monitor-Signature` | hex HMAC-SHA256 |

## Canonical string

```
timestamp + "." + raw_body
```

Where `raw_body` is the exact webhook request body bytes (UTF-8 JSON as sent).

## Pseudo-code (n8n Code node)

```javascript
const crypto = require('crypto');
const secret = $env.LAPTOP_MONITOR_WEBHOOK_SECRET; // set in n8n
const headers = $input.item.json.headers || {};
const ts = headers['x-laptop-monitor-timestamp']
        || headers['X-Laptop-Monitor-Timestamp'];
const sig = headers['x-laptop-monitor-signature']
         || headers['X-Laptop-Monitor-Signature'];
const rawBody = $input.item.json.rawBody
             || JSON.stringify($input.item.json.body);

if (!ts || !sig || !secret) {
  throw new Error('Missing HMAC headers or secret');
}
const skew = Math.abs(Math.floor(Date.now() / 1000) - Number(ts));
if (skew > 300) {
  throw new Error('Timestamp skew too large');
}
const expected = crypto
  .createHmac('sha256', secret)
  .update(`${ts}.${rawBody}`)
  .digest('hex');
if (expected !== sig) {
  throw new Error('Invalid signature');
}
return $input.all();
```

Notes:

- Configure the Webhook node to expose **raw body** if your n8n version requires it for exact HMAC.
- Store the secret in n8n credentials/env — never hardcode in the workflow JSON.
- Reject failed verification (do not branch to SUCCESS).
- After verification, IF/Switch on `status`: `success` | `partial` | `failed`.

Do **not** mount Docker socket into n8n. Do **not** give n8n shell on the VPS.
