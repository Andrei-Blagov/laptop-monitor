# 0.12.2 candidate — Work handoff

## Scope and gates

Application: `Andrei-Blagov/laptop-monitor`, existing **Draft PR #19**, branch
`codex/telegram-ru-simplify`. Baseline `a91d60e6ea8df95f69b41c9b6d5951c92d75a18f`.
Use the exact candidate commit and matching successful CI links in the final PR
handoff; re-read the PR HEAD before any action. No main merge, release tag or
production deploy was performed by Codex.

Production stays **0.12.1**, source
`d4ea1fb9885854b62192b48df6db42fa165918e6`. Preserve observation 19–26 October.
A production deploy requires separate owner approval. Cleanup of proved unused
Laptop Monitor resources is already authorized; do not request new SSH keys,
a test bot or another isolated acceptance run.

## Application changes

- Existing Russian evidence gate and hardware/ranking policy retained: <=290000
  RUB, laptop RTX 5070 Ti/5080, RAM >=32 GB, SSD >=1 TB, screen >=15.6 and the
  existing above-FHD rule; unknown CPU remains explicitly labelled.
- TOP/daily show SKU, own quote clock, 7/30-day changes when observed, confirmed
  configuration minimum and direct URL. No fabricated dates or calendar specs TTL.
- Additive nullable `price_history.configuration_key` migration. Existing rows,
  products, queues, receipts and history are never deleted/backfilled. Only
  evidenced snapshot observations get keys; recording is idempotent. A changed
  GPU/CPU/RAM/SSD/screen/SKU starts a separate price series. Normalized equivalent
  IDs and nominal 1 TB remain equivalent. Legacy prices remain in SQLite, but
  cannot establish a variant minimum or a price-drop comparison.
- Production alert generation receives the confirmed offer set before delivery.
  Cross-store comparisons and BUY use that gate; missing evidence suppresses a
  recommendation rather than filling TOP. Daily unchanged-price delivery and
  partial KNS/ANDPRO isolation keep the existing mechanisms.
- `/history` and charts default to confirmed configurations. Explicit
  `confirmed_only=False` remains a diagnostic API for archived unbound rows; the
  bot never uses it. A stale historical button requests a refreshed TOP rather
  than silently selecting a new configuration.
- Larger-screen scoring is unchanged. Cooling is not invented from a product
  name; comparable measured cooling evidence is absent. TGP, when proved, is
  displayed as TGP and is not described as a measured cooling advantage.
- `backup_db --keep 2` is available after restore/dependency review. Default stays
  seven during observation. Manual/predeploy/rollback copies are not daily
  retention candidates. Production already has Docker `json-file`, `10m` × `3`.

## Actual OVH observation (read-only)

Run: https://github.com/Andrei-Blagov/infra-ops/actions/runs/39380246276

Trusted infra main: `d73ef6debd357b119161ebbe695558d47ce5d22a`.
Artifact: `laptop-final-audit-39380246276/final-audit.json`.
Timestamp: **2026-10-20 06:25:25 UTC**. All production manifest hashes match 0.12.1;
no audit errors. Free root space **15,343,366,144 bytes = 15.34 GB / 14.29 GiB**.
Control container `7c93423b5b46d508deb8f4e063e6d2ee690184cba668c283f0d94459dfddc784`,
started `2026-10-19T10:14:44.975008303Z`, restart count 0.
This checks service/configuration state, not live Telegram delivery.

Machine-readable app-only inventory: [OVH_INVENTORY_20261020.json](OVH_INVENTORY_20261020.json).
No resources were deleted by this audit or by Codex.

| Resource | Observed size / recovery evidence | Action |
|---|---|---|
| `lm-pr19:39376742146` | Docker total 901,665,149 B; shared 285,857,792 B; 0 containers | Fixed cleanup script after all ownership/reference checks |
| `/var/tmp/lm-pr19-39376742146` | Directory inode 788, UID1001, mode0700; recursive size not measured by audit | Script checks exact archive plus **empty** data/evidence directories |
| Original `candidate.tar` | Downloaded recovery artifact: 439,571,456 B; SHA below verified by Codex | Retain on operator machine until cleanup confirmed |
| Images `0.12.1` and `0.12.0` | 2.725 GB each reported total | Preserve running production and rollback |
| Images `0.11.1`, `0.11.0`, stage1/stage21 and 0.2–0.5 releases | Listed individually in JSON; sizes include shared layers | Ownership/dependency proofs incomplete; do not mass-delete |
| `/var/lib/laptop-release-0.11.0` / `0.11.1` | 514,187,264 / 516,718,592 allocated B | Existing trusted infra scripts still reference archives; preserve |
| `/var/lib/laptop-release-0.12.0` / `0.12.1` | 534,962,176 / 549,310,464 allocated B | Preserve rollback, current release and observation evidence |
| Legacy/manual backups, old `/tmp/lm-*` | Inventory available in private audit artifact | Not proven redundant; no deletion by name/age alone |

For the failed run, Docker's image descriptor is
`sha256:fcfddd74df2a52a89f4456f3edb42ac5ae9a1bc46c75749d911e5ebdc1eff218`;
the archive configuration ID is
`sha256:80b09e5b499b30ca154444bc454064b9b0979087fd9cced2529dbe92b4905ed0`.
They are different namespaces. The cleanup implementation verifies both against
fixed evidence, compares configuration and rootfs layers, and does not weaken the
identity check to a tag/name match.

## Execute the authorized fixed cleanup through infra-ops

The currently trusted main-only workflow is read-only. The old cleanup workflow
has an October 18 candidate set and must **not** be repurposed blindly for this
run. No general SSH credential is exposed to the Codex shell. Work should use the
existing reviewed infra-ops SSH boundary with this fixed script, not enable a
new staging run or replay `pr19/controller.py` (which creates a new stage).

1. On the operator/CI machine, retain the original recovery artifact:

   ```sh
   gh run download 39376742146 --repo Andrei-Blagov/infra-ops \
     --name pr19-reviewed-package --dir pr19-recovery
   sha256sum pr19-recovery/candidate.tar
   ```

   Required SHA256:
   `f0ff205ede0c9fb20b5a88e042f57235618e533889128375ae6ef74d715f453f`.
   Artifact ID `12243731294`; original failure evidence `12242739538`.
   Codex downloaded and verified these bytes. Verify again before deleting the
   server copy; GitHub artifacts have finite retention.

2. Review and transfer **only** `scripts/cleanup_pr19_staging.py` from the pinned
   candidate to the existing trusted SSH transport (`andrei`, host
   `135.125.199.21`, existing host-key pin and `sudo -n`). Execute with Python
   `-I -B`; first without arguments. It must prove fixed inode/ownership, archive
   SHA/config, exact image labels/descriptor/layers, empty fixture directories,
   no Docker mounts/container references/process references, unchanged production.
   Preserve sanitized JSON. A refusal is a diagnostic result, not permission to
   force deletion or relax constants.
3. Run the identical reviewed script with `--apply`. Existing owner permission
   covers this scope. It removes only the exact unused image (without force),
   empty fixture directories, verified archive and empty stage root. It never
   recursively deletes an unknown tree, stops a container or touches a volume.
4. Retain JSON: exact removed IDs/paths, free bytes before/after and production
   fingerprint. Re-read Docker inventory to confirm both tag and ID absent.
   The stage must be absent. Record GiB/GB from the actual filesystem difference;
   **0.616 GB image non-shared size + 0.440 GB archive is an estimate, not a claim
   that 1.056 GB has already been freed**. Shared/containerd accounting can differ.
5. Keep disputed resources separate. No global prune, no `volume prune`, no
   mass-delete by prefix; n8n, BLAGOVA_SWEETS and other projects are outside scope.

## Controlled release after independent acceptance and separate deploy approval

Use existing release/full-state maintenance procedures. Do not run an old
infra release controller still pinned to 0.12.1 against a new package.

1. Verify approved exact application SHA, all five existing GitHub CI jobs and
   0.12.2 bundle/image manifests. Repeat independent user/server acceptance;
   actual production Telegram was deliberately not exercised in Codex.
2. Before production changes, use existing maintenance locks and full-state
   backup via kernel read-only source mount, including SQLite/WAL handling,
   specs, complete price history, queue, retry clocks, results and receipts.
   Check the copy manifest and autonomous SQLite integrity; restore into a
   separate **offline** directory and compare state/counts. Preserve a verified
   `predeploy-*` copy outside daily retention and the 0.12.1 rollback image/bundle.
   Synthetic CI restore is not a substitute for this production backup.
3. Following owner deploy approval, use the existing maintenance procedure to
   install only the verified bundle, migrate the DB additively and switch the
   existing service to the exact image. Keep production `.env`, bot credentials,
   volumes, recipients and full state. Do not perform opportunistic queue cleanup.
4. Check service health, correct version, `/status`, `/top`, `/history`, own quote
   dates/SKU, empty-result reasons, significant-drop deduplication and next daily
   08:00 MSK delivery even with unchanged prices. Check the existing bot; no new
   bot and no additional live scan unless the deploy/acceptance scope permits it.
5. Roll back through maintenance to the preserved 0.12.1 image/bundle and the
   **consistent predeploy state** if acceptance fails. The old reader would see
   appended confirmed rows as ordinary rows, so binary-only rollback is not a
   proven compatibility mode. Preserve post-upgrade receipts separately; compare
   them before resuming notifications to avoid replay. No forced queue replay.
6. After successful acceptance and the protected observation period, verify two
   newest full backup restores and active dependencies, then opt into `--keep 2`.
   Retain the additional predeploy copy until the update is accepted. Only then
   remove superseded proved-unused release/test resources by exact inventory.

## Completion report to return

Exact application/infra HEADs; CI links; production backup/restore manifest and
protected rollback; precise removed paths/image IDs; free GB before/after; bot
acceptance results and next daily receipt; disputed retained resources. Until
that evidence exists, state **candidate prepared, production unchanged, cleanup
pending execution**. Do not announce a completed deploy or cleaned VPS.
