# Operational notifications: local candidate, not deployed

Policy approved 23 October 2026: appearance/change of a material problem and
recovery are notified once. No periodic reminder for an unchanged problem.
Daily offers at 08:00 Moscow and useful price alerts are independent and retained.

## One owner and bounded state

The application owns operational Telegram messages. CLI completion always calls
the existing `admin_notify.maybe_notify_ops_failure` entry point, including success
to detect recovery. It delegates to `ops_notifications.notify_transition`.

The incident signature includes pipeline severity/stage, failed source set/status
and structural error category. It excludes run IDs, duration, product counts and
dynamic exception details. Same incident after process restart stays silent.
Disabled/skipped stores are not incidents. An initially healthy installation is
silent; an existing incident at first activation produces one notification.

`data/ops_notifications-<instance hash>.json` stores only hashes, timestamp,
current incident state and one delivery state per recipient. It replaces its
previous state instead of appending logs/history. The existing cross-process
pipeline lock primitive and atomic fsynced replacement protect each claim.
The existing compose binds `./data:/app/data`; that mount must remain persistent.

Each recipient is claimed before I/O. Only API `ok` **and message_id** count as
confirmed. Timeout/crash/negative response remain `unconfirmed`; same state is
not automatically resent. This deliberately favors no repeated technical noise
over guaranteed delivery of an operational notice. Logs record the failure class,
not chat IDs/tokens/raw response. `/status` still exposes the actual source/run
failure; adding delivery-state visibility is a separately reviewed UI correction.
This is not an exactly-once delivery guarantee and does not change daily outbox
or price delivery retry/ACK handling.

Every signed pipeline payload declares `ops_notifications_owner=application`.
The receiver's HMAC projection retains the field; its Failure alert node then
skips only its second Telegram send. HMAC validation, archiving, status routing,
history export and legacy payload handling remain. The current deployed receiver
has NOT been read or changed in this diagnostic block. Repository templates and
README assertions do not prove its active state or recipients.

## Required application sequence (authorized, not yet performed)

1. Read the existing Laptop Monitor receiver `LmPipeEvntRecv01` and its actual
   connections/settings through existing authorized access. Confirm ownership and
   compare with this template; don't import a second workflow or overwrite unrelated
   customizations. Separately check whether the legacy n8n 09:00 technical digest is
   active: it is not the mandatory VPS 08:00 market daily. Do not disable it blindly.
2. Preserve the currently installed image/code pin, consistent history/queue/ACK
   backup and the current receiver JSON/static state on the server. Don't export
   secrets or live data to GitHub/Windows. Verify a supported recovery path first.
3. Run full Linux offline checks and bundle/import-graph checks, including the new
   transition and JS ownership tests. No store/Telegram network calls; Node is
   available on the current Ubuntu CI runner. Windows fixture results don't prove
   Linux concurrency, live n8n compatibility or Docker restore.
4. Publish/merge the bounded app change only after those checks. The exact product
   package was approved on 23 October; this local checkpoint precedes publication.
   Apply just the two receiver code edits **first** on the same existing workflow:
   copy the signed field after verification and skip the second sender for owned
   events. Verify HMAC rejection still fails closed. Old unmarked app payloads
   retain their old behavior during this short compatibility window.
5. Deploy the app through the supported versioned path with persistent data mount.
   Do not replace daily queues/ACK, history, source configuration or recipients.
   No extra real digest or store scan is required to activate this change.
6. Verify installed hashes/version, timers and database/history preservation.
   Observe the next normal VPS collection for one incident transition and then
   silent repeated state. A real recovery cannot be claimed until it occurs.

Rollback restores the prior app image/code and receiver together from the saved
pair, retaining all data/history/receipts and the new ops state file. Old technical
noise may return on rollback; it must not be reported as fixed. No new GitHub
schedules, artifacts/cache uploads, access/security or billing changes are needed.

## Scope boundary

This candidate does not change commands, store parsers/configuration, daily
messages, pricing/history algorithms, server schedules or general n8n watchdogs.
Command/menu improvements remain recommendations in the accompanying audit.

## Local quality checkpoint, 23 October

The candidate is version 0.12.3 and also includes the approved four-command menu,
saved configuration/legacy navigation and separate daily ACK status. Full Linux
regressions and bundle/import checks passed; the isolated Docker image/queue/ACK
recovery checks passed. No production changes, real messages or GitHub runs were
made. The two live receiver code-node hashes match the base template; the receiver
is active, legacy n8n daily is inactive, and its existing target matches the single
configured Laptop admin chat. Only those two verified code nodes need updating.
The old 0.12.2 recovery remains the rollback baseline. An authenticated Telegram
client is not available here; Bot API acceptance cannot prove visual client UX.
