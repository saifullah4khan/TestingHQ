import pytest

from testinghq.core.config import Config, ConfigError, Target, load_config


def _write(tmp_path, text):
    path = tmp_path / "targets.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_reads_declared_targets(tmp_path):
    path = _write(
        tmp_path,
        """
        [targets.local]
        url = "http://localhost:8000/inbound"

        [targets.staging]
        url = "https://staging.example.test/inbound"
        """,
    )
    config = load_config(path, env={})
    assert set(config.allowed_target_names()) == {"local", "staging"}
    assert config.get("local").url == "http://localhost:8000/inbound"
    assert config.get("staging").url == "https://staging.example.test/inbound"


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.toml", env={})


def test_malformed_toml_raises_config_error(tmp_path):
    path = _write(tmp_path, "this is not [valid toml")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_targets_table_must_be_a_table(tmp_path):
    path = _write(tmp_path, 'targets = "not a table"\n')
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_empty_targets_table_raises_config_error(tmp_path):
    path = _write(tmp_path, "[targets]\n")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_target_missing_url_raises_config_error(tmp_path):
    path = _write(tmp_path, "[targets.local]\nname = \"local\"\n")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_target_entry_must_be_a_table(tmp_path):
    path = _write(tmp_path, 'targets.local = "oops"\n')
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_target_url_must_be_a_string(tmp_path):
    path = _write(tmp_path, "[targets.local]\nurl = 8000\n")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_target_url_must_have_http_scheme(tmp_path):
    path = _write(tmp_path, '[targets.local]\nurl = "not-a-url"\n')
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_env_overrides_declared_target_url(tmp_path):
    path = _write(tmp_path, '[targets.local]\nurl = "http://localhost:8000/inbound"\n')
    config = load_config(
        path, env={"TESTINGHQ_TARGET_LOCAL_URL": "http://127.0.0.1:9999/inbound"}
    )
    assert config.get("local").url == "http://127.0.0.1:9999/inbound"


def test_env_override_key_normalizes_hyphenated_names(tmp_path):
    path = _write(tmp_path, '[targets."my-target"]\nurl = "http://localhost:8000/inbound"\n')
    config = load_config(
        path, env={"TESTINGHQ_TARGET_MY_TARGET_URL": "http://overridden.example.test/inbound"}
    )
    assert config.get("my-target").url == "http://overridden.example.test/inbound"


def test_env_does_not_invent_undeclared_targets(tmp_path):
    path = _write(tmp_path, '[targets.local]\nurl = "http://localhost:8000/inbound"\n')
    config = load_config(
        path, env={"TESTINGHQ_TARGET_GHOST_URL": "http://ghost.example.test/inbound"}
    )
    assert "ghost" not in config.allowed_target_names()


def test_get_raises_for_unconfigured_target(tmp_path):
    path = _write(tmp_path, '[targets.local]\nurl = "http://localhost:8000/inbound"\n')
    config = load_config(path, env={})
    with pytest.raises(ConfigError):
        config.get("nonexistent")


def test_target_urls_lists_all_urls(tmp_path):
    path = _write(
        tmp_path,
        """
        [targets.a]
        url = "http://a.example.test/inbound"
        [targets.b]
        url = "http://b.example.test/inbound"
        """,
    )
    config = load_config(path, env={})
    assert sorted(config.target_urls()) == [
        "http://a.example.test/inbound",
        "http://b.example.test/inbound",
    ]


def test_target_rejects_empty_name_or_url():
    with pytest.raises(ConfigError):
        Target(name="", url="http://x.example.test/")
    with pytest.raises(ConfigError):
        Target(name="x", url="")


def test_config_default_is_empty():
    config = Config()
    assert config.targets == {}
    assert config.target_urls() == []
    assert config.allowed_target_names() == []
