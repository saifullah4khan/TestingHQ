branch: agents/coder-b-2026-07-16
implementing: M1 [B][M] Fake in-process sink for tests under tests/integration/; M1 [B][S] Sample target config plus examples/README.md

Claiming these two Lane B backlog items. The engine is still forming
(Coder A's session added InboundEmail and serialize.py; blast/generate.py
and core/transport.py aren't built yet), so this run sticks to the sink
and the examples pieces that don't depend on the generator/transport, per
the backlog's own guidance to prioritize the fake sink, integration tests,
and examples first. Full design notes to follow once implementation lands.
