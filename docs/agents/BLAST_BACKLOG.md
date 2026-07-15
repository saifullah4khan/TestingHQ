# Blast backlog

Priority-ordered, one-session-sized, lane-split, size-tagged. Coders self-direct
from this file. Lane A claims A items, Lane B claims B items. If your items are
done or missing, pick the highest-priority not-done item in your lane that
advances the current milestone.

## How this squad runs

Deterministic given a seed is non-negotiable: same seed plus same config yields
byte-identical output. Never let wall-clock time or unseeded randomness leak into
generated output (seed attachment bytes too). Guardrails are first-class and
already exist in `core/guardrails.py`; extend, never weaken them. Tests must be
real and hermetic (no network; inject clocks and transports). Never fix a test to
match the code.

## M1 - clean path end to end

- [ ] [A][M] InboundEmail model in `blast/payload.py`: fields for headers, to,
  from, subject, text, html, envelope, charsets, attachments. Carry per-payload
  ground-truth metadata (intended from, subject, body core) for later assertion.
- [ ] [A][M] Serialize InboundEmail to SendGrid Inbound Parse multipart fields
  (parsed mode). Envelope and charsets as valid JSON strings.
- [ ] [A][M] Clean generator in `blast/generate.py`: produce well-formed emails
  from a seed, fully deterministic.
- [ ] [A][M] Transport in `core/transport.py`: POST multipart to a configured
  target with an injectable HTTP client; timeout and response capture.
- [ ] [B][M] Fake in-process sink for tests under `tests/integration/`: receives a
  posted payload and records it intact.
- [ ] [B][S] Wire `examples/demo.py`: generate a clean corpus and dry-run it end
  to end against the fake sink.
- [ ] [A][S] Determinism test: same seed plus config yields byte-identical output.

## M2 - chaos (next)

- [ ] [A] Messiness levels and the composable mutator pipeline in `blast/corrupt.py`.
- [ ] [A] Gibberish and encoding-sabotage mutators (declared charset versus actual
  bytes is the highest-value one).
- [ ] [A] Attachment generation in `blast/attachments.py` (types, sizes, degenerate).
- [ ] [A] Named edge-case catalog in `blast/catalog.py`.
- [ ] [B] Mutator and category-mix integration tests.

## M3 - reporting and reproducibility (later)

- [ ] [A] Saved run artifact (JSON with seed and per-message records) and replay.
- [ ] [A] Category-versus-outcome summary (expectation-based, not just status).
- [ ] [A] Generic assertion hook: compare intended ground truth against a
  configurable readback matcher; ship a status-only default matcher.
- [ ] [B] Reporting and replay integration tests.

## UI (v1, Lane B, in parallel as the engine firms up)

- [ ] [B] Branded `web/` shell over the engine: pick mix, count, seed; dry-run or
  fire at a configured target; stream results; show the category-versus-outcome view.
