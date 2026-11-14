"""Search Beatport's own catalogue, the same search as beatport.com's search box.

General web search only finds Beatport pages a search engine has already indexed,
which misses new releases. This loads Beatport's search results page from your
Mac (one request per track, only when you research it) and reads the tracks
out of it.

Beatport's site is built with Next.js, which embeds the page's data as JSON in a
<script id="__NEXT_DATA__"> tag. We walk that JSON for anything that looks like a
track. If the page layout changes and that fails, we fall back to the
/track/<slug>/<id> links in the HTML.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

SEARCH_URL = "https://www.beatport.com/search/tracks?q={q}"
HEADERS = {
    # Look like the browser you'd otherwise use; Beatport refuses obvious bots.
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
_NEXT_DATA = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_TRACK_LINK = re.compile(r'href="(?:https://www\.beatport\.com)?(/track/([^/"?#]+)/(\d+))"[^>]*>(.*?)</a>', re.S)
_TAGS = re.compile(r"<[^>]+>")


@dataclass
class Candidate:
    url: str
    title: str = ""
    mix: str = ""
    artists: str = ""
    remixers: str = ""
    label: str = ""
    release: str = ""
    released: str = ""
    length: str = ""
    bpm: str = ""
    key: str = ""
    isrc: str = ""

    def meta(self) -> str:
        """Short details for the card: 'Ninja Tune · 2017-01-13 · 4:29 · 130 BPM · E Minor'."""
        bits = [self.label, self.released[:10], self.length,
                f"{self.bpm} BPM" if self.bpm else "", self.key]
        return " · ".join(b for b in bits if b)

    def describe(self) -> str:
        bits = [f"{self.artists} - {self.title}" + (f" ({self.mix})" if self.mix else "")]
        for name, v in (("remixers", self.remixers), ("label", self.label), ("release", self.release),
                        ("released", self.released), ("length", self.length), ("BPM", self.bpm),
                        ("key", self.key), ("ISRC", self.isrc)):
            if v:
                bits.append(f"{name}: {v}")
        return f"{self.url} | " + " | ".join(bits)


@dataclass
class Result:
    candidates: list[Candidate] = field(default_factory=list)
    status: int = 0
    found_json: bool = False
    error: str = ""


def _names(v) -> str:
    if isinstance(v, list):
        return ", ".join(filter(None, (_names(x) for x in v)))
    if isinstance(v, dict):
        return str(v.get("name") or v.get("artist_name") or v.get("label_name") or "")
    return str(v or "")


def _length(d: dict) -> str:
    if d.get("length"):
        return str(d["length"])
    ms = d.get("length_ms") or d.get("duration_ms")
    if isinstance(ms, (int, float)) and ms > 0:
        s = int(ms // 1000)
        return f"{s // 60}:{s % 60:02d}"
    return ""


def _as_candidate(d: dict) -> Candidate | None:
    """A dict from Beatport's page data that describes a track, as a Candidate."""
    tid = d.get("track_id") or d.get("id")
    title = d.get("track_name") or d.get("name")
    if not tid or not title or not str(tid).isdigit():
        return None
    if not any(k in d for k in ("mix_name", "artists", "isrc", "bpm", "track_name")):
        return None  # probably a release, label or artist, not a track
    slug = d.get("slug") or d.get("track_slug") or re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    release = d.get("release") if isinstance(d.get("release"), dict) else {}
    key = d.get("key")
    return Candidate(
        url=f"https://www.beatport.com/track/{slug}/{tid}",
        title=str(title),
        mix=str(d.get("mix_name") or ""),
        artists=_names(d.get("artists")),
        remixers=_names(d.get("remixers")),
        label=_names(d.get("label") or release.get("label")),
        release=str(release.get("name") or d.get("release_name") or ""),
        released=str(d.get("publish_date") or d.get("new_release_date") or d.get("release_date") or ""),
        length=_length(d),
        bpm=str(d.get("bpm") or ""),
        key=(key.get("name", "") if isinstance(key, dict) else str(key or d.get("key_name") or "")),
        isrc=str(d.get("isrc") or ""),
    )


def _walk(node, out: dict[str, Candidate]) -> None:
    if isinstance(node, dict):
        c = _as_candidate(node)
        if c and c.url not in out:
            out[c.url] = c
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def parse(html: str) -> tuple[list[Candidate], bool]:
    """Tracks in a Beatport search page. Returns (candidates, whether page JSON was found)."""
    found: dict[str, Candidate] = {}
    m = _NEXT_DATA.search(html)
    if m:
        try:
            _walk(json.loads(m.group(1)), found)
        except json.JSONDecodeError:
            pass
    if not found:
        for path, slug, tid, text in _TRACK_LINK.findall(html):
            url = f"https://www.beatport.com/track/{slug}/{tid}"
            label = _TAGS.sub(" ", text).strip()
            if url not in found:
                found[url] = Candidate(url=url, title=re.sub(r"\s+", " ", label) or slug.replace("-", " "))
    return list(found.values()), bool(m)


def query_for(artist: str, title: str) -> str:
    from .stores import search_query

    return search_query(artist, title)


def search(artist: str, title: str, opener=urllib.request.urlopen, limit: int = 15) -> Result:
    url = SEARCH_URL.format(q=urllib.parse.quote_plus(query_for(artist, title)))
    res = Result()
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with opener(req, timeout=20) as r:
            res.status = getattr(r, "status", 200)
            html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        res.status, res.error = e.code, f"Beatport returned HTTP {e.code}"
        return res
    except Exception as e:
        res.error = f"Couldn't reach Beatport: {e}"
        return res
    res.candidates, res.found_json = parse(html)
    res.candidates = res.candidates[:limit]
    if not res.candidates:
        res.error = "Beatport's page loaded but no tracks could be read from it."
    return res
