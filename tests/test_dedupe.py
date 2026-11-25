import pytest
from aiohttp import web

from runout.config import ConfigError, parse
from runout.dedupe import LastfmHistory, PastScrobble, is_duplicate, parse_recent_tracks
from runout.models import Track

RECENT = {
    "recenttracks": {
        "track": [
            {  # now playing: no date, must be ignored
                "artist": {"#text": "Slowdive"},
                "name": "When the Sun Hits",
                "@attr": {"nowplaying": "true"},
            },
            {
                "artist": {"#text": "Dum Dum Girls"},
                "name": "Coming Down",
                "date": {"uts": "1000"},
            },
        ]
    }
}


def test_parse_ignores_now_playing():
    hist = parse_recent_tracks(RECENT)
    assert hist == [PastScrobble(Track("Dum Dum Girls", "Coming Down").key, 1000)]


def test_parse_single_track_object():
    data = {"recenttracks": {"track": RECENT["recenttracks"]["track"][1]}}
    assert len(parse_recent_tracks(data)) == 1


def test_duplicate_matching():
    hist = parse_recent_tracks(RECENT)
    same = Track("Dum Dum Girls", "Coming Down (Remastered)")
    assert is_duplicate(same, 1100, hist, window=600)
    assert not is_duplicate(same, 5000, hist, window=600)  # a later replay
    assert not is_duplicate(Track("Dum Dum Girls", "Bhang Bhang"), 1000, hist, window=600)


async def test_history_against_fake_lastfm(aiohttp_server):
    seen = {}

    async def api(request):
        seen.update(request.query)
        return web.json_response(RECENT)

    app = web.Application()
    app.router.add_get("/2.0/", api)
    server = await aiohttp_server(app)
    h = LastfmHistory("cfer50", "KEY", 600, url=str(server.make_url("/2.0/")))
    assert await h.already_scrobbled(Track("Dum Dum Girls", "Coming Down"), 1050)
    assert seen["method"] == "user.getRecentTracks" and seen["user"] == "cfer50"
    assert seen["from"] == str(1050 - 600)
    await h.close()


async def test_unreachable_lastfm_scrobbles_anyway():
    h = LastfmHistory("cfer50", "KEY", url="http://127.0.0.1:9/2.0/")
    assert await h.already_scrobbled(Track("A", "B"), 1000) is False
    await h.close()


def test_config_borrows_api_key_from_lastfm_target():
    cfg = parse(
        {
            "dedupe": {"lastfm_user": "cfer50"},
            "target": [{"type": "lastfm", "api_key": "K", "api_secret": "S", "session_key": "SK"}],
        }
    )
    assert cfg.dedupe.api_key == "K"


def test_config_dedupe_needs_a_key():
    with pytest.raises(ConfigError, match="api_key"):
        parse({"dedupe": {"lastfm_user": "x"}, "target": [{"type": "log"}]})
