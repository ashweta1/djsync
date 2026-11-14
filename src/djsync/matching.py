"""Fuzzy matching of tracks by artist + title.

Spotify, stores and rekordbox all spell the same track differently:
"Track (Original Mix)", "Track - Extended Mix", "Track feat. X", accents,
"&" vs "and", etc. We normalise both sides and then fuzzy-compare.
Remix names are kept, because "Track (Bicep Remix)" is a different record.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, process

# Version tags that don't change which record it is.
_NOISE_VERSIONS = (
    r"original mix|extended mix|extended version|extended|radio edit|radio mix|"
    r"club mix|original version|album version|single version|main mix|"
    r"remaster(?:ed)?(?: \d{4})?|\d{4} remaster(?:ed)?|mixed|explicit|clean"
)
_BRACKETED_NOISE = re.compile(rf"[\(\[]\s*(?:{_NOISE_VERSIONS})\s*[\)\]]", re.I)
_DASH_NOISE = re.compile(rf"\s+-\s+(?:{_NOISE_VERSIONS})\s*$", re.I)
_FEAT = re.compile(r"[\(\[]?\s*\b(?:feat\.?|ft\.?|featuring|with)\s+[^\)\]\-]*[\)\]]?", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\bx\b|\band\b|\bvs\.?\b|;|/|\bfeat\.?\b|\bft\.?\b)\s*", re.I)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _clean(s: str) -> str:
    s = _ascii(s).lower().replace("&", " and ")
    s = _NON_ALNUM.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def normalize_title(title: str) -> str:
    t = title or ""
    t = _BRACKETED_NOISE.sub(" ", t)
    t = _DASH_NOISE.sub("", t)
    t = _FEAT.sub(" ", t)
    # "Track - Bicep Remix" -> "Track Bicep Remix"
    return _clean(t)


def split_artists(artist: str) -> list[str]:
    parts = [_clean(p) for p in _ARTIST_SPLIT.split(_ascii(artist or ""))]
    return [p for p in parts if p]


def normalize_artist(artist: str) -> str:
    return " ".join(sorted(split_artists(artist)))


@dataclass(frozen=True)
class TrackKey:
    artist: str
    title: str

    @classmethod
    def of(cls, artist: str, title: str) -> "TrackKey":
        return cls(normalize_artist(artist), normalize_title(title))


def similar(a: TrackKey, b: TrackKey, threshold: int = 88) -> bool:
    if not a.title or not b.title:
        return False
    if fuzz.ratio(a.title, b.title) < threshold:
        return False
    if not a.artist or not b.artist:
        return True  # title strongly matches, artist unknown on one side
    return fuzz.token_set_ratio(a.artist, b.artist) >= threshold - 10


class Index:
    """Lookup structure for 'do I already have this track?'."""

    def __init__(self, keys: list[TrackKey], threshold: int = 88):
        self.threshold = threshold
        self.keys = keys
        self.exact = {k: i for i, k in enumerate(keys)}
        self.titles = [k.title for k in keys]

    def find(self, key: TrackKey) -> int | None:
        """Return the index of the matching library key, or None."""
        if key in self.exact:
            return self.exact[key]
        if not key.title:
            return None
        candidates = process.extract(
            key.title, self.titles, scorer=fuzz.ratio,
            score_cutoff=self.threshold, limit=10,
        )
        for _, _, i in candidates:
            if similar(key, self.keys[i], self.threshold):
                return i
        return None

    def __contains__(self, key: TrackKey) -> bool:
        return self.find(key) is not None


def clean(s: str) -> str:
    return _clean(s)


_HAY_NOISE = re.compile(
    r"\b(?:original mix|extended mix|extended edit|radio edit|club mix|main mix|"
    r"extended version|original version)\b"
)
_VERSION_WORDS = ("remix", "edit", "rework", "bootleg", "dub", "vip", "flip", "refix", "remake")


def file_score(artist: str, title: str, file_artist: str, file_title: str, file_text: str) -> int:
    """How well an audio file matches a wanted track, 0-100.

    Strict tag/filename match scores 100. Otherwise we look for the title and
    each artist anywhere in the file's tags + filename, so files named like
    "glue_master_320.mp3" in a "Bicep" folder, or "Glue [Bicep] 24bit" still match.
    """
    want = TrackKey.of(artist, title)
    if not want.title:
        return 0
    if file_title and similar(want, TrackKey.of(file_artist, file_title)):
        return 100
    hay = _clean(_BRACKETED_NOISE.sub(" ", file_text))
    hay = _HAY_NOISE.sub(" ", hay).strip()
    if not hay:
        return 0
    if len(want.title) >= 4:
        title_score = fuzz.partial_ratio(want.title, hay)
    else:  # very short titles: need the exact word
        title_score = 100 if f" {want.title} " in f" {hay} " else 0
    artists = split_artists(artist)
    artist_score = max((fuzz.partial_ratio(a, hay) for a in artists), default=100)
    score = min(title_score, artist_score) if artists else title_score
    # "Glue" shouldn't grab "Glue (Hammer Remix)", and vice versa
    for w in _VERSION_WORDS:
        if (f" {w}" in f" {hay}") != (f" {w}" in f" {want.title}"):
            score -= 40
            break
    return max(0, int(score))
