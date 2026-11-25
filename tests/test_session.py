from runout.config import SessionConfig
from runout.models import NowPlaying, Scrobble, Track
from runout.session import Session

A = Track("Saint Etienne", "Only Love Can Break Your Heart", album="Foxbase Alpha")
B = Track("The Weakerthans", "Plea from a Cat Named Virtue", album="Reconstruction Site")
SAMPLE = 10
STEP = 20


def make(**kw) -> Session:
    return Session(SessionConfig(**kw), sample_seconds=SAMPLE)


def feed(session: Session, seq, start=1000.0):
    """Feed a sequence of tracks/None at 20s intervals; return all events."""
    events = []
    t = start
    for item in seq:
        events += session.observe(item, t)
        t += STEP
    return events, t


def scrobbles(events):
    return [e for e in events if isinstance(e, Scrobble)]


def test_single_stray_match_is_ignored():
    s = make()
    events, _ = feed(s, [A, None, None, None, None, None, None])
    events += s.end()
    assert events == []


def test_confirmed_track_sends_now_playing_once():
    s = make()
    events, _ = feed(s, [A, A, A, A])
    assert [type(e) for e in events] == [NowPlaying]


def test_track_change_scrobbles_previous():
    s = make()
    events, _ = feed(s, [A] * 6 + [B, B])
    sc = scrobbles(events)
    assert len(sc) == 1 and sc[0].track == A
    assert sum(isinstance(e, NowPlaying) for e in events) == 2


def test_one_odd_match_does_not_interrupt():
    s = make()
    events, _ = feed(s, [A, A, A, B, A, A])
    assert scrobbles(events) == []
    assert s.current.track == A
    assert s.current.hits == 5


def test_long_track_scrobbles_after_four_minutes_only_once():
    s = make()
    events, _ = feed(s, [A] * 20)  # 20 samples * 20s = 400s heard
    events += s.end()
    assert len(scrobbles(events)) == 1


def test_too_short_listen_is_not_scrobbled():
    s = make(min_listen_seconds=60)
    events, _ = feed(s, [A, A])  # heard 30s
    events += s.end()
    assert scrobbles(events) == []


def test_idle_timeout_ends_track():
    s = make(idle_timeout_seconds=50)
    events, _ = feed(s, [A, A, A, None, None, None, None])
    assert len(scrobbles(events)) == 1
    assert s.current is None


def test_timestamp_uses_recogniser_offset():
    s = make()
    a_offset = Track(A.artist, A.title, offset=95.0)
    events, _ = feed(s, [a_offset, a_offset, a_offset], start=10_000.0)
    events += s.end()
    assert scrobbles(events)[0].listened_at == 10_000 - 95


def test_absurd_offset_is_ignored():
    s = make()
    t = Track(A.artist, A.title, offset=99_999.0)
    events, _ = feed(s, [t, t, t], start=10_000.0)
    events += s.end()
    assert scrobbles(events)[0].listened_at == 10_000


def test_remaster_variants_count_as_same_song():
    s = make()
    a2 = Track("Saint Etienne", "Only Love Can Break Your Heart (2009 Remaster)")
    events, _ = feed(s, [A, a2, A])
    assert s.current is not None and s.current.hits == 3


def test_prefers_metadata_with_album():
    s = make()
    bare = Track(A.artist, A.title)
    feed(s, [bare, bare, A])
    assert s.current.track.album == "Foxbase Alpha"


def test_min_confirmations_one_starts_immediately():
    s = make(min_confirmations=1)
    events = s.observe(A, 0)
    assert events == [NowPlaying(A)]
