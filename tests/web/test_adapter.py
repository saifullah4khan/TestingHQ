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
"""
import pytest

from testinghq.core import guardrails

from web import adapter, config, generator


def test_dry_run_never_requires_a_target():
    artifact = adapter.dry_run(["clean"], 5, seed=0)
    assert artifact["config"]["dry_run"] is True
    assert artifact["config"]["target"] is None


def test_dry_run_matches_generator_output_directly():
    from_adapter = adapter.dry_run(["clean", "degenerate"], 8, seed=3)
    from_generator = generator.generate_run(
        ["clean", "degenerate"], 8, seed=3, target=None, dry_run=True
    )
    assert from_adapter == from_generator


def _fake_targets():
    return {"ok-target": config.Target(name="ok-target", url="https://ok.example.com/hook")}


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
    artifact = adapter.fire(
        "ok-target", ["clean"], 5, seed=0, confirm=True, targets=_fake_targets()
    )
    assert artifact["config"]["dry_run"] is False
    assert artifact["config"]["target"] == "ok-target"
    assert len(artifact["records"]) == 5


def test_fire_uses_real_config_loader_by_default():
    real_targets = config.load_targets()
    some_name = next(iter(real_targets))
    artifact = adapter.fire(some_name, ["clean"], 3, seed=0, confirm=True)
    assert artifact["config"]["target"] == some_name


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

    adapter.fire("ok-target", ["clean"], 3, seed=0, confirm=True, targets=_fake_targets())

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
