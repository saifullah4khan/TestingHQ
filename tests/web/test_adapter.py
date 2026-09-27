"""Tests for web/adapter.py - the one seam between the UI and the engine.

This pins down the guardrail behavior the whole assignment cares about:
dry-run never sends, firing requires a configured target AND an explicit
confirm, and there is no way to fire at an arbitrary URL.

Since the guardrail rules are canonical (testinghq.core.guardrails) rather
than reimplemented here, these tests also pin the WIRING: that the adapter
actually delegates to the canonical module, and delegates in a way that lets
the canonical public-host check reach the real destination URL. If someone
later re-inlines a local copy of the rules, or quietly passes the target
name where the URL belongs, these fail.

Since 2026-09-27 the adapter runs the real engine, so every fire test injects
a fake HTTP client. Before that the adapter called a fixture generator that
opened no socket, so the tests had no reason to. That changed here: a fire
test that did not inject a client would be a test that hit
ok.example.com over the internet, which is slow, flaky, and nothing anyone
wants in CI. `_RecordingClient` makes the transport hermetic and lets these
tests also assert on the bytes that would have gone over the wire.
"""
import pytest

from testinghq.core import guardrails, report
from testinghq.core.transport import ClientResponse

from web import adapter, config, generator


class _RecordingClient:
    """A fake transport client implementing the HttpClient shape.

    Returns 200 for everything and records the PreparedRequests it was
    handed, so a test can assert that the real engine really serialized a
    real payload rather than the adapter short-circuiting somewhere.
    """

    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self.body = body
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ClientResponse(status=self.status, body=self.body)


def _no_network(monkeypatch):
    """Make any socket use raise. Returns nothing; call it before the action
    under test. Proving the zero-network property by patching the socket
    module is stronger than asserting on a counter nobody can fool."""
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("this path must not open a socket")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)


def test_dry_run_never_requires_a_target():
    artifact = adapter.dry_run(["clean"], 5, seed=0)
    assert artifact["config"]["dry_run"] is True
    assert artifact["config"]["target"] is None


def test_dry_run_opens_no_socket(monkeypatch):
    """Proven, not asserted. The fixture generator could not have opened a
    socket either, but now that the adapter builds real payloads and the fire
    path really does have a transport, the dry-run guarantee is worth pinning
    directly."""
    _no_network(monkeypatch)
    artifact = adapter.dry_run(["clean", "degenerate"], 10, seed=4)
    assert len(artifact["records"]) == 10


def test_dry_run_builds_records_with_the_real_engine():
    """The swap this lane existed to make. The dry-run payload hashes must be
    the engine's own, computed from a real generated and corrupted corpus, not
    the fixture generator's stand-in hashes."""
    from testinghq.blast.corrupt import corrupt_corpus
    from testinghq.blast.generate import generate_corpus

    artifact = adapter.dry_run(["clean", "degenerate"], 8, seed=3)
    expected = corrupt_corpus(generate_corpus(3, 8), 3, {
        "clean": 0.20,
        "degenerate": 0.05,
    })

    assert len(artifact["records"]) == len(expected) == 8
    for record, (email, recipe) in zip(artifact["records"], expected):
        assert record["payload_sha256"] == report.payload_sha256(email), (
            "the adapter is not building records from the real engine's payloads"
        )
        assert record["category"] == report.category_label(recipe)


def test_dry_run_is_byte_identical_for_the_same_seed():
    """Determinism is load-bearing across the whole product, and the UI now
    runs the same generator the CLI does, so it inherits the guarantee and
    must be checked for it here too."""
    first = adapter.dry_run(["clean", "degenerate"], 12, seed=9)
    second = adapter.dry_run(["clean", "degenerate"], 12, seed=9)
    assert first == second


def test_dry_run_summary_reports_no_phantom_timeouts():
    """A dry run has no responses. If the summary were computed with the
    normal rules every payload would be counted as a timeout and every
    degenerate one flagged as a failure, which is both true and nonsense:
    the endpoint was never asked."""
    artifact = adapter.dry_run(["degenerate"], 6, seed=1)

    assert artifact["summary"]["by_status_class"] == {
        "2xx": 0,
        "4xx": 0,
        "5xx": 0,
        "timeout": 0,
    }
    assert artifact["summary"]["flags"] == []
    for record in artifact["records"]:
        assert record["assertion"] == {"passed": True, "mismatches": []}
    # The category tally is real, because the corpus really was built.
    assert artifact["summary"]["by_category"]["degenerate"] == 6


def test_dry_run_rejects_a_bad_count_or_seed():
    for bad_count in (-1, "5", 1.5, True):
        with pytest.raises(generator.GeneratorError):
            adapter.dry_run(["clean"], bad_count, seed=0)
    with pytest.raises(generator.GeneratorError):
        adapter.dry_run(["clean"], 5, seed="0")


def test_dry_run_rejects_an_unknown_category():
    with pytest.raises(generator.GeneratorError):
        adapter.dry_run(["clean", "not-a-category"], 5, seed=0)


def _fake_targets():
    return {"ok-target": config.Target(name="ok-target", url="https://ok.example.com/hook")}


@pytest.fixture(autouse=True)
def no_default_transport_client(monkeypatch):
    """Make the real transport client unusable for the whole module.

    `core.transport.post` builds a real `UrllibHttpClient` when it is handed
    no client. Since the adapter now runs the real engine, a fire test that
    forgets to inject one does not quietly do nothing: it opens a socket to
    whatever `web/targets.json` points at, which is a localhost URL, and then
    waits out the transport timeout. That is how a duplicate test definition
    managed to add twelve seconds to this suite while still passing.

    Failing loudly and instantly is the correct signal for "this test reached
    the network by accident". Tests that genuinely want the real client do not
    exist here and should not be added without saying so.
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "a web test reached the default transport client and would have "
            "opened a socket. Inject a fake client instead."
        )

    monkeypatch.setattr("testinghq.core.transport.UrllibHttpClient", _forbidden)


def test_fire_without_confirm_is_refused():
    with pytest.raises(guardrails.GuardrailError):
        adapter.fire("ok-target", ["clean"], 5, seed=0, confirm=False, targets=_fake_targets())


def test_fire_with_truthy_but_not_true_confirm_is_refused():
    # confirm must be an explicit True, not just any truthy value. This is
    # the UI-layer narrowing on top of the canonical send gate.
    for sneaky in (1, "yes", "false", 0.1, [1]):
        with pytest.raises(guardrails.GuardrailError):
            adapter.fire(
                "ok-target", ["clean"], 5, seed=0, confirm=sneaky, targets=_fake_targets()
            )


def test_fire_at_unconfigured_target_is_refused():
    with pytest.raises(guardrails.GuardrailError):
        adapter.fire(
            "not-a-real-target", ["clean"], 5, seed=0, confirm=True, targets=_fake_targets()
        )


def test_fire_at_arbitrary_url_is_refused_even_with_confirm():
    with pytest.raises(guardrails.GuardrailError):
        adapter.fire(
            "https://evil.example.com/hook",
            ["clean"],
            5,
            seed=0,
            confirm=True,
            targets=_fake_targets(),
        )


def test_fire_with_empty_target_is_refused():
    for empty in ("", None):
        with pytest.raises(guardrails.GuardrailError):
            adapter.fire(empty, ["clean"], 5, seed=0, confirm=True, targets=_fake_targets())


def test_fire_with_configured_target_and_explicit_confirm_succeeds():
    client = _RecordingClient()
    artifact = adapter.fire(
        "ok-target",
        ["clean"],
        5,
        seed=0,
        confirm=True,
        targets=_fake_targets(),
        client=client,
    )
    assert artifact["config"]["dry_run"] is False
    assert artifact["config"]["target"] == "ok-target"
    assert len(artifact["records"]) == 5
    assert len(client.requests) == 5


def test_fire_posts_the_real_serialized_payload():
    """The point of the swap: what goes over the wire is the engine's real
    Inbound Parse multipart body, not a synthesized stand-in. Compares the
    bytes the transport actually produced against what the engine's own
    build_request would produce for the same corpus entry."""
    from testinghq.blast.corrupt import corrupt_corpus
    from testinghq.blast.generate import generate_corpus
    from testinghq.core import transport

    client = _RecordingClient()
    adapter.fire(
        "ok-target",
        ["clean"],
        3,
        seed=5,
        confirm=True,
        targets=_fake_targets(),
        client=client,
    )

    expected_pairs = corrupt_corpus(generate_corpus(5, 3), 5, {"clean": 0.20})

    assert len(client.requests) == len(expected_pairs) == 3
    for request, (email, _recipe) in zip(client.requests, expected_pairs):
        expected = transport.build_request(email, "https://ok.example.com/hook")
        assert request.url == "https://ok.example.com/hook"
        assert request.method == "POST"
        assert request.body == expected.body
        assert request.headers["Content-Type"].startswith("multipart/form-data;")
    # And the bodies really are multipart bodies, not a fixture.
    assert b"------testinghq-boundary" in client.requests[0].body
    # Distinct payloads, not the same one resent: a swap that reused one
    # payload would satisfy every assertion above.
    assert len({r.body for r in client.requests}) == 3


def test_fire_records_come_from_the_real_engine():
    client = _RecordingClient(status=200)
    artifact = adapter.fire(
        "ok-target",
        ["clean", "degenerate"],
        10,
        seed=2,
        confirm=True,
        targets=_fake_targets(),
        client=client,
    )

    assert artifact["summary"] == report.compute_summary(
        artifact["records"], artifact["seed"], artifact["config"]
    ), "the fire summary must be the engine's own, not a UI-local recount"
    assert sum(artifact["summary"]["by_category"].values()) == 10


def test_fire_records_a_transport_failure_as_no_status():
    """A client that raises is a transport failure, and the engine models
    that as a null status. The UI must show it as such rather than inventing
    a 5xx."""
    class _ExplodingClient:
        def send(self, request):
            raise OSError("connection refused")

    artifact = adapter.fire(
        "ok-target",
        ["clean"],
        4,
        seed=0,
        confirm=True,
        targets=_fake_targets(),
        client=_ExplodingClient(),
    )
    assert artifact["summary"]["by_status_class"]["timeout"] == 4
    for record in artifact["records"]:
        assert record["response"]["status"] is None


def test_fire_uses_real_config_loader_by_default():
    real_targets = config.load_targets()
    some_name = next(iter(real_targets))
    client = _RecordingClient()
    artifact = adapter.fire(some_name, ["clean"], 3, seed=0, confirm=True, client=client)
    assert artifact["config"]["target"] == some_name
    assert len(client.requests) == 3


# ---------------------------------------------------------------------------
# Wiring: refusals must come from the canonical guardrail, not a local copy
# ---------------------------------------------------------------------------


def _record_calls(monkeypatch):
    """Wrap the canonical guard so we can see it was really called, while
    still letting the real implementation decide the outcome.
    """
    calls = []
    real_guard = guardrails.require_configured_target

    def recording_guard(target, allowed_targets, **kwargs):
        allowed = tuple(allowed_targets or ())
        calls.append({"target": target, "allowed": allowed, "kwargs": kwargs})
        return real_guard(target, allowed_targets, **kwargs)

    monkeypatch.setattr(guardrails, "require_configured_target", recording_guard)
    return calls


def test_unconfigured_target_refusal_comes_from_canonical_guardrail(monkeypatch):
    """The UI's fire path must refuse an unconfigured target THROUGH the
    canonical guardrail. Spying proves the call actually happens; if someone
    re-inlines a local membership check, the spy never fires and this fails.
    """
    calls = _record_calls(monkeypatch)

    with pytest.raises(guardrails.GuardrailError):
        adapter.fire(
            "not-configured", ["clean"], 3, seed=0, confirm=True, targets=_fake_targets()
        )

    assert calls, "adapter.fire did not call the canonical require_configured_target"
    first = calls[0]
    assert first["target"] == "not-configured"
    assert set(first["allowed"]) == {"ok-target"}
    # The UI must never opt out of the public-host hardening.
    assert "allow_public_hosts" not in first["kwargs"]


def test_fire_passes_the_resolved_url_through_the_canonical_host_check(monkeypatch):
    """The canonical public-host check parses a host out of its argument, so
    it is only meaningful if the adapter hands it the target's URL. Passing
    only the bare name would make the check vacuous, because a single-label
    name is classified as an internal host and always passes.
    """
    calls = _record_calls(monkeypatch)

    adapter.fire(
        "ok-target",
        ["clean"],
        3,
        seed=0,
        confirm=True,
        targets=_fake_targets(),
        client=_RecordingClient(),
    )

    submitted = [c["target"] for c in calls]
    assert "https://ok.example.com/hook" in submitted, (
        "adapter.fire never passed the resolved target URL to the canonical "
        "guardrail, so the public-host check cannot bite"
    )
    for call in calls:
        assert "allow_public_hosts" not in call["kwargs"]


def test_configured_target_with_public_host_is_refused_by_canonical_check():
    """The exact failure the hardening exists to catch: a target that IS in
    the allow-list but points at a real, publicly routable host. The adapter
    must refuse it because the canonical guardrail refuses it, not because
    the web lane keeps its own opinion about hosts.
    """
    public_targets = {
        "prod-real": config.Target(name="prod-real", url="https://ingest.mycompany.com/hook")
    }
    with pytest.raises(guardrails.GuardrailError) as exc_info:
        adapter.fire("prod-real", ["clean"], 3, seed=0, confirm=True, targets=public_targets)
    assert "non-reserved public host" in str(exc_info.value)


def test_public_ip_target_is_refused_by_canonical_check():
    public_targets = {
        "prod-ip": config.Target(name="prod-ip", url="http://93.184.216.34/hook")
    }
    with pytest.raises(guardrails.GuardrailError):
        adapter.fire("prod-ip", ["clean"], 3, seed=0, confirm=True, targets=public_targets)


def test_adapter_does_not_define_a_competing_guardrail_error():
    """One exception hierarchy: a caller catching the canonical error must
    catch everything the adapter raises.
    """
    assert not hasattr(adapter, "AdapterGuardrailError")
