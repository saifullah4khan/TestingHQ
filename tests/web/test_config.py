"""Tests for the target allow-list loader in web/config.py.

No free-text target ever reaches the engine: everything the UI can pick from
comes out of this loader, and it must fail loudly (never silently) on a
malformed or unsafe targets file.
"""
import json

import pytest

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
