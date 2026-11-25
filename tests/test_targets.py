import hashlib

import pytest
from aiohttp import web

from runout.models import Track
from runout.targets import (
    LastfmTarget,
    ListenBrainzTarget,
    TargetError,
    lastfm_sign,
    listenbrainz_payload,
)

T = Track("Robyn", "Dancing On My Own", album="Body Talk", isrc="SE1234")


def test_listenbrainz_single_payload():
    p = listenbrainz_payload(T, 1700000000)
    assert p["listen_type"] == "single"
    listen = p["payload"][0]
    assert listen["listened_at"] == 1700000000
    meta = listen["track_metadata"]
    assert meta["artist_name"] == "Robyn"
    assert meta["release_name"] == "Body Talk"
    assert meta["additional_info"]["media_player"] == "Turntable"
    assert meta["additional_info"]["isrc"] == "SE1234"


def test_listenbrainz_now_playing_payload_has_no_timestamp():
    p = listenbrainz_payload(Track("A", "B"), None)
    assert p["listen_type"] == "playing_now"
    assert "listened_at" not in p["payload"][0]
    assert "release_name" not in p["payload"][0]["track_metadata"]


def test_lastfm_signature_matches_spec():
    params = {"method": "auth.getSession", "api_key": "KEY", "token": "TOK", "format": "json"}
    expected = hashlib.md5(b"api_keyKEYmethodauth.getSessiontokenTOKSECRET").hexdigest()
    assert lastfm_sign(params, "SECRET") == expected


async def test_listenbrainz_target_posts(aiohttp_server):
    received = []

    async def submit(request):
        received.append((request.headers["Authorization"], await request.json()))
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_post("/apis/listenbrainz/1/submit-listens", submit)
    server = await aiohttp_server(app)
    t = ListenBrainzTarget("maloja", str(server.make_url("/apis/listenbrainz")), "tok")
    await t.scrobble(T, 123)
    await t.close()
    auth, body = received[0]
    assert auth == "Token tok"
    assert body["payload"][0]["listened_at"] == 123


@pytest.mark.parametrize("status,retryable", [(500, True), (429, True), (401, True), (400, False)])
async def test_listenbrainz_errors(aiohttp_server, status, retryable):
    async def submit(request):
        return web.Response(status=status, text="nope")

    app = web.Application()
    app.router.add_post("/1/submit-listens", submit)
    server = await aiohttp_server(app)
    t = ListenBrainzTarget("lb", str(server.make_url("/")), "tok")
    with pytest.raises(TargetError) as e:
        await t.scrobble(T, 1)
    await t.close()
    assert e.value.retryable is retryable


async def test_lastfm_target_scrobble(aiohttp_server):
    got = {}

    async def api(request):
        got.update(await request.post())
        return web.json_response({"scrobbles": {"@attr": {"accepted": 1, "ignored": 0}}})

    app = web.Application()
    app.router.add_post("/2.0/", api)
    server = await aiohttp_server(app)
    t = LastfmTarget("lastfm", "KEY", "SECRET", "SK", url=str(server.make_url("/2.0/")))
    await t.scrobble(T, 1700000000)
    await t.close()
    assert got["method"] == "track.scrobble"
    assert got["timestamp"] == "1700000000"
    assert got["sk"] == "SK"
    sent = {k: v for k, v in got.items() if k not in ("api_sig",)}
    assert got["api_sig"] == lastfm_sign(sent, "SECRET")


async def test_lastfm_bad_params_is_not_retryable(aiohttp_server):
    async def api(request):
        return web.json_response({"error": 6, "message": "Invalid parameters"})

    app = web.Application()
    app.router.add_post("/2.0/", api)
    server = await aiohttp_server(app)
    t = LastfmTarget("lastfm", "K", "S", "SK", url=str(server.make_url("/2.0/")))
    with pytest.raises(TargetError) as e:
        await t.scrobble(T, 1)
    await t.close()
    assert e.value.retryable is False
