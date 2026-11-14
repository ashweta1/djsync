"""Research agent: find the best place to buy each approved track.

For each track, Claude (with Anthropic's server-side web search):
  1. searches Beatport first and compares the results against the Spotify track
     (artist, title, remix/version, length, ISRC) to pick the best match;
  2. if Beatport doesn't have it, searches the wider web for a legitimate store
     that sells the download (Bandcamp, Traxsource, Beatsource, Apple, Amazon, ...)
     or an official free download from the artist/label.

Every link must have appeared in the search results and be on a known store
(see STORES below); anything else is thrown away. As a last resort, the Deezer link
is looked up with Deezer's public API (by ISRC, then artist + title), so there's
always somewhere to listen and check.

Needs Anthropic API credentials: ANTHROPIC_API_KEY, or a profile from
`ant auth login`.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .matching import TrackKey, similar
from .state import Track

BEATPORT_TRACK = re.compile(r"^https://(?:www\.)?beatport\.com/track/[^/?#]+/\d+/?$")

# Legitimate places to buy (or officially free-download) a track, by domain.
STORES: dict[str, str] = {
    "beatport.com": "Beatport",
    "beatsource.com": "Beatsource",
    "bandcamp.com": "Bandcamp",          # also <artist>.bandcamp.com
    "traxsource.com": "Traxsource",
    "music.apple.com": "Apple Music / iTunes",
    "itunes.apple.com": "iTunes",
    "amazon.com": "Amazon",
    "amazon.co.uk": "Amazon",
    "amazon.de": "Amazon",
    "qobuz.com": "Qobuz",
    "7digital.com": "7digital",
    "boomkat.com": "Boomkat",
    "bleep.com": "Bleep",
    "hardwax.com": "Hard Wax",
    "whatpeopleplay.com": "WhatPeoplePlay",
    "soundcloud.com": "SoundCloud (free download)",
    "hypeddit.com": "Hypeddit (free download)",
    "toneden.io": "ToneDen (free download)",
}


def store_for(url: str) -> str:
    """'beatport', 'bandcamp.com', ... for an allowed URL, else ''."""
    host = urllib.parse.urlparse(url).hostname or ""
    host = host.removeprefix("www.")
    for domain in STORES:
        if host == domain or host.endswith("." + domain):
            return domain
    return ""


SYSTEM = """\
You help a DJ buy tracks. Given a track from their Spotify likes, find the best \
page to buy (download) that exact track.

Order of search:
1. Beatport first. Search for the track on Beatport (include "beatport" in the \
query). Beatport track pages look like https://www.beatport.com/track/<slug>/<id>.
2. Only if Beatport clearly doesn't have it, search the web for another \
legitimate store selling the download: Bandcamp, Beatsource, Traxsource, Apple \
Music/iTunes, Amazon, Qobuz, 7digital, Boomkat, Bleep - or an official free \
download the artist or label offers on SoundCloud or Hypeddit. Never suggest \
pirate sites, rip/converter tools, "free mp3" sites or file hosts.

How to judge a match:
- The artist(s) and the track title must match. Ignore "feat." credits and \
punctuation differences.
- The version matters. A remix by a specific artist (e.g. "Hammer Remix") must be \
that remix. If Spotify has a radio edit, single version or no version name, the \
"Original Mix" or "Extended Mix" of the same track is the right match - DJs \
prefer the longer version, so pick Extended Mix when both exist.
- Use the ISRC, label and length to break ties when the results show them.
- Only give a URL that appeared in your search results, pointing at that \
track's own page (or the release page that contains it). Never construct or \
guess a URL.
- If nothing is clearly the same track, set found to false. A wrong match costs \
the DJ money; no match just sends them elsewhere.

Use at most five searches. Then answer with only a JSON object with the keys \
found, store, url, title, mix, artists, confidence (high, medium or low) and \
reason."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "store": {"type": "string", "description": "Store name, e.g. Beatport, Bandcamp"},
        "url": {"type": "string", "description": "Track page URL, or empty"},
        "title": {"type": "string", "description": "Track title on the store"},
        "mix": {"type": "string", "description": "Mix/version name, e.g. Extended Mix"},
        "artists": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string", "description": "One short sentence for the DJ"},
    },
    "required": ["found", "store", "url", "title", "mix", "artists", "confidence", "reason"],
    "additionalProperties": False,
}


@dataclass
class Finding:
    store: str = ""        # store domain ("beatport.com", "bandcamp.com", ...) or "deezer"
    store_name: str = ""   # for display: "Beatport", "Bandcamp", ...
    url: str = ""
    label: str = ""        # e.g. "Glue (Extended Mix) - Bicep"
    confidence: str = ""   # high | medium | low
    note: str = ""
    deezer_url: str = ""
    error: str = ""


def _describe(t: Track) -> str:
    lines = [f"Artist(s): {t.artist}", f"Title: {t.title}"]
    if t.album:
        lines.append(f"Album/release: {t.album}")
    if t.isrc:
        lines.append(f"ISRC: {t.isrc}")
    if t.duration_ms:
        s = t.duration_ms // 1000
        lines.append(f"Length on Spotify: {s // 60}:{s % 60:02d}")
    return "Find where to buy this track:\n" + "\n".join(lines)


def _norm_url(u: str) -> str:
    u = u.strip().split("#")[0].rstrip("/")
    if "beatport.com" in u:  # Beatport URLs carry tracking query strings
        u = u.split("?")[0].replace("://beatport.com", "://www.beatport.com")
    return u


def _search_result_urls(content) -> set[str]:
    urls = set()
    for block in content:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        results = getattr(block, "content", None)
        if isinstance(results, list):  # an error result is an object, not a list
            for r in results:
                if getattr(r, "url", ""):
                    urls.add(_norm_url(r.url))
    return urls


def _final_json(content) -> dict | None:
    texts = [b.text for b in content if getattr(b, "type", "") == "text" and b.text.strip()]
    for text in reversed(texts):
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    return None


def ask_claude(cfg: Config, t: Track, client=None) -> tuple[dict | None, set[str]]:
    """Run the Beatport search. Returns (answer, urls seen in search results)."""
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    messages = [{"role": "user", "content": _describe(t)}]
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 5,
    }]
    seen: set[str] = set()
    content: list = []
    use_schema = True
    for _ in range(5):  # first request + pause_turn continuations
        output_config = {"effort": cfg.agent_effort}
        if use_schema:
            output_config["format"] = {"type": "json_schema", "schema": RESULT_SCHEMA}
        try:
            response = client.beta.messages.create(
                model=cfg.agent_model,
                max_tokens=16000,
                system=SYSTEM,
                messages=messages,
                tools=tools,
                output_config=output_config,
                # If the model declines, retry the same request on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as e:
            # Some API versions don't allow a JSON schema together with web search
            # citations; the prompt already asks for JSON, so retry without it.
            if use_schema and getattr(e, "status_code", None) == 400:
                use_schema = False
                continue
            raise
        content = list(response.content)
        seen |= _search_result_urls(content)
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    return _final_json(content), seen


def deezer_link(t: Track, opener=urllib.request.urlopen) -> str:
    """Find the track on Deezer via its public API (no account needed)."""
    def get(url: str) -> dict:
        with opener(url, timeout=15) as r:
            return json.loads(r.read().decode())

    want = TrackKey.of(t.artist, t.title)
    try:
        if t.isrc:
            d = get(f"https://api.deezer.com/2.0/track/isrc:{urllib.parse.quote(t.isrc)}")
            if d.get("link") and not d.get("error"):
                return d["link"]
        first_artist = t.artist.split(",")[0].strip()
        q = urllib.parse.quote(f'artist:"{first_artist}" track:"{t.title}"')
        for item in get(f"https://api.deezer.com/search?q={q}&limit=10").get("data", []):
            got = TrackKey.of(item.get("artist", {}).get("name", ""), item.get("title", ""))
            if similar(want, got, 80) and item.get("link"):
                return item["link"]
    except Exception:
        return ""
    return ""


def research(cfg: Config, t: Track, client=None, opener=urllib.request.urlopen) -> Finding:
    f = Finding()
    try:
        answer, seen = ask_claude(cfg, t, client)
    except Exception as e:  # no credentials, network, API error: report, don't crash
        answer, seen = None, set()
        f.error = f"Store search failed: {e}"
    if answer and answer.get("found"):
        url = _norm_url(answer.get("url", ""))
        domain = store_for(url)
        seen_pages = {u.split("?")[0] for u in seen}
        ok = bool(domain) and url.split("?")[0] in seen_pages
        if domain == "beatport.com":
            ok = ok and bool(BEATPORT_TRACK.match(url))
        if ok:
            mix = answer.get("mix", "").strip()
            title = answer.get("title", "").strip() or t.title
            if mix and mix.lower() not in title.lower():
                title = f"{title} ({mix})"
            f.store, f.store_name, f.url = domain, STORES[domain], url
            f.label = f"{title} - {answer.get('artists', '').strip() or t.artist}"
            f.confidence = answer.get("confidence", "medium")
            f.note = answer.get("reason", "")
        else:
            f.note = "The suggested link wasn't a known store page from the search results, so it was ignored."
    elif answer and not f.error:
        f.note = answer.get("reason", "") or "Not found in any store."

    f.deezer_url = deezer_link(t, opener)
    if not f.store and f.deezer_url:
        f.store, f.store_name, f.url = "deezer", "Deezer", f.deezer_url
        f.label = f"{t.title} - {t.artist}"
    return f


def apply(t: Track, f: Finding) -> None:
    t.buy_store, t.buy_store_name, t.buy_url, t.buy_label = f.store, f.store_name, f.url, f.label
    t.buy_confidence, t.buy_note, t.deezer_url = f.confidence, f.note, f.deezer_url
    t.research_error = f.error
    t.researched_at = datetime.now().isoformat(timespec="seconds")


def research_many(cfg: Config, tracks: list[Track], client=None, workers: int = 4,
                  on_done=None) -> None:
    """Research several tracks in parallel. ``on_done(track, finding)`` is called for each."""
    def one(t: Track):
        f = research(cfg, t, client)
        if on_done:
            on_done(t, f)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, tracks))
