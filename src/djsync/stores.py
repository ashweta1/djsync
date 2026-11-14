"""Search links for stores that sell DJ-quality downloads."""

from __future__ import annotations

from urllib.parse import quote_plus

STORES: dict[str, tuple[str, str]] = {
    # key: (display name, search URL template)
    "beatport": ("Beatport", "https://www.beatport.com/search/tracks?q={q}"),
    "bandcamp": ("Bandcamp", "https://bandcamp.com/search?q={q}&item_type=t"),
    "traxsource": ("Traxsource", "https://www.traxsource.com/search?term={q}"),
    "juno": ("Juno Download", "https://www.junodownload.com/search/?q%5Ball%5D%5B%5D={q}"),
    "itunes": ("Apple Music / iTunes", "https://music.apple.com/search?term={q}"),
    "amazon": ("Amazon Music", "https://www.amazon.com/s?k={q}&i=digital-music"),
}


def search_query(artist: str, title: str) -> str:
    first_artist = artist.split(",")[0].strip()
    return f"{first_artist} {title}".strip()


def links(artist: str, title: str, enabled: list[str]) -> list[dict]:
    q = quote_plus(search_query(artist, title))
    out = []
    for key in enabled:
        if key in STORES:
            name, tpl = STORES[key]
            out.append({"key": key, "name": name, "url": tpl.format(q=q)})
    return out
