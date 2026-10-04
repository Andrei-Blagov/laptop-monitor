# Changelog

## 0.3.0 — 2026-10-04

Post-v0.2.0 production features packaged as a stable release.

### User-visible

- **Deal Ranking / Scoring v2** — explainable score `0..100` with confidence; components: GPU, price/value, CPU class, RAM, SSD, screen, historical opportunity, cross-store saving; cluster-wide specs resolution
- **Telegram TOP** — shows score / confidence / short reasons; uses ranking v2
- **Price History v1** — per-model history card from Telegram control bot
- **Price History v2** — PNG charts for 30 / 90 / all-time periods (`sendPhoto`, in-memory matplotlib)
- History picker order follows the same ranking as TOP

### Operations

- Independent **n8n host watchdog** (systemd)
- **Failure Alert** for PARTIAL / FAILED pipeline events
- **Daily Digest v1** from `pipeline.completed` summaries
- Production **n8n pinned** to immutable image `2.42.1` (`sha256:e7634e62…`)
- Telegram / httpx logging does not expose bot token URLs

### Unchanged from v0.2.0

- Global price cap **≤ 300 000 ₽** for TOP / alerts / cross-store tracking
- Four enabled stores: Regard, ANDPRO, KNS, Citilink
- Systemd primary scheduler; n8n as OPS layer only

## 0.2.0 — 2026-09-30

Initial multi-store production cutover (Regard / ANDPRO / KNS / Citilink), systemd deploy, Telegram control bot, price cap, baseline ranking.
