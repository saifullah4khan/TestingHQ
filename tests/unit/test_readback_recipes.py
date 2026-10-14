"""Every readback recipe loads with the real loaders and builds an adapter.

A recipe the config loader or the readback parser rejects is a broken example.
These tests load each one exactly as a run would, with placeholder values for
the environment variables it names, and build the adapter without a socket.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from testinghq.core.config import load_config
from testinghq.pipeline.adapters import AdapterError, build_adapter, parse_readback_config

REPO_ROOT = Path(__file__).resolve().parents[2]
RECIPES = REPO_ROOT / "examples" / "readback"
NAMES = sorted(p.name for p in RECIPES.glob("*.toml"))


class _NoNetwork:
    def send(self, request):  # pragma: no cover - building must not send
        raise AssertionError(f"a recipe build sent {request.url}")


def _raw(name: str):
    return load_config(RECIPES / name).readback_table()


def _placeholder_env(raw) -> dict:
    return {
        value[len("env:"):]: "placeholder-not-a-credential"
        for value in (raw.get("headers") or {}).values()
        if isinstance(value, str) and value.startswith("env:")
    }


def test_the_recipes_include_the_four_vendors_and_the_two_generic_ones():
    assert {
        "zendesk.toml", "freshdesk.toml", "mailpit.toml", "chatwoot.toml",
        "generic-rest.toml", "mail-sink.toml",
    } <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_a_recipe_loads_and_builds_an_adapter(name, tmp_path):
    config = load_config(RECIPES / name)
    assert config.targets, f"{name} declares no firing target"
    raw = config.readback_table()
    assert raw is not None, f"{name} declares no [readback] table"

    readback = parse_readback_config(raw, env=_placeholder_env(raw))
    assert readback.field_map(), f"{name} maps no fields"
    adapter = build_adapter(readback, client=_NoNetwork(), config_dir=str(tmp_path))
    assert callable(getattr(adapter, "fetch", None))


@pytest.mark.parametrize(
    "name", [n for n in NAMES if (_raw(n) or {}).get("headers")]
)
def test_a_recipe_with_credentials_is_refused_when_they_are_unset(name):
    with pytest.raises(AdapterError, match="not set"):
        parse_readback_config(_raw(name), env={})
