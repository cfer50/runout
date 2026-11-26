"""Capture short audio samples from a microphone or line-in.

Two backends:

* ``arecord`` (default): shells out to ALSA's arecord. Nothing to compile,
  works on a fresh Raspberry Pi OS Lite install.
* ``sounddevice``: PortAudio via the ``sounddevice`` package, for macOS or
  setups where arecord isn't available.

Samples come back as 16-bit mono/stereo WAV bytes, which every recogniser
accepts.
"""

from __future__ import annotations

import asyncio
import io
import re
import shutil
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from runout.config import AudioConfig


@dataclass
class Sample:
    wav: bytes
    seconds: float
    rms: float  # 0..1, fraction of full scale

    def is_silent(self, threshold: float) -> bool:
        return self.rms < threshold


def rms_of_wav(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit audio")
        frames = w.readframes(w.getnframes())
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if pcm.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(pcm))) / 32768.0)


def trim_wav_start(wav: bytes, seconds: float) -> bytes:
    """Drop the first ``seconds`` of a WAV.

    Many USB mics click or pop as they start recording. A 10 ms pop at full volume
    is enough to make a silent room measure as loud as music, so we cut it off.
    """
    with wave.open(io.BytesIO(wav)) as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        w.setpos(min(w.getnframes(), int(seconds * rate)))
        frames = w.readframes(w.getnframes())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(frames)
    return buf.getvalue()


def pcm_to_wav(pcm: np.ndarray, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.astype("<i2").tobytes())
    return buf.getvalue()


class Recorder(ABC):
    def __init__(self, cfg: AudioConfig):
        self.cfg = cfg

    @abstractmethod
    async def record(self, seconds: float) -> bytes: ...

    async def sample(self, seconds: float | None = None) -> Sample:
        seconds = seconds or self.cfg.sample_seconds
        trim = self.cfg.trim_start_seconds
        wav = await self.record(seconds + trim)
        if trim > 0:
            wav = trim_wav_start(wav, trim)
        return Sample(wav=wav, seconds=seconds, rms=rms_of_wav(wav))


_CARD_LINE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+):")


def parse_capture_devices(arecord_l: str) -> list[tuple[str, str]]:
    """Parse `arecord -l` into [(alsa_device, description)], in listed order."""
    found = []
    for line in arecord_l.splitlines():
        m = _CARD_LINE.match(line.strip())
        if m:
            _num, card_id, desc, dev = m.groups()
            found.append((f"plughw:CARD={card_id},DEV={dev}", desc.strip()))
    return found


class ArecordRecorder(Recorder):
    def __init__(self, cfg: AudioConfig):
        super().__init__(cfg)
        if shutil.which("arecord") is None:
            raise RuntimeError(
                "arecord not found. Install it with `sudo apt install alsa-utils`, "
                'or set [audio] backend = "sounddevice".'
            )

    async def _device(self) -> str:
        """With device = "auto", use the first microphone the system lists.

        Looked up on every sample, so swapping mics needs no config change or restart.
        """
        if self.cfg.device != "auto":
            return self.cfg.device
        proc = await asyncio.create_subprocess_exec(
            "arecord", "-l", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        devices = parse_capture_devices(out.decode(errors="replace"))
        if not devices:
            raise RuntimeError("no microphone found (`arecord -l` lists no capture devices)")
        return devices[0][0]

    async def record(self, seconds: float) -> bytes:
        cmd = [
            "arecord", "-q",
            "-D", await self._device(),
            "-f", "S16_LE",
            "-r", str(self.cfg.sample_rate),
            "-c", str(self.cfg.channels),
            "-s", str(max(1, int(seconds * self.cfg.sample_rate))),  # exact length, not whole secs
            "-t", "wav", "-",
        ]  # fmt: skip
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"arecord failed: {err.decode(errors='replace').strip()}")
        # arecord writes a streaming header with bogus sizes when piping; re-wrap it.
        return _fix_streamed_wav(out, self.cfg.sample_rate, self.cfg.channels)


def _fix_streamed_wav(data: bytes, sample_rate: int, channels: int) -> bytes:
    idx = data.find(b"data")
    if idx == -1:
        raise RuntimeError("arecord returned no audio data")
    pcm = np.frombuffer(data[idx + 8 :][: (len(data) - idx - 8) // 2 * 2], dtype="<i2")
    return pcm_to_wav(pcm, sample_rate, channels)


class SoundDeviceRecorder(Recorder):
    def __init__(self, cfg: AudioConfig):
        super().__init__(cfg)
        try:
            import sounddevice  # noqa: F401
        except ImportError as e:  # pragma: no cover - depends on system
            raise RuntimeError(
                "sounddevice isn't installed: pip install 'runout[sounddevice]'"
            ) from e

    async def record(self, seconds: float) -> bytes:
        import sounddevice as sd

        device = None if self.cfg.device == "default" else self.cfg.device

        def _rec() -> np.ndarray:
            data = sd.rec(
                int(seconds * self.cfg.sample_rate),
                samplerate=self.cfg.sample_rate,
                channels=self.cfg.channels,
                dtype="int16",
                device=device,
            )
            sd.wait()
            return data

        pcm = await asyncio.to_thread(_rec)
        return pcm_to_wav(pcm, self.cfg.sample_rate, self.cfg.channels)


def build_recorder(cfg: AudioConfig) -> Recorder:
    if cfg.backend == "sounddevice":
        return SoundDeviceRecorder(cfg)
    return ArecordRecorder(cfg)
