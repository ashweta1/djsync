"""Search links for stores that sell DJ-quality downloads."""

from __future__ import annotations

import re
import webbrowser
from urllib.parse import quote, quote_plus

from .matching import _BRACKETED_NOISE, _DASH_NOISE, _FEAT

STORES: dict[str, tuple[str, str]] = {
    # key: (display name, search URL template)
    "beatport": ("Beatport", "https://www.beatport.com/search/tracks?q={q}"),
    "bandcamp": ("Bandcamp", "https://bandcamp.com/search?q={q}&item_type=t"),
    "deezer": ("Deezer", "https://www.deezer.com/search/{q}/track"),
    "itunes": ("Apple Music / iTunes", "https://music.apple.com/search?term={q}"),
    "amazon": ("Amazon Music", "https://www.amazon.com/s?k={q}&i=digital-music"),
}


MAX_TABS = 30  # don't flood the browser in one click


def search_query(artist: str, title: str) -> str:
    """'Bicep, Hammer' + 'Glue (Original Mix) [feat. X]' -> 'Bicep Glue'.

    Keeps remix names ("Glue (Hammer Remix)") since they pick the right version.
    """
    first_artist = artist.split(",")[0].strip()
    t = _BRACKETED_NOISE.sub(" ", title or "")
    t = _DASH_NOISE.sub("", t)
    t = _FEAT.sub(" ", t)
    t = re.sub(r"[\(\)\[\]]", " ", t)
    return re.sub(r"\s+", " ", f"{first_artist} {t}").strip()


def url(store: str, artist: str, title: str) -> str:
    q = search_query(artist, title)
    # Deezer takes the query as a path segment, so spaces must be %20, not +
    enc = quote(q, safe="") if store == "deezer" else quote_plus(q)
    return STORES[store][1].format(q=enc)


def links(artist: str, title: str, enabled: list[str]) -> list[dict]:
    return [
        {"key": k, "name": STORES[k][0], "url": url(k, artist, title)}
        for k in enabled if k in STORES
    ]


def open_tabs(urls: list[str]) -> int:
    """Open URLs as tabs in the default browser. Returns how many were opened."""
    urls = urls[:MAX_TABS]
    for u in urls:
        webbrowser.open_new_tab(u)
    return len(urls)
