"""Turn an audio sample into a Track.

* ``shazam`` (default): free, no account, very good on mic recordings of
  vinyl. Uses the reverse-engineered API via the ``shazamio`` package, so
  it can break if Shazam changes things, and it's not an official API.
* ``audd``: AudD.io's paid API (has a free trial). Official and stable.

Adding another (ACRCloud, a local fingerprint DB...) means writing one class
with a ``recognise(wav) -> Track | None`` method and registering it in
``build_recogniser``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import aiohttp

from runout.config import RecogniserConfig
from runout.models import Track

log = logging.getLogger(__name__)


class Recogniser(ABC):
    name = "base"

    @abstractmethod
    async def recognise(self, wav: bytes) -> Track | None: ...

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass


def parse_shazam(result: dict[str, Any]) -> Track | None:
    track = result.get("track")
    if not track or not track.get("title") or not track.get("subtitle"):
        return None
    album = None
    for section in track.get("sections", []):
        for item in section.get("metadata", []) or []:
            if item.get("title", "").lower() == "album":
                album = item.get("text")
                break
        if album:
            break
    offset = None
    matches = result.get("matches") or []
    if matches and isinstance(matches[0].get("offset"), (int, float)):
        offset = float(matches[0]["offset"])
    return Track(
        artist=track["subtitle"],
        title=track["title"],
        album=album,
        isrc=track.get("isrc"),
        offset=offset,
        source="shazam",
        extra={"shazam_key": track.get("key"), "url": track.get("url")},
    )


class ShazamRecogniser(Recogniser):
    name = "shazam"

    def __init__(self, cfg: RecogniserConfig):
        from aiohttp_retry import ExponentialRetry
        from shazamio import Shazam
        from shazamio.client import HTTPClient

        # shazamio's default is 20 retries with up to 60s backoff, which would stall the
        # loop for minutes if the internet drops. A missed sample is cheap; retry briefly.
        self._http = HTTPClient(
            retry_options=ExponentialRetry(
                attempts=3, max_timeout=5, statuses={500, 502, 503, 504, 429}
            )
        )
        self._shazam = Shazam(http_client=self._http)
        self._max_skew = cfg.max_skew

    async def recognise(self, wav: bytes) -> Track | None:
        kwargs = {"max_skew": self._max_skew} if self._max_skew else {}
        try:
            result = await self._shazam.recognize(wav, **kwargs)
        except Exception as e:  # shazamio raises a grab-bag of errors
            log.warning("shazam lookup failed: %s", e)
            return None
        return parse_shazam(result)

    async def close(self) -> None:
        await self._http.close()


def parse_audd(result: dict[str, Any]) -> Track | None:
    if result.get("status") != "success" or not result.get("result"):
        return None
    r = result["result"]
    if not r.get("artist") or not r.get("title"):
        return None
    offset = None
    timecode = r.get("timecode")  # "MM:SS"
    if isinstance(timecode, str) and ":" in timecode:
        try:
            m, s = timecode.split(":", 1)
            offset = int(m) * 60 + float(s)
        except ValueError:
            offset = None
    return Track(
        artist=r["artist"],
        title=r["title"],
        album=r.get("album"),
        offset=offset,
        source="audd",
        extra={"url": r.get("song_link")},
    )


class AudDRecogniser(Recogniser):
    name = "audd"
    endpoint = "https://api.audd.io/"

    def __init__(self, cfg: RecogniserConfig):
        self.token = cfg.token
        self._session: aiohttp.ClientSession | None = None

    async def recognise(self, wav: bytes) -> Track | None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        form = aiohttp.FormData()
        form.add_field("api_token", self.token)
        form.add_field("file", wav, filename="sample.wav", content_type="audio/wav")
        try:
            async with self._session.post(self.endpoint, data=form) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("audd lookup failed: %s", e)
            return None
        if data.get("status") == "error":
            log.warning("audd error: %s", data.get("error"))
        return parse_audd(data)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def build_recogniser(cfg: RecogniserConfig) -> Recogniser:
    if cfg.type == "audd":
        return AudDRecogniser(cfg)
    return ShazamRecogniser(cfg)
