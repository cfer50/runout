"""Triggers decide when to listen.

The point is to *not* run a microphone all day. A trigger answers one
question, "is the record player in use right now?", and runout only
samples audio while the answer is yes.

Built in:

* ``always``        - listen continuously (fine for a dedicated listening room)
* ``homeassistant`` - read any Home Assistant entity, e.g. a smart plug's
                      power sensor (turntable motor spinning draws a few
                      extra watts) or a media_player/binary_sensor state
* ``command``       - run a shell command; exit code 0 means "on". Use it
                      for a GPIO pin, an MQTT one-liner, anything.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import aiohttp

from runout.config import TriggerConfig

log = logging.getLogger(__name__)


class Trigger(ABC):
    @abstractmethod
    async def poll(self) -> bool | None:
        """Return True/False, or None if the state couldn't be read."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass


class AlwaysTrigger(Trigger):
    async def poll(self) -> bool:
        return True


class HomeAssistantTrigger(Trigger):
    def __init__(self, cfg: TriggerConfig, session: aiohttp.ClientSession | None = None):
        self.url = cfg.url.rstrip("/") + f"/api/states/{cfg.entity_id}"
        self.headers = {"Authorization": f"Bearer {cfg.token}"}
        self.on_above = cfg.on_above
        self.on_states = {s.lower() for s in cfg.on_states}
        self._session = session
        self._own_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    def interpret(self, state: str) -> bool | None:
        if state in ("unavailable", "unknown", ""):
            return None
        if self.on_above is not None:
            try:
                return float(state) > self.on_above
            except ValueError:
                log.warning("entity state %r is not numeric but on_above is set", state)
                return None
        return state.lower() in self.on_states

    async def poll(self) -> bool | None:
        session = await self._get_session()
        try:
            async with session.get(self.url, headers=self.headers) as resp:
                if resp.status != 200:
                    log.warning("home assistant returned HTTP %s for %s", resp.status, self.url)
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("home assistant unreachable: %s", e)
            return None
        return self.interpret(str(data.get("state", "")))

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()


class CommandTrigger(Trigger):
    def __init__(self, cfg: TriggerConfig):
        self.command = cfg.command

    async def poll(self) -> bool | None:
        try:
            proc = await asyncio.create_subprocess_shell(
                self.command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            code = await asyncio.wait_for(proc.wait(), timeout=15)
        except (TimeoutError, OSError) as e:
            log.warning("trigger command failed: %s", e)
            return None
        return code == 0


class Debounced:
    """Wraps a trigger so a single odd reading doesn't start or stop a session.

    Unreadable states (None) leave the current state unchanged.
    """

    def __init__(self, trigger: Trigger, on_after: int = 2, off_after: int = 3):
        self.trigger = trigger
        self.on_after = max(1, on_after)
        self.off_after = max(1, off_after)
        self.active = False
        self._streak = 0

    def feed(self, reading: bool | None) -> bool:
        if reading is None or reading == self.active:
            self._streak = 0
            return self.active
        self._streak += 1
        if self._streak >= (self.on_after if reading else self.off_after):
            self.active = reading
            self._streak = 0
        return self.active

    async def poll(self) -> bool:
        return self.feed(await self.trigger.poll())


def build_trigger(cfg: TriggerConfig) -> Debounced:
    inner: Trigger
    if cfg.type == "homeassistant":
        inner = HomeAssistantTrigger(cfg)
    elif cfg.type == "command":
        inner = CommandTrigger(cfg)
    else:
        inner = AlwaysTrigger()
    on_after = 1 if cfg.type == "always" else cfg.on_debounce
    return Debounced(inner, on_after=on_after, off_after=cfg.off_debounce)
