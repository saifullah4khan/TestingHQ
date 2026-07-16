"""Tests for web/adapter.py - the one seam between the UI and the engine.

This pins down the guardrail behavior the whole assignment cares about:
dry-run never sends, firing requires a configured target AND an explicit
confirm, and there is no way to fire at an arbitrary URL.
"""
import pytest

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
    with pytest.raises(adapter.AdapterGuardrailError):
        adapter.fire("ok-target", ["clean"], 5, seed=0, confirm=False, targets=_fake_targets())


def test_fire_with_truthy_but_not_true_confirm_is_refused():
    # confirm must be an explicit True, not just any truthy value.
    with pytest.raises(adapter.AdapterGuardrailError):
        adapter.fire("ok-target", ["clean"], 5, seed=0, confirm=1, targets=_fake_targets())
    with pytest.raises(adapter.AdapterGuardrailError):
        adapter.fire("ok-target", ["clean"], 5, seed=0, confirm="yes", targets=_fake_targets())


def test_fire_at_unconfigured_target_is_refused():
    with pytest.raises(adapter.AdapterGuardrailError):
        adapter.fire("not-a-real-target", ["clean"], 5, seed=0, confirm=True, targets=_fake_targets())


def test_fire_at_arbitrary_url_is_refused_even_with_confirm():
    with pytest.raises(adapter.AdapterGuardrailError):
        adapter.fire(
            "https://evil.example.com/hook",
            ["clean"],
            5,
            seed=0,
            confirm=True,
            targets=_fake_targets(),
        )


def test_fire_with_empty_target_is_refused():
    with pytest.raises(adapter.AdapterGuardrailError):
        adapter.fire("", ["clean"], 5, seed=0, confirm=True, targets=_fake_targets())
    with pytest.raises(adapter.AdapterGuardrailError):
        adapter.fire(None, ["clean"], 5, seed=0, confirm=True, targets=_fake_targets())


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
