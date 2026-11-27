"""Fill in songs the microphone missed.

A quiet track can slip past recognition. But if runout heard track 3 of an album
and then track 5, with just the right amount of time between them, track 4 played
too. This module checks the album's real tracklist and song lengths on MusicBrainz
(free and open, no account) and works out what was missed and when it started.

It only fills a gap when the timing adds up, so lifting the needle, skipping a
song or changing records never produces made-up plays.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from runout.models import Scrobble, Track, title_key
from runout.targets import USER_AGENT

log = logging.getLogger(__name__)

MUSICBRAINZ = "https://musicbrainz.org/ws/2"


@dataclass(frozen=True)
class AlbumTrack:
    title: str
    artist: str
    length: float | None  # seconds


@dataclass(frozen=True)
class Heard:
    """A song runout recognised, and when it started."""

    track: Track
    started_at: int


def plan_fill(
    tracklist: list[AlbumTrack],
    before: Heard,
    after: Heard,
    max_missing: int = 3,
    tolerance: float = 20.0,
) -> list[tuple[AlbumTrack, int]] | None:
    """Work out which tracks played between two recognised songs, and when.

    Returns [(track, start time)] for the missed tracks, or None if the album
    and the timing don't clearly agree.
    """
    keys = [title_key(t.title) for t in tracklist]
    a_key, b_key = title_key(before.track.title), title_key(after.track.title)
    try:
        ia = keys.index(a_key)
        ib = keys.index(b_key, ia + 1)
    except ValueError:
        return None
    missing = tracklist[ia + 1 : ib]
    if not missing or len(missing) > max_missing:
        return None
    span = tracklist[ia:ib]  # the song we heard first, plus everything we missed
    if any(t.length is None for t in span):
        return None
    expected = sum(t.length for t in span)
    actual = after.started_at - before.started_at
    # Allow for the needle's run-in gaps between songs and turntable speed drift.
    slack = tolerance + 3 * len(missing) + 0.02 * expected
    if abs(actual - expected) > slack:
        return None
    stretch = actual / expected  # spread any difference evenly across the songs
    out, t = [], before.started_at + span[0].length * stretch
    for track in missing:
        out.append((track, int(round(t))))
        t += track.length * stretch
    return out


def _artist(credit: list[dict[str, Any]] | None) -> str:
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credit or []).strip()


def parse_release(data: dict[str, Any]) -> list[AlbumTrack]:
    """MusicBrainz release JSON -> tracks in playing order, across all discs/sides."""
    album_artist = _artist(data.get("artist-credit"))
    out = []
    for medium in sorted(data.get("media") or [], key=lambda m: m.get("position", 0)):
        for t in sorted(medium.get("tracks") or [], key=lambda t: t.get("position", 0)):
            length = t.get("length") or (t.get("recording") or {}).get("length")
            out.append(
                AlbumTrack(
                    title=t.get("title") or (t.get("recording") or {}).get("title", ""),
                    artist=_artist(t.get("artist-credit")) or album_artist,
                    length=length / 1000 if length else None,
                )
            )
    return out


def release_ids(recordings: list[dict[str, Any]], title: str, min_score: int = 90) -> list[str]:
    """Official releases first, from recording search/lookup results matching the title."""
    official, other = [], []
    want = title_key(title)
    for rec in recordings:
        if int(rec.get("score", 100)) < min_score or title_key(rec.get("title", "")) != want:
            continue
        for rel in rec.get("releases") or []:
            rid = rel.get("id")
            if rid and rid not in official and rid not in other:
                (official if rel.get("status") == "Official" else other).append(rid)
    return official + other


def _lucene(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class MusicBrainz:
    """A polite MusicBrainz client: one request a second, results cached."""

    def __init__(self, url: str = MUSICBRAINZ, min_interval: float = 1.1):
        self.url = url.rstrip("/")
        self.min_interval = min_interval
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._releases: dict[str, list[str]] = {}
        self._tracklists: dict[str, list[AlbumTrack]] = {}

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": USER_AGENT}
            )
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._session.get(
                    f"{self.url}/{path}", params={**params, "fmt": "json"}
                ) as resp:
                    if resp.status == 404:
                        return {}
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            finally:
                self._last = time.monotonic()

    async def releases_for(self, track: Track) -> list[str]:
        key = track.key
        if key in self._releases:
            return self._releases[key]
        ids: list[str] = []
        if track.isrc:
            data = await self._get(f"isrc/{track.isrc}", {"inc": "releases"})
            ids = release_ids(data.get("recordings") or [], track.title)
        if not ids:
            query = f'recording:"{_lucene(track.title)}" AND artist:"{_lucene(track.artist)}"'
            data = await self._get("recording", {"query": query, "limit": "25"})
            ids = release_ids(data.get("recordings") or [], track.title)
        self._releases[key] = ids
        return ids

    async def tracklist(self, release_id: str) -> list[AlbumTrack]:
        if release_id not in self._tracklists:
            data = await self._get(f"release/{release_id}", {"inc": "recordings+artist-credits"})
            self._tracklists[release_id] = parse_release(data)
        return self._tracklists[release_id]

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class GapFiller:
    def __init__(
        self,
        mb: MusicBrainz | None = None,
        max_missing: int = 3,
        tolerance: float = 20.0,
        releases_to_try: int = 4,
    ):
        self.mb = mb or MusicBrainz()
        self.max_missing = max_missing
        self.tolerance = tolerance
        self.releases_to_try = releases_to_try

    async def fill(self, before: Heard, after: Heard) -> list[Scrobble]:
        """Scrobbles for any tracks that played between two recognised songs."""
        a, b = before.track, after.track
        # Cheap check first: only same-album neighbours are worth a lookup.
        if not a.album or not b.album or title_key(a.album) != title_key(b.album):
            return []
        if a.key == b.key or after.started_at <= before.started_at:
            return []
        try:
            a_releases = await self.mb.releases_for(a)
            b_releases = set(await self.mb.releases_for(b)) if a_releases else set()
            shared = [r for r in a_releases if r in b_releases]
            for release_id in shared[: self.releases_to_try]:
                plan = plan_fill(
                    await self.mb.tracklist(release_id),
                    before,
                    after,
                    self.max_missing,
                    self.tolerance,
                )
                if plan:
                    return [
                        Scrobble(
                            Track(t.artist or a.artist, t.title, album=a.album, source="gapfill"),
                            start,
                        )
                        for t, start in plan
                    ]
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            log.warning("couldn't check the album on MusicBrainz (%s); not filling gaps", e)
        return []

    async def close(self) -> None:
        await self.mb.close()
