"""Where scrobbles go.

* ``listenbrainz``: anything that speaks the ListenBrainz API, which covers
  ListenBrainz itself, Maloja (``https://your-maloja/apis/listenbrainz``)
  and Koito (``https://your-koito/apis/listenbrainz``).
* ``lastfm``: last.fm's scrobbling API. Run ``runout lastfm-auth`` once
  to get a session key.
* ``log``: just prints. Handy while setting things up.

You can list as many targets as you like; each scrobble goes to all of them.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Any

import aiohttp

from runout import __version__
from runout.config import TargetConfig
from runout.models import Track

log = logging.getLogger(__name__)

USER_AGENT = f"runout/{__version__} (+https://github.com/cfer50/runout)"


class TargetError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Target(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def now_playing(self, track: Track) -> None: ...

    @abstractmethod
    async def scrobble(self, track: Track, listened_at: int) -> None: ...

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass


class _HttpTarget(Target):
    def __init__(self, name: str, session: aiohttp.ClientSession | None = None):
        super().__init__(name)
        self._session = session
        self._own_session = session is None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": USER_AGENT}
            )
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()


# -- ListenBrainz-compatible -------------------------------------------------------


def listenbrainz_payload(
    track: Track, listened_at: int | None, media_player: str = "Turntable"
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "media_player": media_player,
        "submission_client": "runout",
        "submission_client_version": __version__,
    }
    if track.isrc:
        info["isrc"] = track.isrc
    meta: dict[str, Any] = {
        "artist_name": track.artist,
        "track_name": track.title,
        "additional_info": info,
    }
    if track.album:
        meta["release_name"] = track.album
    listen: dict[str, Any] = {"track_metadata": meta}
    if listened_at is not None:
        listen["listened_at"] = listened_at
    return {
        "listen_type": "single" if listened_at is not None else "playing_now",
        "payload": [listen],
    }


class ListenBrainzTarget(_HttpTarget):
    def __init__(
        self,
        name: str,
        url: str,
        token: str,
        media_player: str = "Turntable",
        session: aiohttp.ClientSession | None = None,
    ):
        super().__init__(name, session)
        self.endpoint = url.rstrip("/") + "/1/submit-listens"
        self.token = token
        self.media_player = media_player

    async def _submit(self, payload: dict[str, Any]) -> None:
        headers = {"Authorization": f"Token {self.token}"}
        try:
            async with self.session.post(self.endpoint, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    return
                body = (await resp.text())[:300]
                # Auth errors are retryable too: they get fixed by correcting the token, and
                # queueing means the scrobbles go through once that's done instead of vanishing.
                retryable = resp.status >= 500 or resp.status in (401, 403, 429)
                raise TargetError(f"{self.name}: HTTP {resp.status}: {body}", retryable)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise TargetError(f"{self.name}: {e}") from e

    async def now_playing(self, track: Track) -> None:
        await self._submit(listenbrainz_payload(track, None, self.media_player))

    async def scrobble(self, track: Track, listened_at: int) -> None:
        await self._submit(listenbrainz_payload(track, listened_at, self.media_player))


# -- last.fm -----------------------------------------------------------------------

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
# last.fm error codes worth keeping in the queue: service offline/unavailable, rate limits,
# and auth/session problems (fixable by re-running lastfm-auth). Bad params won't fix themselves.
_LASTFM_RETRYABLE = {4, 9, 11, 14, 16, 26, 29}  # incl. auth/session problems


def lastfm_sign(params: dict[str, str], secret: str) -> str:
    """last.fm's api_sig: md5 of sorted key+value pairs followed by the secret."""
    raw = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("format", "callback"))
    return hashlib.md5((raw + secret).encode("utf-8")).hexdigest()


async def lastfm_call(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    secret: str,
    url: str = LASTFM_API,
) -> dict[str, Any]:
    params = {k: v for k, v in params.items() if v is not None}
    params["api_sig"] = lastfm_sign(params, secret)
    params["format"] = "json"
    try:
        async with session.post(url, data=params) as resp:
            data = await resp.json(content_type=None)
            status = resp.status
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise TargetError(f"last.fm: {e}") from e
    if isinstance(data, dict) and "error" in data:
        code = int(data["error"])
        raise TargetError(f"last.fm error {code}: {data.get('message')}", code in _LASTFM_RETRYABLE)
    if status != 200:
        raise TargetError(f"last.fm: HTTP {status}", status >= 500)
    return data


class LastfmTarget(_HttpTarget):
    def __init__(
        self,
        name: str,
        api_key: str,
        api_secret: str,
        session_key: str,
        url: str = LASTFM_API,
        session: aiohttp.ClientSession | None = None,
    ):
        super().__init__(name, session)
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_key = session_key
        self.url = url

    def _params(self, method: str, track: Track) -> dict[str, str]:
        p = {
            "method": method,
            "artist": track.artist,
            "track": track.title,
            "api_key": self.api_key,
            "sk": self.session_key,
        }
        if track.album:
            p["album"] = track.album
        return p

    async def now_playing(self, track: Track) -> None:
        await lastfm_call(
            self.session, self._params("track.updateNowPlaying", track), self.api_secret, self.url
        )

    async def scrobble(self, track: Track, listened_at: int) -> None:
        p = self._params("track.scrobble", track)
        p["timestamp"] = str(listened_at)
        p["chosenByUser"] = "1"
        data = await lastfm_call(self.session, p, self.api_secret, self.url)
        ignored = (
            data.get("scrobbles", {}).get("@attr", {}).get("ignored")
            if isinstance(data, dict)
            else None
        )
        if ignored and str(ignored) != "0":
            log.warning("last.fm ignored the scrobble of %s - %s", track.artist, track.title)


# -- log ---------------------------------------------------------------------------


class LogTarget(Target):
    async def now_playing(self, track: Track) -> None:
        log.info("[%s] now playing: %s - %s", self.name, track.artist, track.title)

    async def scrobble(self, track: Track, listened_at: int) -> None:
        album = f" ({track.album})" if track.album else ""
        log.info(
            "[%s] scrobble: %s - %s%s @ %d",
            self.name,
            track.artist,
            track.title,
            album,
            listened_at,
        )


def build_target(cfg: TargetConfig, media_player: str = "Turntable") -> Target:
    o = cfg.options
    if cfg.type == "listenbrainz":
        return ListenBrainzTarget(cfg.name, o["url"], o["token"], media_player=media_player)
    if cfg.type == "lastfm":
        return LastfmTarget(cfg.name, o["api_key"], o["api_secret"], o["session_key"])
    return LogTarget(cfg.name)
