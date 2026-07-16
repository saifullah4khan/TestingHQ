"""Tests for the target allow-list loader in web/config.py.

No free-text target ever reaches the engine: everything the UI can pick from
comes out of this loader, and it must fail loudly (never silently) on a
malformed or unsafe targets file.

Host safety is delegated to the canonical guardrail rather than decided
here, so these tests assert the delegated OUTCOME (unsafe hosts refused,
reserved and private ones admitted) rather than a local list of suffixes.
"""
import json

import pytest

from testinghq.core import guardrails

from web import config


def test_default_targets_file_loads_and_validates():
    targets = config.load_targets()
    assert len(targets) >= 1
    for name, target in targets.items():
        assert target.name == name
        assert target.url.startswith("http://") or target.url.startswith("https://")


def test_target_names_helper():
    targets = config.load_targets()
    names = config.target_names(targets)
    assert set(names) == set(targets.keys())


def test_missing_file_raises_config_error(tmp_path):
    missing = tmp_path / "no-such-file.json"
    with pytest.raises(config.ConfigError):
        config.load_targets(missing)


def test_invalid_json_raises_config_error(tmp_path):
    bad = tmp_path / "targets.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_targets(bad)


def test_empty_targets_list_raises(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"targets": []}), encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_targets(path)


def test_non_reserved_domain_is_rejected(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps({"targets": [{"name": "evil", "url": "https://real-inbox.gmail.com/hook"}]}),
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError):
        config.load_targets(path)


def test_non_http_scheme_is_rejected(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps({"targets": [{"name": "bad", "url": "ftp://example.com/hook"}]}),
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError):
        config.load_targets(path)


def test_reserved_domain_variants_are_accepted(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            {
                "targets": [
                    {"name": "a", "url": "https://sink.example.com/hook"},
                    {"name": "b", "url": "https://qa.example.test/hook"},
                    {"name": "c", "url": "http://localhost:9000/hook"},
                    {"name": "d", "url": "https://x.example.invalid/hook"},
                ]
            }
        ),
        encoding="utf-8",
    )
    targets = config.load_targets(path)
    assert set(targets.keys()) == {"a", "b", "c", "d"}


def test_public_host_target_is_refused_at_load_time(tmp_path):
    """A real public host in targets.json must never become selectable in
    the UI dropdown. The refusal is the canonical guardrail's, surfaced as a
    ConfigError so the loader still fails loudly.
    """
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps({"targets": [{"name": "prod", "url": "https://ingest.mycompany.com/hook"}]}),
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError) as exc_info:
        config.load_targets(path)
    assert "non-reserved public host" in str(exc_info.value)


def test_host_safety_is_delegated_to_the_canonical_guardrail(tmp_path, monkeypatch):
    """web/config.py must not keep its own opinion about which hosts are
    safe. Spying on the canonical guard proves the loader routes through it.
    """
    calls = []
    real_guard = guardrails.require_configured_target

    def recording_guard(target, allowed_targets, **kwargs):
        calls.append(target)
        return real_guard(target, allowed_targets, **kwargs)

    monkeypatch.setattr(guardrails, "require_configured_target", recording_guard)

    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps({"targets": [{"name": "a", "url": "https://sink.example.com/hook"}]}),
        encoding="utf-8",
    )
    config.load_targets(path)

    assert "https://sink.example.com/hook" in calls, (
        "config.load_targets did not validate the url through the canonical guardrail"
    )


def test_config_module_defines_no_local_reserved_domain_list():
    """The duplicate reserved-domain list is gone for good. If it comes back,
    the two definitions can drift again, which is the whole point of this.
    """
    assert not hasattr(config, "_RESERVED_DOMAIN_SUFFIXES")
    assert not hasattr(config, "_is_reserved_host")
