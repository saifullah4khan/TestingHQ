branch: agents/coder-b-2026-07-16
implementing: M1 [B][M] Fake in-process sink for tests under tests/integration/; M1 [B][S] Sample target config plus examples/README.md

Scope: the engine is still forming (Coder A's 2026-07-16 session shipped
InboundEmail and serialize.py; blast/generate.py and core/transport.py are
not built yet). Per the routing note in BLAST_BACKLOG.md's UI section
("prioritize the fake in-process sink, integration tests, and examples
first, then build the web UI as the engine firms up"), this session did the
sink plus the examples items that don't depend on the not-yet-built
generator/transport, and left the two that do for a session after Lane A
lands them. No web/ work this session for the same reason.

What shipped:
- tests/integration/fake_sink.py: a real multipart/form-data encoder and
  decoder (stdlib only, no new dependency), plus FakeSink, an in-process
  stand-in for an intake endpoint. FakeSink.receive_parts(parts) takes the
  FormField/FormFile list testinghq.blast.serialize.to_multipart_parts
  already produces, encodes it to real multipart bytes, decodes it back,
  and records the result. This proves payloads survive the actual wire
  format (not just object identity) while staying hermetic: encode->decode
  happens entirely in memory, no socket ever opens. FakeSink.post(body,
  content_type) is the lower-level entry point Coder A's transport.py can
  target later with an injectable HTTP client, without a real request.
  Encoding uses a fixed default boundary (no randomness), so bytes are
  deterministic across runs.
- tests/integration/test_fake_sink.py: hermetic tests of the sink itself
  (round-trips of plain fields, file parts, unicode, empty strings, empty
  attachment filenames, binary content that ends in \r\n bytes -- a
  regression guard for a naive strip()-based decoder that would corrupt
  that case -- plus malformed-input error handling).
- tests/integration/test_intake_happy_path.py: hand-builds a small fixed
  set of clean InboundEmail payloads (blast/generate.py doesn't exist yet,
  so this isn't the seeded corpus the backlog item ultimately wants),
  dry-run posts each through the fake sink, and asserts every payload
  arrives with decoded fields matching its ground truth (from_addr,
  subject, body_core) plus full-payload fidelity (to, html, envelope,
  attachment) beyond just the three ground-truth fields. Once
  blast/generate.py lands, extend this file (or add a sibling) to drive the
  same assertions from a seeded corpus instead of the hand-built list.
- examples/target.example.toml and examples/README.md (new files only,
  didn't touch examples/demo.py -- it doesn't exist yet either). The
  README documents dry-run vs --send invocation and is explicit that
  demo.py isn't wired yet pending the generator and transport.

Deliberately not done this session (blocked on Lane A, not skipped):
- examples/demo.py wiring: needs blast/generate.py (clean generator) and
  core/transport.py, neither built yet. Did not stub either to avoid
  encroaching on Lane A's files or faking the engine.
- The fixed-seed multi-message happy-path integration test as literally
  specced (needs the same generator). test_intake_happy_path.py above
  covers the same assertions against a hand-built stand-in corpus in the
  meantime.
- Web UI: explicitly deferred until the engine (generator + transport)
  firms up, per the backlog's own guidance.

Verified locally before pushing: cloned the repo plus Coder A's branch,
`pip install -e ".[dev]"`, ran the full suite (`pytest -q`): 47 passed (28
pre-existing plus 19 new), all under tests/integration/ and none touching
testinghq/blast/** or tests/unit/** (Coder A's lane).

Remaining Lane B items for the next coder-b session: wire examples/demo.py
and the fixed-seed happy-path integration test once blast/generate.py and
core/transport.py land; then the branded web/ shell.
