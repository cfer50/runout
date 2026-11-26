import pytest

from runout.models import Track


@pytest.mark.parametrize(
    "a,b",
    [
        ("Heroes", "Heroes (2017 Remaster)"),
        ("Heroes", "Heroes - 2017 Remastered"),
        ("Heroes", "heroes"),
        ("Heroes", "Heroes - Single Version"),
        ("Heroes", "Heroes [Mono]"),
    ],
)
def test_title_variants_share_a_key(a, b):
    assert Track("David Bowie", a).key == Track("David Bowie", b).key


def test_accents_and_ampersands():
    assert Track("Beyoncé & Jay-Z", "X").key == Track("Beyonce and JayZ", "X").key


def test_different_songs_differ():
    assert Track("Robyn", "Dancing On My Own").key != Track("Robyn", "Call Your Girlfriend").key


def test_parenthetical_that_is_part_of_title_survives():
    # Not a version qualifier, so it stays part of the identity.
    assert (
        Track("Pet Shop Boys", "Love etc.").key
        != Track("Pet Shop Boys", "Love etc. (Was It Something I Said)").key
    )


def test_artist_spelling_variants_match():
    # Seen in the wild: Deezer vs Shazam for the same song.
    assert Track("Salt-N-Pepa", "Whatta Man").key == Track("Salt 'N' Pepa", "Whatta Man").key


def test_featuring_credit_is_ignored():
    assert (
        Track("Salt-N-Pepa", "Whatta Man (feat. En Vogue)").key
        == Track("Salt 'N' Pepa", "Whatta Man").key
    )
