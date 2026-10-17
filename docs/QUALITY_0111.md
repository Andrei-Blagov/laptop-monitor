# 0.11.1 — correction of saved-output quality

The owner authorized this corrective release after read-only diagnosis
infra-ops run 39019766866 of scan f2035f4a-841c-492a-8f96-9734e174be47.
This is a patch release: hardware gates, score weights and A–F2 remain intact.

- A successful Product quote has its own price_checked_at. Legacy successful
  last_checked_at values migrate unchanged, without assigning export/pipeline
  time. Failed store collections do not persist products. last_checked_at
  remains the availability clock. price_history remains an append-only change
  history; snapshot price_changed_at ignores stock-only history changes.
- Explicit desktop graphics/component listings are excluded before Thai
  catalog/ranking. SpeedCom reads an own storage label instead of accepting
  a RAM number followed by SSD. Missing evidence still prevents purchase.
  Factual saved titles are fixtures; reconstructed body strings are labelled,
  since the original SpeedCom responses were not retained.
- User messages show Moscow time, memory units, understandable source states
  and an explicit empty-RU explanation. Raw diagnostics remain in saved JSON.
- Physical message reuse requires identical complete formatted presentation,
  result and recipient. Each original event/view still receives its own ACK
  and receipt. A content ACK is written immediately after successful HTTP,
  before the view receipt; a failure between these writes can recover from
  the content ACK. The existing HTTP-before-first-durable-ACK crash window
  remains; exactly-once is not claimed. Different text, result or recipient
  never reuses that ACK.

Targeted offline check:
`python -m scripts.check_offline --tests tests.test_quality_correction tests.test_market_snapshot tests.test_market_telegram tests.test_market_events tests.test_thailand_speedcom tests.test_thailand_jib`

Final source: full `python -m scripts.check_offline` and GitHub-hosted real
Docker checks. No live shop/CBR/Telegram calls are part of development tests.
Deployment needs a new exact-SHA image, private backup/restore and verification
with both previous/new images. The new nullable SQLite column is compatible
with the previous image; rollback must preserve new delivery/content ACKs,
queues, history and Retry-After. Never restore old receipts over delivered work.
Historical replay must prove input provenance; it may not infer missing specs
or substitute newer observations. One new live control cycle only after deploy.

Thai catalog compatibility includes evidence revision0111-laptop-components-own-ssd-v1. Pre-fix retained evidence is refreshed by the standard planner without editing original observations or bypassing source backoff.
