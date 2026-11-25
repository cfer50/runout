"""End-to-end: fake trigger, fake mic, fake recogniser, real session/queue/daemon."""

import numpy as np

from runout.audio import Sample, pcm_to_wav, rms_of_wav
from runout.config import parse
from runout.daemon import Daemon
from runout.models import Track
from runout.queue import ScrobbleQueue
from runout.targets import Target, TargetError
from runout.triggers import Debounced

A = Track("Ministry", "Jesus Built My Hotrod", offset=5.0)
B = Track("Jimmy Eat World", "The Middle")


class FakeTrigger:
    def __init__(self, readings):
        self.readings = list(readings)

    async def poll(self):
        return self.readings.pop(0) if self.readings else False

    async def close(self):
        pass


class FakeRecorder:
    def __init__(self, levels):
        self.levels = list(levels)

    async def sample(self, seconds=None):
        return Sample(wav=b"", seconds=10, rms=self.levels.pop(0) if self.levels else 0.0)


class FakeRecogniser:
    def __init__(self, results):
        self.results = list(results)

    async def recognise(self, wav):
        return self.results.pop(0) if self.results else None

    async def close(self):
        pass


class RecordingTarget(Target):
    def __init__(self, name="rec", fail_times=0):
        super().__init__(name)
        self.now = []
        self.scrobbles = []
        self.fail_times = fail_times

    async def now_playing(self, track):
        self.now.append(track)

    async def scrobble(self, track, listened_at):
        if self.fail_times:
            self.fail_times -= 1
            raise TargetError("offline", retryable=True)
        self.scrobbles.append((track, listened_at))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_daemon(readings, levels, results, target, queue):
    cfg = parse({"target": [{"type": "log"}], "trigger": {"type": "command", "command": "x"}})
    clock = Clock()
    d = Daemon(
        cfg,
        trigger=Debounced(FakeTrigger(readings), on_after=1, off_after=1),
        recorder=FakeRecorder(levels),
        recogniser=FakeRecogniser(results),
        targets=[target],
        queue=queue,
        clock=clock,
    )
    return d, clock


async def run_ticks(d, clock, n):
    for _ in range(n):
        await d.tick()
        clock.t += 20


async def test_side_of_a_record():
    target = RecordingTarget()
    q = ScrobbleQueue(":memory:")
    #           A plays for 8 samples, gap of silence, B for 6, then the turntable switches off.
    results = [A] * 8 + [B] * 6
    levels = [0.05] * 8 + [0.0] + [0.05] * 6
    readings = [True] * 15 + [False]
    d, clock = make_daemon(readings, levels, results[:8] + [None] + results[8:], target, q)
    await run_ticks(d, clock, 16)
    assert [t.title for t, _ in target.scrobbles] == ["Jesus Built My Hotrod", "The Middle"]
    assert [t.title for t in target.now] == ["Jesus Built My Hotrod", "The Middle"]
    # first scrobble timestamp = first sample time minus the 5s offset
    assert target.scrobbles[0][1] == 1_000_000 - 5


async def test_offline_target_queues_and_retries():
    target = RecordingTarget(fail_times=1)
    q = ScrobbleQueue(":memory:")
    d, clock = make_daemon([True] * 4 + [False], [0.05] * 4, [A] * 4, target, q)
    d._last_flush = clock.t  # don't flush during the run
    await run_ticks(d, clock, 5)
    assert target.scrobbles == []
    assert q.count() == 1
    assert await q.flush([target]) == 1
    assert q.count() == 0 and len(target.scrobbles) == 1


async def test_shutdown_scrobbles_current_track():
    target = RecordingTarget()
    q = ScrobbleQueue(":memory:")
    d, clock = make_daemon([True] * 4, [0.05] * 4, [A] * 4, target, q)
    await run_ticks(d, clock, 4)
    await d.shutdown()
    assert len(target.scrobbles) == 1


def test_rms():
    silent = pcm_to_wav(np.zeros(4410, dtype=np.int16), 44100, 1)
    loud = pcm_to_wav(np.full(4410, 16384, dtype=np.int16), 44100, 1)
    assert rms_of_wav(silent) == 0.0
    assert abs(rms_of_wav(loud) - 0.5) < 1e-6


async def test_restart_mid_song_does_not_double_scrobble():
    q = ScrobbleQueue(":memory:")
    first = RecordingTarget()
    d, clock = make_daemon([True] * 4, [0.05] * 4, [A] * 4, first, q)
    await run_ticks(d, clock, 4)
    await d.dispatch(d.session.end())  # stopped mid-song: scrobbles
    # restarted 30s later, same song still playing, recogniser offset says it started earlier
    later = Track(A.artist, A.title, offset=5.0 + 4 * 20 + 30)
    d2, clock2 = make_daemon([True] * 3, [0.05] * 3, [later] * 3, first, q)
    clock2.t = clock.t + 30
    await run_ticks(d2, clock2, 3)
    await d2.dispatch(d2.session.end())
    assert len(first.scrobbles) == 1


def test_replay_of_same_song_is_not_a_duplicate():
    q = ScrobbleQueue(":memory:")
    assert q.seen_recently(A, 1000) is False
    assert q.seen_recently(A, 1030) is True
    assert q.seen_recently(A, 1000 + 200) is False
