# Laptop Monitor — VPS deployment

Production target: **Ubuntu VPS** via Docker Compose + systemd timers.

Windows Task Scheduler is **not** used for production.
Do **not** enable these units until the first manual VPS run succeeds.

Isolate from other VPS workloads (n8n, Caddy, PostgreSQL, other compose projects).
Prefer a dedicated directory, e.g. `/opt/laptop-monitor`. No public HTTP port required
(Telegram control uses long polling).

## Layout

```
deploy/
  Dockerfile
  docker-compose.yml
  systemd/
    laptop-monitor.service
    laptop-monitor.timer          # every 2 hours
    laptop-monitor-control.service
    laptop-monitor-backup.service
    laptop-monitor-backup.timer   # daily
  README.md
```

## Image contents

Runtime image includes application Python modules, `parsers/`, `stores/`, `VERSION`,
and `scripts/backup_db.py`.

Excluded via `.dockerignore`: `.git`, tests, local DBs, logs, screenshots, `.venv`,
IDE files, Windows scripts (`scripts/windows/`), caches, backups.

Labels: `org.opencontainers.image.version=0.2.0`.

## Volumes

| Path | Purpose |
|------|---------|
| `data/` | SQLite DB + `data/backups/` |
| `logs/` | pipeline / control-bot logs |
| `.env` | secrets on host only (never baked into image) |

## Services

| Service | Role |
|---------|------|
| `pipeline` | oneshot `python run_pipeline.py` (manual profile / systemd) |
| `control-bot` | long-polling Telegram admin bot (`restart: unless-stopped`) |
| `backup` | `python scripts/backup_db.py` |

```bash
cd /opt/laptop-monitor
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d control-bot
docker compose -f deploy/docker-compose.yml run --rm pipeline python run_pipeline.py
docker compose -f deploy/docker-compose.yml run --rm backup
```

## Systemd (prepare only — do not enable yet)

Copy units from `deploy/systemd/` to `/etc/systemd/system/`, adjust `WorkingDirectory`
if needed, then **after** successful first manual run:

```bash
# AFTER successful manual run only:
# sudo systemctl daemon-reload
# sudo systemctl enable --now laptop-monitor.timer
# sudo systemctl enable --now laptop-monitor-control.service
# sudo systemctl enable --now laptop-monitor-backup.timer
```

Timer: `OnCalendar` every 2 hours, `Persistent=true`. Pipeline process lock is a second
guard against parallel runs.

## Logging

Write under `logs/` with timestamp, `app_version`, `instance_id`, run id, stage,
store status, errors, duration. Never log bot token / `.env`. Retain ~30 days
(host logrotate or manual prune).

## SQLite backup

`scripts/backup_db.py` uses the SQLite backup API (not a live file copy), runs
`PRAGMA integrity_check`, stores files in `data/backups/`, prunes after 30 days.

## Migration: Windows → VPS

Preserve the existing production SQLite (history, products, identifiers, alerts,
deliveries, pipeline_runs). Do **not** start from an empty DB.

1. On Windows: `python scripts/backup_db.py` → note path  
2. Compute SHA256 of the backup  
3. Transfer backup to VPS (scp/sftp) into staging  
4. Verify SHA256 on VPS  
5. Place as `/opt/laptop-monitor/data/laptop_monitor.db` (or configured path)  
6. Run another backup on VPS before first write  
7. Start container once so `init_db` applies schema migrations (`store_runs`,
   `app_version`, `instance_id`, …)  
8. `PRAGMA integrity_check`  
9. Read-only: `docker compose … run --rm pipeline python run_pipeline.py --status`  
10. First **manual** pipeline run  
11. Verify Telegram price + ops messages; `control_bot` status/TOP  
12. Enable systemd timer + control-bot + backup timer  
13. Stop any Windows production monitoring (`LAPTOP_MONITOR_INSTANCE` must be unique;
    only `vps-prod` sends production alerts)  
14. Tag `v0.2.0` after successful cutover (not part of this prep commit)

## Env on VPS

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_ADMIN_CHAT_ID=...
LAPTOP_MONITOR_INSTANCE=vps-prod
```

## Single production instance

| Instance | Role |
|----------|------|
| `vps-prod` | production |
| `local-dev` / Windows | development only |

Running two independent SQLite copies against the same Telegram chat creates duplicate alerts.
