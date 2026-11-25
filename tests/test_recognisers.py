from runout.recognisers import parse_audd, parse_shazam

SHAZAM = {
    "matches": [{"id": "1", "offset": 42.7, "timeskew": 0.0001}],
    "track": {
        "key": "123",
        "title": "Plea from a Cat Named Virtue",
        "subtitle": "The Weakerthans",
        "isrc": "CA1230000001",
        "url": "https://www.shazam.com/track/123",
        "sections": [
            {
                "type": "SONG",
                "metadata": [
                    {"title": "Album", "text": "Reconstruction Site"},
                    {"title": "Label", "text": "Epitaph"},
                ],
            }
        ],
    },
}


def test_parse_shazam():
    t = parse_shazam(SHAZAM)
    assert t.artist == "The Weakerthans"
    assert t.title == "Plea from a Cat Named Virtue"
    assert t.album == "Reconstruction Site"
    assert t.offset == 42.7
    assert t.isrc == "CA1230000001"


def test_parse_shazam_no_match():
    assert parse_shazam({"matches": [], "tag": "x"}) is None


def test_parse_audd():
    t = parse_audd(
        {
            "status": "success",
            "result": {
                "artist": "Madness",
                "title": "Our House",
                "album": "The Rise & Fall",
                "timecode": "01:23",
            },
        }
    )
    assert t.artist == "Madness" and t.offset == 83


def test_parse_audd_no_match():
    assert parse_audd({"status": "success", "result": None}) is None
