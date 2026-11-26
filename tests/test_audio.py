import numpy as np

from runout.audio import parse_capture_devices, pcm_to_wav, rms_of_wav, trim_wav_start

SAMPLE = """**** List of CAPTURE Hardware Devices ****
card 2: camera [USB  Live camera], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 3: Mic [Fifine USB Microphone], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""


def test_parses_cards_in_order():
    assert parse_capture_devices(SAMPLE) == [
        ("plughw:CARD=camera,DEV=0", "USB  Live camera"),
        ("plughw:CARD=Mic,DEV=0", "Fifine USB Microphone"),
    ]


def test_no_devices():
    assert parse_capture_devices("**** List of CAPTURE Hardware Devices ****\n") == []


def test_trimming_removes_start_up_pop():
    rate = 44100
    pcm = np.zeros(rate * 6, dtype=np.int16)
    pcm[:441] = 24576  # 10 ms pop at 0.75 full scale, like the AIRHUG 28
    wav = pcm_to_wav(pcm, rate, 1)
    assert rms_of_wav(wav) > 0.02  # the pop alone looks like music
    trimmed = trim_wav_start(wav, 0.5)
    assert rms_of_wav(trimmed) == 0.0
    assert len(trimmed) < len(wav)
