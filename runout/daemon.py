"""The main loop: trigger -> sample -> recognise -> session -> targets."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal
import time
from collections.abc import Awaitable, Callable

from runout.audio import Recorder, build_recorder
from runout.config import Config
from runout.dedupe import LastfmHistory
from runout.models import NowPlaying, Scrobble
from runout.queue import ScrobbleQueue
from runout.recognisers import Recogniser, build_recogniser
from runout.session import Event, Session
from runout.targets import Target, TargetError, build_target
from runout.triggers import Debounced, build_trigger

log = logging.getLogger(__name__)

QUEUE_FLUSH_SECONDS = 300


class Daemon:
    def __init__(
        self,
        cfg: Config,
        *,
        trigger: Debounced,
        recorder: Recorder,
        recogniser: Recogniser,
        targets: list[Target],
        queue: ScrobbleQueue,
        history: LastfmHistory | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.cfg = cfg
        self.trigger = trigger
        self.recorder = recorder
        self.recogniser = recogniser
        self.targets = targets
        self.queue = queue
        self.history = history
        # Scrobbles waiting out the dedupe delay: (due_time, event)
        self.deferred: list[tuple[float, Scrobble]] = []
        self.clock = clock
        self.sleep = sleep
        session_cfg = cfg.session
        if history is not None:
            # With dedupe on, never scrobble mid-song (the 4-minute rule): a streaming app
            # usually logs a song when it ends, so checking before then would be too early
            # for long tracks. Scrobble when the song finishes, then wait and check.
            session_cfg = dataclasses.replace(session_cfg, scrobble_after_seconds=float("inf"))
        self.session = Session(session_cfg, cfg.audio.sample_seconds)
        self.listening = False
        self._last_flush = 0.0
        self._stop = asyncio.Event()

    @classmethod
    def from_config(cls, cfg: Config) -> Daemon:
        return cls(
            cfg,
            trigger=build_trigger(cfg.trigger),
            recorder=build_recorder(cfg.audio),
            recogniser=build_recogniser(cfg.recogniser),
            targets=[build_target(t, cfg.media_player) for t in cfg.targets],
            queue=ScrobbleQueue(cfg.queue_path),
            history=(
                LastfmHistory(cfg.dedupe.lastfm_user, cfg.dedupe.api_key, cfg.dedupe.window_seconds)
                if cfg.dedupe.lastfm_user
                else None
            ),
        )

    def stop(self) -> None:
        self._stop.set()

    async def handle(self, events: list[Event]) -> None:
        """Send now-playing straight away; hold scrobbles back if dedupe is on."""
        for event in events:
            if isinstance(event, Scrobble) and self.history is not None:
                due = self.clock() + self.cfg.dedupe.delay_seconds
                self.deferred.append((due, event))
            else:
                await self.dispatch([event])

    async def release_deferred(self, everything: bool = False) -> None:
        """Check held scrobbles against last.fm once their wait is over."""
        now = self.clock()
        ready = [e for due, e in self.deferred if everything or due <= now]
        self.deferred = [(due, e) for due, e in self.deferred if not (everything or due <= now)]
        for event in ready:
            if await self.history.already_scrobbled(event.track, event.listened_at):
                log.info(
                    "already on last.fm from another app, skipping: %s - %s",
                    event.track.artist,
                    event.track.title,
                )
                continue
            await self.dispatch([event])

    async def dispatch(self, events: list[Event]) -> None:
        for event in events:
            if isinstance(event, Scrobble) and self.queue.seen_recently(
                event.track, event.listened_at
            ):
                log.info("skipping duplicate: %s - %s", event.track.artist, event.track.title)
                continue
            failed: list[str] = []
            for target in self.targets:
                try:
                    if isinstance(event, NowPlaying):
                        await target.now_playing(event.track)
                    elif isinstance(event, Scrobble):
                        await target.scrobble(event.track, event.listened_at)
                except TargetError as e:
                    failed.append(target.name)
                    if isinstance(event, Scrobble) and e.retryable:
                        log.warning("%s; queued for retry", e)
                        self.queue.put(target.name, event.track, event.listened_at, str(e))
                    else:
                        log.warning("%s", e)
            note = f" (failed for {', '.join(failed)})" if failed else ""
            if isinstance(event, Scrobble):
                log.info("scrobbled: %s - %s%s", event.track.artist, event.track.title, note)
            else:
                log.info("now playing: %s - %s", event.track.artist, event.track.title)

    async def tick(self) -> float:
        """One iteration. Returns how long to wait before the next one."""
        active = await self.trigger.poll()

        if not active:
            if self.listening:
                log.info("record player off; stopped listening")
                self.listening = False
                await self.handle(self.session.end())
            await self.release_deferred()
            await self._maybe_flush()
            return self.cfg.trigger.poll_seconds

        if not self.listening:
            log.info("record player on; listening")
            self.listening = True

        started = self.clock()
        try:
            sample = await self.recorder.sample()
        except RuntimeError as e:
            if not self._stop.is_set():  # arecord gets killed on shutdown; that's not an error
                log.error("couldn't record: %s", e)
            return self.cfg.audio.interval_seconds

        if sample.is_silent(self.cfg.audio.silence_rms):
            log.debug("silence (rms %.4f)", sample.rms)
            track = None
        else:
            track = await self.recogniser.recognise(sample.wav)
            if track:
                log.debug("matched %s - %s (rms %.4f)", track.artist, track.title, sample.rms)
            else:
                log.debug("no match (rms %.4f)", sample.rms)

        await self.handle(self.session.observe(track, started))
        await self.release_deferred()
        await self._maybe_flush()
        elapsed = self.clock() - started
        return max(0.0, self.cfg.audio.interval_seconds - elapsed)

    async def _maybe_flush(self) -> None:
        now = self.clock()
        if now - self._last_flush < QUEUE_FLUSH_SECONDS:
            return
        self._last_flush = now
        if self.queue.count():
            await self.queue.flush(self.targets)

    async def run(self) -> None:
        log.info(
            "runout started: trigger=%s, recogniser=%s, targets=%s",
            self.cfg.trigger.type,
            self.cfg.recogniser.type,
            ", ".join(t.name for t in self.targets),
        )
        try:
            while not self._stop.is_set():
                wait = await self.tick()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except TimeoutError:
                    pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        # Don't lose a song that was nearly finished when we got stopped.
        await self.handle(self.session.end())
        if self.deferred:
            await self.release_deferred(everything=True)
        closeables = [self.trigger.trigger, self.recogniser, *self.targets]
        if self.history is not None:
            closeables.append(self.history)
        for closeable in closeables:
            try:
                await closeable.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self.queue.close()
        log.info("runout stopped")


async def run_forever(cfg: Config) -> None:
    daemon = Daemon.from_config(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await daemon.run()
