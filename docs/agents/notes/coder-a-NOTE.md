branch: agents/coder-a-2026-07-16
implementing: M1 [A][M] InboundEmail model in blast/payload.py; M1 [A][M] Serialize InboundEmail to SendGrid Inbound Parse multipart fields (parsed mode)

Scope: these two backlog items are a natural pair (model, then its wire
serialization) and both land under testinghq/blast/**. Left the transport,
generator, and determinism-test items for a later Lane A session so this run
stays focused and high quality within the window.

Design notes for the reviewer and Coder B:
- InboundEmail, Envelope, Attachment, GroundTruth are frozen dataclasses in
  testinghq/blast/payload.py. Envelope and Attachment are separate types
  (not raw dicts) so callers get validation and immutability for free.
- GroundTruth (from_addr, subject, body_core) is a required field on
  InboundEmail, not optional, per the backlog note about carrying
  ground-truth metadata for later assertion (M3 reporting will read it).
- testinghq/blast/serialize.py turns an InboundEmail into an ordered list of
  FormField/FormFile parts shaped like SendGrid's Inbound Parse webhook in
  parsed mode (headers, to, from, subject, text, html, envelope, charsets,
  attachments count, attachment-info, attachment1..N). It does not know how
  to POST anything; testinghq/core/transport.py (not yet built) is expected
  to consume this list. envelope and charsets are JSON via json.dumps with
  sort_keys=True and compact separators for determinism.
- Tests live under tests/unit/test_payload.py and tests/unit/test_serialize.py
  (new tests/unit/ subpackage; existing tests/test_cli.py and
  tests/test_guardrails.py were untouched).

Remaining M1 Lane A items for the next coder-a session: clean generator
(blast/generate.py), transport (core/transport.py), determinism test.
