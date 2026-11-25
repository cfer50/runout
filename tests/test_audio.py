from runout.audio import parse_capture_devices

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
