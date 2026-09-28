"""`format` in a `[targets.<name>]` table, and what happens when it is wrong.

The interesting cases are the failures. A format name is a string an operator
types, so the plausible mistakes are a typo, a name that is not a string, and
the case where the value is absent. Each has to fail at load with a message that
says what to write, or at send with bytes nobody intended.
"""
from __future__ import annotations

import pytest

from testinghq.core.config import ConfigError, Target, load_config


def _write(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_target_with_no_format_gets_the_default(tmp_path):
    """The compatibility case. A config written before formats were selectable
    must keep meaning SendGrid, and the default has to be visible on the loaded
    target rather than implied by a None."""
    config = load_config(_write(tmp_path, """
[targets.local]
url = "http://127.0.0.1:9000/inbound"
"""))
    assert config.get("local").wire_format == "sendgrid"


def test_a_target_can_name_a_format(tmp_path):
    config = load_config(_write(tmp_path, """
[targets.local]
url = "http://127.0.0.1:9000/inbound"
format = "sendgrid"
"""))
    assert config.get("local").wire_format == "sendgrid"


def test_each_target_selects_its_own_format(tmp_path):
    """Per target, not global. Two endpoints in one config, each speaking a
    different protocol, is the normal case this exists for."""
    config = load_config(_write(tmp_path, """
[targets.a]
url = "http://127.0.0.1:9000/a"

[targets.b]
url = "http://127.0.0.1:9000/b"
format = "sendgrid"
"""))
    assert config.get("a").wire_format == "sendgrid"
    assert config.get("b").wire_format == "sendgrid"


def test_an_unknown_format_is_refused_at_load(tmp_path):
    """At load, not at send. A dry-run that looked fine and then posted the
    wrong bytes is the failure mode this avoids."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, """
[targets.local]
url = "http://127.0.0.1:9000/inbound"
format = "sendgridd"
"""))
    message = str(excinfo.value)
    assert "sendgridd" in message
    assert "sendgrid" in message, "the error must name the valid values"


def test_a_non_string_format_is_refused_and_says_where_it_came_from(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, """
[targets.local]
url = "http://127.0.0.1:9000/inbound"
format = 3
"""))
    message = str(excinfo.value)
    assert "targets.local.format" in message
    assert "int" in message


def test_an_empty_format_is_refused(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, """
[targets.local]
url = "http://127.0.0.1:9000/inbound"
format = ""
"""))
    assert "format" in str(excinfo.value)


def test_a_constructed_target_validates_its_format_too():
    """Not only via the loader. `Target` is public and the CLI builds one, so
    the check has to live where the object is made."""
    with pytest.raises(ConfigError) as excinfo:
        Target(name="x", url="http://127.0.0.1:9000", wire_format="nope")
    assert "nope" in str(excinfo.value)


def test_the_default_on_a_constructed_target_is_sendgrid():
    assert Target(name="x", url="http://127.0.0.1:9000").wire_format == "sendgrid"


def test_an_env_url_override_does_not_disturb_the_format(tmp_path):
    """The env override replaces the URL only. Coupling the two would let a
    token refresh silently change the protocol."""
    config = load_config(
        _write(tmp_path, """
[targets.local]
url = "http://127.0.0.1:9000/inbound"
format = "sendgrid"
"""),
        env={"TESTINGHQ_TARGET_LOCAL_URL": "http://127.0.0.1:9100/other"},
    )
    assert config.get("local").url == "http://127.0.0.1:9100/other"
    assert config.get("local").wire_format == "sendgrid"
