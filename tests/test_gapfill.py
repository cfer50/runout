"""Gap filling: the mic misses a quiet song, the album's timing fills it in."""

from aiohttp import web

from runout.gapfill import (
    AlbumTrack,
    GapFiller,
    Heard,
    MusicBrainz,
    parse_release,
    plan_fill,
    release_ids,
)
from runout.models import Track

CURE = "The Cure"
ALBUM = "Disintegration (Remastered)"
# Disintegration, side A-ish, with real-ish lengths in seconds.
TRACKS = [
    AlbumTrack("Plainsong", CURE, 312.0),
    AlbumTrack("Pictures of You", CURE, 444.0),
    AlbumTrack("Closedown", CURE, 256.0),
    AlbumTrack("Lovesong", CURE, 209.0),
    AlbumTrack("Last Dance", CURE, 282.0),
    AlbumTrack("Lullaby", CURE, 248.0),
]
T0 = 1_000_000


def heard(title, start, album=ALBUM, isrc=None):
    return Heard(Track(CURE, title, album=album, isrc=isrc), start)


def test_fills_one_missed_song():
    gap = 444 + 256 + 4  # Pictures of You + Closedown + needle gaps
    plan = plan_fill(TRACKS, heard("Pictures of You", T0), heard("Lovesong", T0 + gap))
    assert [(t.title, start) for t, start in plan] == [("Closedown", T0 + 447)]


def test_fills_two_missed_songs_in_order():
    gap = 312 + 444 + 256
    plan = plan_fill(TRACKS, heard("Plainsong", T0), heard("Lovesong", T0 + gap))
    assert [t.title for t, _ in plan] == ["Pictures of You", "Closedown"]
    assert plan[0][1] < plan[1][1]


def test_remaster_tags_still_match():
    gap = 444 + 256
    plan = plan_fill(
        TRACKS,
        heard("Pictures of You (Remastered)", T0),
        heard("Lovesong - 2010 Remaster", T0 + gap),
    )
    assert [t.title for t, _ in plan] == ["Closedown"]


def test_skipped_song_is_not_filled():
    # Needle lifted over Closedown: Lovesong starts right after Pictures of You.
    assert plan_fill(TRACKS, heard("Pictures of You", T0), heard("Lovesong", T0 + 446)) is None


def test_long_pause_is_not_filled():
    # Went to make a cup of tea: the gap is way longer than the missing song.
    gap = 444 + 256 + 600
    assert plan_fill(TRACKS, heard("Pictures of You", T0), heard("Lovesong", T0 + gap)) is None


def test_neighbours_need_nothing():
    assert plan_fill(TRACKS, heard("Closedown", T0), heard("Lovesong", T0 + 256)) is None


def test_backwards_is_not_filled():
    assert plan_fill(TRACKS, heard("Lovesong", T0), heard("Closedown", T0 + 500)) is None


def test_too_many_missing():
    gap = sum(t.length for t in TRACKS[:5])
    assert (
        plan_fill(TRACKS, heard("Plainsong", T0), heard("Lullaby", T0 + gap), max_missing=3) is None
    )


def test_unknown_length_is_not_guessed():
    tracks = list(TRACKS)
    tracks[2] = AlbumTrack("Closedown", CURE, None)
    assert plan_fill(tracks, heard("Pictures of You", T0), heard("Lovesong", T0 + 700)) is None


def test_slightly_fast_turntable_still_fits():
    gap = int((444 + 256) * 0.98)  # deck running 2% fast
    plan = plan_fill(TRACKS, heard("Pictures of You", T0), heard("Lovesong", T0 + gap))
    assert [t.title for t, _ in plan] == ["Closedown"]


# --- MusicBrainz JSON ------------------------------------------------------------


def mb_track(pos, title, ms):
    return {
        "position": pos,
        "number": str(pos),
        "title": title,
        "length": ms,
        "recording": {"id": f"rec-{pos}", "title": title, "length": ms},
        "artist-credit": [{"name": CURE, "joinphrase": "", "artist": {"name": CURE}}],
    }


RELEASE = {
    "id": "rel-1",
    "title": "Disintegration",
    "status": "Official",
    "artist-credit": [{"name": CURE, "joinphrase": ""}],
    "media": [
        {"position": 2, "format": '12" Vinyl', "tracks": [mb_track(1, "Lovesong", 209000)]},
        {
            "position": 1,
            "format": '12" Vinyl',
            "tracks": [
                mb_track(2, "Pictures of You", 444000),
                mb_track(1, "Plainsong", 312000),
                mb_track(3, "Closedown", 256000),
            ],
        },
    ],
}


def test_parse_release_orders_sides_and_tracks():
    tl = parse_release(RELEASE)
    assert [t.title for t in tl] == ["Plainsong", "Pictures of You", "Closedown", "Lovesong"]
    assert tl[0].length == 312.0 and tl[0].artist == CURE


def search_result(title, releases, score=100):
    return {
        "id": "rec-x",
        "score": score,
        "title": title,
        "artist-credit": [{"name": CURE}],
        "releases": releases,
    }


def test_release_ids_prefers_official_and_drops_poor_matches():
    recs = [
        search_result(
            "Lovesong", [{"id": "boot", "status": "Bootleg"}, {"id": "rel-1", "status": "Official"}]
        ),
        search_result("Lovesong (live)", [{"id": "live", "status": "Official"}]),
        search_result("Love Song", [{"id": "other-artist", "status": "Official"}], score=60),
    ]
    assert release_ids(recs, "Lovesong") == ["rel-1", "live", "boot"]


# --- end to end against a fake MusicBrainz ----------------------------------------


async def fake_mb(aiohttp_server, seen):
    async def isrc(request):
        seen.append(("isrc", request.match_info["isrc"]))
        return web.json_response(
            {
                "recordings": [
                    search_result("Pictures of You", [{"id": "rel-1", "status": "Official"}])
                ]
            }
        )

    async def search(request):
        seen.append(("search", request.query["query"]))
        q = request.query["query"]
        title = "Lovesong" if "Lovesong" in q else "?"
        return web.json_response(
            {"recordings": [search_result(title, [{"id": "rel-1", "status": "Official"}])]}
        )

    async def release(request):
        seen.append(("release", request.match_info["rid"], request.query.get("inc")))
        assert request.headers["User-Agent"].startswith("runout/")
        return web.json_response(RELEASE)

    app = web.Application()
    app.router.add_get("/ws/2/isrc/{isrc}", isrc)
    app.router.add_get("/ws/2/recording", search)
    app.router.add_get("/ws/2/release/{rid}", release)
    return await aiohttp_server(app)


async def test_gapfiller_end_to_end(aiohttp_server):
    seen = []
    server = await fake_mb(aiohttp_server, seen)
    gf = GapFiller(MusicBrainz(str(server.make_url("/ws/2")), min_interval=0))
    out = await gf.fill(
        heard("Pictures of You", T0, isrc="GBAKW8900001"),
        heard("Lovesong", T0 + 444 + 256 + 3),
    )
    assert [(s.track.title, s.track.album, s.track.source) for s in out] == [
        ("Closedown", ALBUM, "gapfill")
    ]
    assert ("isrc", "GBAKW8900001") in seen  # used the precise ISRC lookup
    assert any(
        k == "search" and 'artist:"The Cure"' in q for k, q, *_ in [(*s, None) for s in seen]
    )
    # a second album gap reuses the cached tracklist
    n = len([s for s in seen if s[0] == "release"])
    await gf.fill(heard("Pictures of You", T0, isrc="GBAKW8900001"), heard("Lovesong", T0 + 703))
    assert len([s for s in seen if s[0] == "release"]) == n
    await gf.close()


async def test_different_albums_skip_the_lookup(aiohttp_server):
    seen = []
    server = await fake_mb(aiohttp_server, seen)
    gf = GapFiller(MusicBrainz(str(server.make_url("/ws/2")), min_interval=0))
    out = await gf.fill(heard("Pictures of You", T0), heard("Lovesong", T0 + 700, album="Show"))
    assert out == [] and seen == []
    await gf.close()


async def test_musicbrainz_down_fills_nothing():
    gf = GapFiller(MusicBrainz("http://127.0.0.1:9/ws/2", min_interval=0))
    assert await gf.fill(heard("Pictures of You", T0), heard("Lovesong", T0 + 703)) == []
    await gf.close()


async def test_whole_side_with_a_quiet_song(aiohttp_server):
    """Real daemon, real session, real GapFiller, fake MusicBrainz. The mic hears
    nothing during the quiet middle song; runout still scrobbles it."""
    from test_queue_and_daemon import (
        Clock,
        FakeRecogniser,
        FakeRecorder,
        FakeTrigger,
        RecordingTarget,
    )

    from runout.config import parse
    from runout.daemon import Daemon
    from runout.queue import ScrobbleQueue
    from runout.triggers import Debounced

    release = {
        "title": "Disintegration",
        "artist-credit": [{"name": CURE}],
        "media": [
            {
                "position": 1,
                "tracks": [
                    mb_track(1, "Lovesong", 100000),
                    mb_track(2, "Closedown", 60000),
                    mb_track(3, "Lullaby", 100000),
                ],
            }
        ],
    }

    async def search(request):
        q = request.query["query"]
        title = "Lovesong" if "Lovesong" in q else "Lullaby"
        return web.json_response(
            {"recordings": [search_result(title, [{"id": "r", "status": "Official"}])]}
        )

    async def get_release(request):
        return web.json_response(release)

    app = web.Application()
    app.router.add_get("/ws/2/recording", search)
    app.router.add_get("/ws/2/release/{rid}", get_release)
    server = await aiohttp_server(app)

    def at(title, offset):
        return Track(CURE, title, album=ALBUM, offset=offset)

    # One sample every 20 s. Lovesong 0-100 s, Closedown 100-160 s (too quiet), Lullaby from 160 s.
    results = (
        [at("Lovesong", o) for o in (0, 20, 40, 60, 80)]
        + [None, None, None]
        + [at("Lullaby", o) for o in (0, 20, 40, 60, 80)]
    )
    target = RecordingTarget()
    clock = Clock(t=1_000_000.0)
    d = Daemon(
        parse({"target": [{"type": "log"}], "trigger": {"type": "command", "command": "x"}}),
        trigger=Debounced(FakeTrigger([True] * 13 + [False] * 3), on_after=1, off_after=1),
        recorder=FakeRecorder([0.05] * 13),
        recogniser=FakeRecogniser(results),
        targets=[target],
        queue=ScrobbleQueue(":memory:"),
        gapfiller=GapFiller(MusicBrainz(str(server.make_url("/ws/2")), min_interval=0)),
        clock=clock,
    )
    for _ in range(16):
        await d.tick()
        clock.t += 20
    await d.shutdown()
    assert [(t.title, ts - 1_000_000) for t, ts in target.scrobbles] == [
        ("Lovesong", 0),
        ("Closedown", 100),
        ("Lullaby", 160),
    ]
