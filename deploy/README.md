# Laptop Monitor — VPS deployment

Production: **Ubuntu VPS** via clean release bundle + Docker Compose + systemd timers.

- Windows Task Scheduler is **not** used.
- VPS does **not** use `git clone` of the full repo.
- Do **not** enable systemd until the first manual pipeline succeeds.

Isolate from other VPS workloads (n8n, Caddy, PostgreSQL). Dedicated directory:
`/opt/laptop-monitor`. No public HTTP port (Telegram long polling).

## Version

Single source of truth: project root `VERSION`.

Always use the wrapper:

```bash
./deploy/compose.sh build
./deploy/compose.sh run --rm pipeline python run_pipeline.py --version
./deploy/compose.sh up -d control-bot
./deploy/compose.sh run --rm backup
```

`compose.sh` exports `LAPTOP_MONITOR_VERSION` from `VERSION` and passes
`APP_VERSION` into the Dockerfile build arg / image tag.

Never hardcode `0.x.y` in Dockerfile / compose / systemd.

## Stores (must match `stores/registry.py`)

| Store | Status | Mode | Reliability | Notes |
|-------|--------|------|-------------|-------|
| Regard | **enabled** | http | stable | HTML/API catalog |
| ANDPRO | **enabled** | http | stable | HTML catalog |
| KNS | **enabled** | http | stable | HTML + goodsList; public ≠ club price |
| Citilink | **enabled** | browser | experimental | Playwright Chromium; may fail alone |
| DNS | disabled | — | — | HTTP 401 / API 403; no stable public catalog |
| XCOM | disabled | — | — | DDoS-Guard / captcha |
| Technopark | disabled | — | — | HTTP 401/403 |

**Citilink** requires Playwright Chromium (installed in the Docker image). Collection is
fail-safe: challenge / zero products / browser crash → **store FAILED** only for Citilink;
Regard / ANDPRO / KNS continue. Stale/failed Citilink is excluded from TOP / cross-store.

Region for all adapters: `MONITOR_REGION=moscow`.

## Layout on VPS

```
/opt/laptop-monitor/
  VERSION
  *.py
  parsers/
  stores/
  integrations/    # n8n webhook client
  scripts/          # backup_db, migrate_db only (+ __init__)
  deploy/           # Dockerfile, compose.yml, compose.sh, systemd/, n8n/
  .env              # host secrets only
  data/             # SQLite + backups/
  logs/
```

No `.git`, no `tests/`, no `scripts/windows/`.

## Build release bundle (dev/CI machine)

```bash
python -m scripts.build_deploy_bundle
# dist/laptop-monitor-<VERSION>.tar.gz
# dist/laptop-monitor-<VERSION>.tar.gz.sha256
```

Allowlist-only. Excludes: `.git`, tests, Windows scripts, local DB, logs, `.env`, caches.

## Volumes / services

| Path / service | Purpose |
|----------------|---------|
| `data/` | SQLite + `data/backups/` |
| `logs/` | application logs |
| `.env` | secrets on host |
| `pipeline` | oneshot via systemd / manual |
| `control-bot` | long-polling admin bot |
| `backup` | `python -m scripts.backup_db` |

Pipeline and control-bot use `shm_size: 256mb` for Chromium (Citilink). No published ports.
Control-bot does **not** keep Chromium open; browser starts only during pipeline / manual Run.

## Explicit DB migration

```bash
# host python or container:
python -m scripts.migrate_db --db data/laptop_monitor.db
# or
./deploy/compose.sh run --rm --no-deps pipeline python -m scripts.migrate_db --db data/laptop_monitor.db
```

Schema only + integrity_check. Idempotent. No collection / alerts / Telegram.

Control bot refuses to start if schema is missing (`Run database migration first`).

## Fresh TOP

TOP uses only stores whose **latest** `store_runs` row is `ok` and within
`STORE_FRESHNESS_MAX_MINUTES` (default 180). Failed latest attempt => stale for TOP.

## Systemd (prepare only)

Units call `/opt/laptop-monitor/deploy/compose.sh ...`.

**Do not enable** `laptop-monitor.service` directly — the timer invokes it.

After a successful **manual** production pipeline run:

```bash
sudo systemctl enable --now laptop-monitor-control.service
sudo systemctl enable --now laptop-monitor.timer
sudo systemctl enable --now laptop-monitor-backup.timer
```

Semantics:

| Unit | Behavior |
|------|----------|
| `laptop-monitor-control.service` | `Type=oneshot` + `RemainAfterExit=yes`: starts detached `control-bot`, then systemd shows **active (exited)**. Docker `restart: unless-stopped` recovers container crashes. `systemctl stop` runs `compose stop control-bot`. |
| `laptop-monitor.timer` | Schedules pipeline every 2h (`Persistent=true`). **`enable --now` does not start an immediate pipeline** — next run is the next `OnCalendar` elapse. No `Requires=` on the oneshot service. |
| `laptop-monitor-backup.timer` | Daily 03:15; same pattern (no immediate backup on enable). |

Verify after enable:

```bash
systemctl list-timers laptop-monitor.timer laptop-monitor-backup.timer
systemctl status laptop-monitor-control.service
docker ps --filter name=laptop-monitor-control
```

## n8n (optional OPS layer)

Primary scheduler remains **systemd**. n8n receives `pipeline.completed` webhooks.

**Before** pointing laptop-monitor at n8n:

1. Record the production **n8n version**.
2. Confirm that version’s Webhook node can expose **exact raw body bytes**.
3. Verify HMAC on a **test** webhook (see [`n8n/HMAC.md`](n8n/HMAC.md)).
4. Send a request with a **deliberately bad signature** → must be **rejected**.
5. Confirm reject on missing timestamp / signature / raw body / skew > 300s.
6. Only then set `N8N_WEBHOOK_URL` (and secret) in laptop-monitor `.env`.

First VPS pipeline run may keep:

```
N8N_WEBHOOK_URL=
```

Enable n8n only after the receiver passes the checklist. Sender failures
(timeout / 4xx / 5xx / DNS) never change pipeline status.

Do **not** mount `/var/run/docker.sock` into n8n.

## Cutover checklist

### Windows

1. `python -m scripts.backup_db` on production DB
2. SHA256 of DB backup
3. `python -m scripts.build_deploy_bundle`
4. SHA256 of bundle

### VPS

5. Create `/opt/laptop-monitor`
6. Upload bundle
7. Verify SHA256
8. Extract clean tree
9. Create `.env` (`LAPTOP_MONITOR_INSTANCE=vps-prod`, Telegram admin allowlist;
   leave `N8N_WEBHOOK_URL=` empty until step 20a)
10. Upload DB into `data/`
11. Verify DB SHA256
12. Backup imported DB on VPS
13. `./deploy/compose.sh build`
14. `python -m scripts.migrate_db --db data/laptop_monitor.db`
15. integrity_check OK
16. Read-only: `./deploy/compose.sh run --rm pipeline python run_pipeline.py --status`
17. Start control bot: `./deploy/compose.sh up -d control-bot`
18. Check Version / Status / TOP in Telegram
19. Manual pipeline run (Regard / ANDPRO / KNS / Citilink fail-safe)
20. Verify alerts / Telegram
20a. **n8n gate (before enabling webhook):**
    - note n8n version;
    - confirm exact raw body + HMAC (see [`n8n/HMAC.md`](n8n/HMAC.md));
    - test webhook with valid signature → accept;
    - test deliberately bad signature → reject;
    - only then set `N8N_WEBHOOK_URL` + `N8N_WEBHOOK_SECRET` and re-run or wait for next pipeline
21. Enable systemd (order):
    `enable --now laptop-monitor-control.service`,
    then `laptop-monitor.timer`,
    then `laptop-monitor-backup.timer`
    (`enable --now` on the pipeline timer must **not** fire an immediate run)
22. Confirm timers / control status / `docker ps --filter name=laptop-monitor-control`
23. Confirm next scheduled pipeline elapse
24. After confirmation: create git tag `v0.2.0` (not before)

## DNS / disabled stores

- **DNS:** disabled — catalog HTTP 401, API 403, plain Playwright not usable.
  Future: official feed / partner API only (no anti-bot bypass).
- **XCOM / Technopark:** disabled — captcha / 401–403.
- Not blockers for v0.2.0 cutover.

## Single production instance

| Instance | Role |
|----------|------|
| `vps-prod` | production |
| `local-dev` / Windows | development only |
