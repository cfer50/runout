"""Command line interface.

runout run                 run the daemon
runout check               validate config, read the trigger, measure mic level
runout identify            record one sample (or --file) and show the match
runout lastfm-auth         get a last.fm session key
runout queue [--flush]     show or retry queued scrobbles
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import aiohttp

from runout import __version__
from runout.config import Config, ConfigError, load

DEFAULT_PATHS = [
    Path("~/.config/runout/config.toml").expanduser(),
    Path("/etc/runout/config.toml"),
]


def find_config(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if env := os.environ.get("RUNOUT_CONFIG"):
        return Path(env)
    for p in DEFAULT_PATHS:
        if p.exists():
            return p
    raise ConfigError(
        "no config found. Copy config.example.toml to ~/.config/runout/config.toml "
        "or pass --config."
    )


def _load(args) -> Config:
    return load(find_config(args.config))


def _meter(rms: float, width: int = 30) -> str:
    filled = min(width, int(rms / 0.1 * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# -- commands --------------------------------------------------------------------


def cmd_run(args) -> int:
    from runout.daemon import run_forever

    asyncio.run(run_forever(_load(args)))
    return 0


def cmd_check(args) -> int:
    from runout.audio import build_recorder
    from runout.triggers import build_trigger

    cfg = _load(args)
    print(f"config ok: {find_config(args.config)}")
    print(f"  targets: {', '.join(f'{t.name} ({t.type})' for t in cfg.targets)}")

    async def go() -> int:
        trig = build_trigger(cfg.trigger)
        reading = await trig.trigger.poll()
        await trig.trigger.close()
        label = {True: "ON", False: "off", None: "UNREADABLE"}[reading]
        print(f"trigger ({cfg.trigger.type}): {label}")

        rec = build_recorder(cfg.audio)
        print(f"recording {cfg.audio.sample_seconds:g}s from {cfg.audio.device!r}...")
        try:
            sample = await rec.sample()
        except RuntimeError as e:
            print(f"mic: FAILED - {e}")
            print("  run `arecord -l` and set [audio] device to plughw:CARD=<name>,DEV=0")
            return 1
        verdict = "silent" if sample.is_silent(cfg.audio.silence_rms) else "sound detected"
        print(
            f"mic level: {_meter(sample.rms)} rms={sample.rms:.4f} "
            f"(silence below {cfg.audio.silence_rms}) -> {verdict}"
        )
        return 0 if reading is not None else 1

    return asyncio.run(go())


def cmd_identify(args) -> int:
    from runout.recognisers import build_recogniser

    cfg = _load(args)

    async def go() -> int:
        if args.file:
            wav = Path(args.file).read_bytes()
        else:
            from runout.audio import build_recorder

            print(f"recording {cfg.audio.sample_seconds:g}s...")
            try:
                sample = await build_recorder(cfg.audio).sample()
            except RuntimeError as e:
                print(f"mic: FAILED - {e}")
                return 1
            print(f"rms={sample.rms:.4f}")
            wav = sample.wav
        rec = build_recogniser(cfg.recogniser)
        track = await rec.recognise(wav)
        await rec.close()
        if not track:
            print("no match")
            return 1
        print(f"{track.artist} - {track.title}")
        if track.album:
            print(f"  album:  {track.album}")
        if track.offset is not None:
            print(f"  offset: {track.offset:.1f}s into the track")
        return 0

    return asyncio.run(go())


def cmd_lastfm_auth(args) -> int:
    from runout.targets import LASTFM_API, TargetError, lastfm_call

    api_key = args.api_key or input("last.fm API key: ").strip()
    secret = args.api_secret or input("last.fm API secret: ").strip()

    async def go() -> int:
        async with aiohttp.ClientSession() as s:
            try:
                data = await lastfm_call(s, {"method": "auth.getToken", "api_key": api_key}, secret)
                token = data["token"]
                print("\nOpen this URL, log in and click 'Yes, allow access':\n")
                print(f"  https://www.last.fm/api/auth/?api_key={api_key}&token={token}\n")
                input("Press Enter once you've allowed access... ")
                data = await lastfm_call(
                    s,
                    {"method": "auth.getSession", "api_key": api_key, "token": token},
                    secret,
                    LASTFM_API,
                )
            except TargetError as e:
                print(f"failed: {e}", file=sys.stderr)
                return 1
        sk = data["session"]["key"]
        print(f"\nAuthorised as {data['session']['name']}. Add this to your config:\n")
        print('[[target]]\ntype = "lastfm"')
        print(f'api_key = "{api_key}"\napi_secret = "{secret}"\nsession_key = "{sk}"')
        return 0

    return asyncio.run(go())


def cmd_queue(args) -> int:
    from runout.queue import ScrobbleQueue
    from runout.targets import build_target

    cfg = _load(args)
    q = ScrobbleQueue(cfg.queue_path)
    items = q.items()
    if not items:
        print("queue is empty")
        return 0
    for _id, target, track, ts, attempts, err in items:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        print(
            f"{when}  {target:<12} {track.artist} - {track.title}  "
            f"(attempts {attempts}{', ' + err if err else ''})"
        )
    if args.flush:
        targets = [build_target(t, cfg.media_player) for t in cfg.targets]

        async def go() -> int:
            n = await q.flush(targets)
            for t in targets:
                await t.close()
            return n

        print(f"delivered {asyncio.run(go())} of {len(items)}")
    q.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runout", description="Scrobble your records.")
    p.add_argument("--version", action="version", version=f"runout {__version__}")
    p.add_argument("-c", "--config", help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the daemon").set_defaults(func=cmd_run)
    sub.add_parser("check", help="check config, trigger and mic").set_defaults(func=cmd_check)

    ident = sub.add_parser("identify", help="identify one sample")
    ident.add_argument("--file", help="identify a WAV/MP3 file instead of recording")
    ident.set_defaults(func=cmd_identify)

    auth = sub.add_parser("lastfm-auth", help="get a last.fm session key")
    auth.add_argument("--api-key")
    auth.add_argument("--api-secret")
    auth.set_defaults(func=cmd_lastfm_auth, needs_config=False)

    q = sub.add_parser("queue", help="show queued scrobbles")
    q.add_argument("--flush", action="store_true", help="retry them now")
    q.set_defaults(func=cmd_queue)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        for noisy in ("aiohttp", "shazamio", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
