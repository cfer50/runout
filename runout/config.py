"""Load and validate runout.toml.

Any string value written as ``"env:NAME"`` is read from the environment
variable NAME, so secrets can live in a systemd EnvironmentFile instead of
the config file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def _resolve(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        if name not in os.environ:
            raise ConfigError(f"environment variable {name!r} is not set")
        return os.environ[name]
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


@dataclass
class TriggerConfig:
    type: str = "always"
    # homeassistant
    url: str | None = None
    token: str | None = None
    entity_id: str | None = None
    on_above: float | None = None  # numeric sensors: "on" when state > this
    on_states: list[str] = field(default_factory=lambda: ["on", "playing"])
    # command
    command: str | None = None
    # shared
    poll_seconds: float = 5.0
    on_debounce: int = 2  # consecutive "on" polls before we start listening
    off_debounce: int = 3  # consecutive "off" polls before we stop


@dataclass
class AudioConfig:
    backend: str = "arecord"  # "arecord" or "sounddevice"
    device: str = "auto"  # "auto" = first microphone found, or an ALSA name
    sample_rate: int = 44100
    channels: int = 1
    sample_seconds: float = 10.0
    interval_seconds: float = 20.0  # time between the starts of two samples
    silence_rms: float = 0.002  # RMS as a fraction of full scale; below this is "silence"
    trim_start_seconds: float = 0.5  # discard the mic's start-up click/pop


@dataclass
class RecogniserConfig:
    type: str = "shazam"  # "shazam" or "audd"
    token: str | None = None  # audd api token
    max_skew: float | None = None  # shazam: reject matches skewed more than this


@dataclass
class SessionConfig:
    min_confirmations: int = 2
    min_listen_seconds: float = 30.0
    scrobble_after_seconds: float = 240.0
    idle_timeout_seconds: float = 90.0


@dataclass
class DedupeConfig:
    lastfm_user: str | None = None  # set this to turn dedupe on
    api_key: str | None = None  # defaults to the lastfm target's api_key
    delay_seconds: float = 120.0  # wait this long after a song before checking
    window_seconds: int = 600  # same song within this many seconds = already scrobbled


@dataclass
class GapfillConfig:
    enabled: bool = True  # fill in album tracks the mic missed, using MusicBrainz
    max_missing: int = 3  # never fill more than this many tracks in one gap
    tolerance_seconds: float = 20.0  # how closely the timing must match the tracklist


@dataclass
class TargetConfig:
    type: str
    name: str
    options: dict[str, Any]


@dataclass
class Config:
    trigger: TriggerConfig
    audio: AudioConfig
    recogniser: RecogniserConfig
    session: SessionConfig
    targets: list[TargetConfig]
    queue_path: Path
    media_player: str = "Turntable"
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    gapfill: GapfillConfig = field(default_factory=GapfillConfig)


_TRIGGERS = {"always", "homeassistant", "command"}
_BACKENDS = {"arecord", "sounddevice"}
_RECOGNISERS = {"shazam", "audd"}
_TARGETS = {"listenbrainz", "lastfm", "log"}


def _build(cls, data: dict[str, Any], section: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError as e:  # pragma: no cover - defensive
        raise ConfigError(f"[{section}] {e}") from e


def parse(raw: dict[str, Any]) -> Config:
    raw = _resolve(raw)

    trigger = _build(TriggerConfig, raw.get("trigger", {}), "trigger")
    if trigger.type not in _TRIGGERS:
        raise ConfigError(f"[trigger] type must be one of {sorted(_TRIGGERS)}")
    if trigger.type == "homeassistant":
        for key in ("url", "token", "entity_id"):
            if not getattr(trigger, key):
                raise ConfigError(f"[trigger] homeassistant needs {key!r}")
    if trigger.type == "command" and not trigger.command:
        raise ConfigError("[trigger] command trigger needs 'command'")

    audio = _build(AudioConfig, raw.get("audio", {}), "audio")
    if audio.backend not in _BACKENDS:
        raise ConfigError(f"[audio] backend must be one of {sorted(_BACKENDS)}")
    if audio.interval_seconds < audio.sample_seconds:
        raise ConfigError("[audio] interval_seconds must be >= sample_seconds")

    recogniser = _build(RecogniserConfig, raw.get("recogniser", {}), "recogniser")
    if recogniser.type not in _RECOGNISERS:
        raise ConfigError(f"[recogniser] type must be one of {sorted(_RECOGNISERS)}")
    if recogniser.type == "audd" and not recogniser.token:
        raise ConfigError("[recogniser] audd needs 'token'")

    session = _build(SessionConfig, raw.get("session", {}), "session")
    if session.min_confirmations < 1:
        raise ConfigError("[session] min_confirmations must be at least 1")

    targets: list[TargetConfig] = []
    for i, t in enumerate(raw.get("target", [])):
        t = dict(t)
        ttype = t.pop("type", None)
        if ttype not in _TARGETS:
            raise ConfigError(f"[[target]] #{i + 1}: type must be one of {sorted(_TARGETS)}")
        name = t.pop("name", ttype)
        required = {
            "listenbrainz": ("url", "token"),
            "lastfm": ("api_key", "api_secret", "session_key"),
            "log": (),
        }[ttype]
        missing = [k for k in required if not t.get(k)]
        if missing:
            raise ConfigError(f"[[target]] {name!r} is missing {', '.join(missing)}")
        targets.append(TargetConfig(type=ttype, name=name, options=t))
    if not targets:
        raise ConfigError('add at least one [[target]] (use type = "log" to just print)')
    names = [t.name for t in targets]
    if len(set(names)) != len(names):
        raise ConfigError("[[target]] names must be unique")

    dedupe = _build(DedupeConfig, raw.get("dedupe", {}), "dedupe")
    if dedupe.lastfm_user and not dedupe.api_key:
        lastfm = next((t for t in targets if t.type == "lastfm"), None)
        if lastfm is None:
            raise ConfigError("[dedupe] needs api_key (or a lastfm [[target]] to borrow it from)")
        dedupe.api_key = lastfm.options["api_key"]

    gapfill = _build(GapfillConfig, raw.get("gapfill", {}), "gapfill")
    if gapfill.max_missing < 1:
        raise ConfigError("[gapfill] max_missing must be at least 1")

    general = raw.get("general", {})
    queue_path = Path(general.get("queue_path", "~/.local/state/runout/queue.db")).expanduser()

    return Config(
        trigger=trigger,
        audio=audio,
        recogniser=recogniser,
        session=session,
        targets=targets,
        queue_path=queue_path,
        media_player=general.get("media_player", "Turntable"),
        dedupe=dedupe,
        gapfill=gapfill,
    )


def load(path: str | Path) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
    return parse(raw)
