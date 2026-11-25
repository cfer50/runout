from aiohttp import web

from runout.config import TriggerConfig
from runout.triggers import CommandTrigger, Debounced, HomeAssistantTrigger


def test_debounce_needs_consecutive_readings():
    d = Debounced(trigger=None, on_after=2, off_after=3)
    assert d.feed(True) is False
    assert d.feed(True) is True
    assert d.feed(False) is True
    assert d.feed(None) is True  # unreadable: no change, streak reset
    assert d.feed(False) is True
    assert d.feed(False) is True
    assert d.feed(False) is False


def test_numeric_interpretation():
    cfg = TriggerConfig(
        type="homeassistant", url="http://x", token="t", entity_id="sensor.p", on_above=6.5
    )
    t = HomeAssistantTrigger(cfg)
    assert t.interpret("8.1") is True
    assert t.interpret("5.0") is False
    assert t.interpret("unavailable") is None
    assert t.interpret("abc") is None


def test_state_interpretation():
    cfg = TriggerConfig(type="homeassistant", url="http://x", token="t", entity_id="switch.tt")
    t = HomeAssistantTrigger(cfg)
    assert t.interpret("on") is True
    assert t.interpret("off") is False


async def test_homeassistant_poll_against_fake_server(aiohttp_server):
    seen = {}

    async def state(request):
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response({"entity_id": "sensor.p", "state": "8.4"})

    app = web.Application()
    app.router.add_get("/api/states/sensor.p", state)
    server = await aiohttp_server(app)
    cfg = TriggerConfig(
        type="homeassistant",
        url=str(server.make_url("/")),
        token="abc",
        entity_id="sensor.p",
        on_above=6.5,
    )
    t = HomeAssistantTrigger(cfg)
    assert await t.poll() is True
    assert seen["auth"] == "Bearer abc"
    await t.close()


async def test_homeassistant_unreachable_is_none():
    cfg = TriggerConfig(
        type="homeassistant", url="http://127.0.0.1:9", token="t", entity_id="sensor.p", on_above=1
    )
    t = HomeAssistantTrigger(cfg)
    assert await t.poll() is None
    await t.close()


async def test_command_trigger():
    assert await CommandTrigger(TriggerConfig(type="command", command="true")).poll() is True
    assert await CommandTrigger(TriggerConfig(type="command", command="false")).poll() is False
