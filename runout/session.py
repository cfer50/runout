"""Turn a stream of noisy recognitions into clean scrobbles.

This is pure logic with no I/O, so it's easy to test and to tweak.

Every few seconds the daemon hands us either a Track (a match), or None
(silence, or no match). We decide when a song has *really* started and when
it has been listened to enough to count.

Rules, which are close to last.fm's own:

* A song only counts once it's matched ``min_confirmations`` times in a row.
  One stray match during a quiet intro won't create a fake scrobble.
* A song that's interrupted by a single odd match carries on; the odd match
  has to confirm itself before it replaces the current song.
* The song is scrobbled once it's been heard for ``scrobble_after_seconds``
  (last.fm uses 4 minutes), or when it ends, if it was heard for at least
  ``min_listen_seconds``.
* "Ends" means a different song confirmed, silence or no matches for
  ``idle_timeout_seconds``, or the trigger turned off (``end()``).
* The scrobble timestamp is when the song *started*, worked out from the
  recogniser's offset into the track where it reports one.
"""

from __future__ import annotations

from dataclasses import dataclass

from runout.config import SessionConfig
from runout.models import NowPlaying, Scrobble, Track

Event = NowPlaying | Scrobble

# Ignore recogniser offsets bigger than this; they're more likely wrong than a 30-minute song.
_MAX_TRUSTED_OFFSET = 30 * 60


@dataclass
class _Play:
    track: Track
    first_heard: float
    last_heard_end: float
    started_at: float
    hits: int = 1
    now_playing_sent: bool = False
    scrobbled: bool = False

    @classmethod
    def begin(cls, track: Track, at: float, seconds: float) -> _Play:
        started = at
        if track.offset is not None and 0 <= track.offset <= _MAX_TRUSTED_OFFSET:
            started = at - track.offset
        return cls(track=track, first_heard=at, last_heard_end=at + seconds, started_at=started)

    def heard_again(self, track: Track, at: float, seconds: float) -> None:
        self.hits += 1
        self.last_heard_end = max(self.last_heard_end, at + seconds)
        # Prefer metadata that includes an album.
        if self.track.album is None and track.album:
            self.track = track

    @property
    def listened(self) -> float:
        return self.last_heard_end - self.first_heard


class Session:
    def __init__(self, cfg: SessionConfig, sample_seconds: float):
        self.cfg = cfg
        self.sample_seconds = sample_seconds
        self.current: _Play | None = None
        self.pending: _Play | None = None

    # -- public API -------------------------------------------------------

    def observe(self, track: Track | None, at: float) -> list[Event]:
        """Feed one sample's result. ``at`` is when the sample started (unix time)."""
        events: list[Event] = []
        seconds = self.sample_seconds

        if track is None:
            if self.current and at - self.current.last_heard_end > self.cfg.idle_timeout_seconds:
                events += self._finish_current()
            if self.pending and at - self.pending.last_heard_end > self.cfg.idle_timeout_seconds:
                self.pending = None
            return events

        if self.current and track.key == self.current.track.key:
            self.current.heard_again(track, at, seconds)
            self.pending = None
        else:
            if self.pending and track.key == self.pending.track.key:
                self.pending.heard_again(track, at, seconds)
            else:
                self.pending = _Play.begin(track, at, seconds)
            if self.pending.hits >= self.cfg.min_confirmations:
                events += self._finish_current()
                self.current, self.pending = self.pending, None

        cur = self.current
        if cur is not None:
            if not cur.now_playing_sent:
                cur.now_playing_sent = True
                events.append(NowPlaying(cur.track, int(cur.started_at)))
            if not cur.scrobbled and cur.listened >= self.cfg.scrobble_after_seconds:
                cur.scrobbled = True
                events.append(Scrobble(cur.track, int(cur.started_at)))
        return events

    def end(self) -> list[Event]:
        """The record stopped (trigger off, or shutting down)."""
        self.pending = None
        return self._finish_current()

    # -- internals ----------------------------------------------------------

    def _finish_current(self) -> list[Event]:
        cur, self.current = self.current, None
        if cur is None or cur.scrobbled:
            return []
        if cur.listened >= self.cfg.min_listen_seconds:
            return [Scrobble(cur.track, int(cur.started_at))]
        return []
