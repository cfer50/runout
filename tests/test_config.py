from pathlib import Path

import pytest

from runout.config import ConfigError, load, parse

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_parses(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "x")
    monkeypatch.setenv("MALOJA_KEY", "y")
    cfg = load(ROOT / "config.example.toml")
    assert cfg.trigger.type == "homeassistant"
    assert [t.type for t in cfg.targets] == ["listenbrainz", "log"]


def test_env_values(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    cfg = parse({"target": [{"type": "listenbrainz", "url": "http://x", "token": "env:TOK"}]})
    assert cfg.targets[0].options["token"] == "secret"


def test_missing_env_is_an_error(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError, match="NOPE"):
        parse({"target": [{"type": "log", "x": "env:NOPE"}]})


def test_needs_a_target():
    with pytest.raises(ConfigError, match="target"):
        parse({})


def test_unknown_key_is_reported():
    with pytest.raises(ConfigError, match="sampel_seconds"):
        parse({"audio": {"sampel_seconds": 5}, "target": [{"type": "log"}]})


def test_homeassistant_requires_fields():
    with pytest.raises(ConfigError, match="entity_id"):
        parse(
            {
                "trigger": {"type": "homeassistant", "url": "http://ha", "token": "t"},
                "target": [{"type": "log"}],
            }
        )


def test_lastfm_requires_session_key():
    with pytest.raises(ConfigError, match="session_key"):
        parse({"target": [{"type": "lastfm", "api_key": "k", "api_secret": "s"}]})


def test_duplicate_target_names():
    with pytest.raises(ConfigError, match="unique"):
        parse({"target": [{"type": "log"}, {"type": "log"}]})


def test_interval_shorter_than_sample():
    with pytest.raises(ConfigError, match="interval"):
        parse({"audio": {"sample_seconds": 10, "interval_seconds": 5}, "target": [{"type": "log"}]})
