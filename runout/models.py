"""Small data types shared across the package."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Track:
    """A track as identified by a recogniser."""

    artist: str
    title: str
    album: str | None = None
    isrc: str | None = None
    # Seconds into the track at which the sample started, if the recogniser knows.
    offset: float | None = None
    source: str = "unknown"
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def key(self) -> str:
        """Identity used to decide whether two matches are the same song.

        Ignores case, accents, punctuation and trailing qualifiers such as
        "(2012 Remaster)" or "- Live", because the same vinyl side often
        matches several releases of one song.
        """
        return f"{_normalise(self.artist)}::{_normalise(_strip_qualifiers(self.title))}"


_QUALIFIER = re.compile(
    r"\s*([(\[].*?(remaster|version|mix|edit|live|mono|stereo|deluxe"
    r"|\bfeat\b|\bft\.|featuring|\bwith\b).*?[)\]]"
    r"|-\s*(\d{4}\s*)?(remaster(ed)?|live|mono|stereo|single version|radio edit).*)$",
    re.IGNORECASE,
)


def _strip_qualifiers(title: str) -> str:
    prev = None
    while prev != title:
        prev = title
        title = _QUALIFIER.sub("", title).strip()
    return title


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("&", "and")
    # Drop punctuation AND spaces: "Salt-N-Pepa" and "Salt 'N' Pepa" must match.
    return re.sub(r"[\W_]+", "", text)


def title_key(title: str) -> str:
    """A song title reduced to what identifies it (no case, accents, punctuation, remaster tags)."""
    return _normalise(_strip_qualifiers(title))


@dataclass(frozen=True)
class Scrobble:
    """A completed listen, ready to submit."""

    track: Track
    listened_at: int  # unix seconds, when the track started


@dataclass(frozen=True)
class NowPlaying:
    track: Track
    # When the song started (unix seconds), if known. Used to fill in missed tracks.
    started_at: int | None = field(default=None, compare=False)
