"""Skip songs another app already scrobbled.

If you stream (Deezer, Spotify...) while the turntable has power, runout hears it
through the mic and would scrobble it a second time. With ``[dedupe]`` set, runout
waits a little after each song, then looks at your last.fm history: if the same
song is already there at about the same time, someone else got it first and runout
skips it.

This only reads your public listening history, so it needs your username and an
API key but no login.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from runout.models import Track
from runout.targets import LASTFM_API, USER_AGENT

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PastScrobble:
    key: str
    listened_at: int


def parse_recent_tracks(data: dict[str, Any]) -> list[PastScrobble]:
    """Pull (track key, timestamp) out of a user.getRecentTracks response.

    The currently-playing entry has no date and is ignored: it could be runout's own
    "now playing", so it proves nothing.
    """
    tracks = (data.get("recenttracks") or {}).get("track") or []
    if isinstance(tracks, dict):  # last.fm returns a bare object when there's only one
        tracks = [tracks]
    out = []
    for t in tracks:
        date = t.get("date") or {}
        uts = date.get("uts")
        artist = t.get("artist") or {}
        artist_name = (
            artist.get("#text") or artist.get("name") if isinstance(artist, dict) else None
        )
        if not uts or not artist_name or not t.get("name"):
            continue
        try:
            out.append(PastScrobble(Track(artist_name, t["name"]).key, int(uts)))
        except (TypeError, ValueError):
            continue
    return out


def is_duplicate(track: Track, listened_at: int, history: list[PastScrobble], window: int) -> bool:
    return any(p.key == track.key and abs(p.listened_at - listened_at) <= window for p in history)


class LastfmHistory:
    def __init__(
        self,
        user: str,
        api_key: str,
        window_seconds: int = 600,
        url: str = LASTFM_API,
        session: aiohttp.ClientSession | None = None,
    ):
        self.user = user
        self.api_key = api_key
        self.window = window_seconds
        self.url = url
        self._session = session
        self._own_session = session is None

    async def _fetch(self, since: int) -> list[PastScrobble]:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15), headers={"User-Agent": USER_AGENT}
            )
        params = {
            "method": "user.getRecentTracks",
            "user": self.user,
            "api_key": self.api_key,
            "from": str(since),
            "limit": "50",
            "format": "json",
        }
        async with self._session.get(self.url, params=params) as resp:
            data = await resp.json(content_type=None)
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"last.fm error {data['error']}: {data.get('message')}")
        return parse_recent_tracks(data)

    async def already_scrobbled(self, track: Track, listened_at: int) -> bool:
        """True if last.fm already has this song near this time.

        If last.fm can't be reached we say False and scrobble anyway: a rare double
        beats a lost listen.
        """
        try:
            history = await self._fetch(listened_at - self.window)
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as e:
            log.warning("couldn't check last.fm history (%s); scrobbling anyway", e)
            return False
        return is_duplicate(track, listened_at, history, self.window)

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
