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

Two ways to run it (`[agent] backend` in the config):
  * "claude-code" - runs the `claude` command (Claude Code) on this Mac, so it uses
    your Claude Pro/Max plan's allowance. No API key needed.
  * "api"         - calls the Claude API directly with ANTHROPIC_API_KEY (pay per use).
  * "auto"        - Claude Code if it's installed, else the API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .matching import TrackKey, similar
from .state import Track

# Beatport track or release page -> ("track"|"release", id). The slug part varies.
BEATPORT_PAGE = re.compile(r"^https://www\.beatport\.com/(?:[a-z]{2}/)?(track|release)/[^/?#]*/(\d+)")
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
1. Beatport first. Search with a query like "site:beatport.com <artist> <title>", \
and try once more with "beatport" plus the remix or label name if needed. \
Beatport track pages look like https://www.beatport.com/track/<slug>/<id>; give \
the track page if it's in your results, otherwise the release page \
(https://www.beatport.com/release/<slug>/<id>) that contains the track.
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
    meta: str = ""         # details of the Beatport pick: label · date · length · BPM · key
    alts: list = field(default_factory=list)  # other Beatport versions: [{url, label, meta}]


def _describe(t: Track, cands=None) -> str:
    lines = [f"Artist(s): {t.artist}", f"Title: {t.title}"]
    if t.album:
        lines.append(f"Album/release: {t.album}")
    if t.isrc:
        lines.append(f"ISRC: {t.isrc}")
    if t.duration_ms:
        s = t.duration_ms // 1000
        lines.append(f"Length on Spotify: {s // 60}:{s % 60:02d}")
    text = "Find where to buy this track:\n" + "\n".join(lines)
    if cands:
        text += ("\n\nBeatport's own search (its live catalogue, newer than any web search "
                 "index) returned these tracks:\n" + "\n".join(f"- {c.describe()}" for c in cands)
                 + "\n\nIf one of these is the track, answer with its URL - you don't need to "
                 "search. Only search the web if none of them is the right track.")
    return text


_BEATPORT_HOST = re.compile(r"^https?://(?:[a-z0-9-]+\.)*beatport\.com(?=/|$)", re.I)


def _norm_url(u: str) -> str:
    u = u.strip().split("#")[0].rstrip("/")
    if _BEATPORT_HOST.match(u):
        # classic.beatport.com, http://, beatport.com and tracking query strings all
        # point at the same page: use one form.
        u = _BEATPORT_HOST.sub("https://www.beatport.com", u.split("?")[0])
        # /fr/track/..., /ja/release/... -> the default-language page
        u = re.sub(r"^(https://www\.beatport\.com)/[a-z]{2}(?=/(?:track|release|artist|label)/)", r"\1", u)
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


def ask_claude(cfg: Config, t: Track, client=None, cands=None) -> tuple[dict | None, set[str]]:
    """Run the Beatport search. Returns (answer, urls seen in search results)."""
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    messages = [{"role": "user", "content": _describe(t, cands)}]
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


_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


# Where Claude Code's installers put the `claude` command. Started from the Desktop
# launcher, djsync doesn't get your Terminal's PATH, so look in these too.
CLAUDE_PLACES = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.bun/bin/claude",
)


def find_claude(cfg: Config, which=None) -> str | None:
    """Full path to Claude Code's `claude` command, or None if it isn't installed."""
    which = which or shutil.which
    if cfg.claude_path:
        p = os.path.expanduser(cfg.claude_path)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else which(cfg.claude_path)
    found = which("claude")
    if found:
        return found
    for place in CLAUDE_PLACES:
        p = os.path.expanduser(place)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def backend(cfg: Config) -> str:
    if cfg.agent_backend in ("api", "claude-code"):
        return cfg.agent_backend
    if find_claude(cfg):
        return "claude-code"
    return "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-code"


def _tool_text(content) -> str:
    """Plain text of a tool result, whether it's a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_tool_text(b.get("text", b.get("content", ""))) if isinstance(b, dict)
                         else str(b) for b in content)
    if isinstance(content, dict):
        return _tool_text(content.get("text", content.get("content", "")))
    return ""


def ask_claude_code(cfg: Config, t: Track, run=subprocess.run, cands=None) -> tuple[dict | None, set[str]]:
    """Same search, run through Claude Code (`claude -p`) on your Claude plan."""
    exe = find_claude(cfg) or cfg.claude_path or "claude"
    cmd = [
        exe, "-p", _describe(t, cands) + ("\n\nGive your answer." if cands else
                                           "\n\nUse the web search tool, then give your answer."),
        "--append-system-prompt", SYSTEM,
        "--tools", "WebSearch", "--allowedTools", "WebSearch",
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--json-schema", json.dumps(RESULT_SCHEMA),
    ]
    if cfg.claude_code_model:
        cmd += ["--model", cfg.claude_code_model]
    cfg.home.mkdir(parents=True, exist_ok=True)

    def call(c):
        try:
            # Run from djsync's own folder so no project's CLAUDE.md or settings get loaded.
            return run(c, capture_output=True, text=True, timeout=300, cwd=str(cfg.home))
        except FileNotFoundError:
            raise RuntimeError("Claude Code isn't installed (no `claude` command found). Install it "
                               "from https://claude.com/claude-code and sign in by running `claude` "
                               "once, or set claude_path under [agent] in ~/.djsync/config.toml.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Claude Code took more than 5 minutes; try again.")

    proc = call(cmd)
    if "unknown option" in (proc.stderr or "").lower():
        # Older Claude Code: drop the newer flags; the prompt still asks for JSON.
        newer = {"--tools": 1, "--json-schema": 1, "--no-session-persistence": 0}
        legacy, skip = [], 0
        for i, a in enumerate(cmd):
            if skip:
                skip -= 1
                continue
            if a in newer:
                skip = newer[a]
                continue
            legacy.append(a)
        proc = call(legacy)

    seen: set[str] = set()
    result = None
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "user":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result":
                    continue
                seen |= {_norm_url(u) for u in _URL.findall(_tool_text(block.get("content")))}
    if result is None:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError("Claude Code didn't return a result" + (f": {err[-1]}" if err else ".")
                           + " Run `claude` in Terminal once to check it's installed and signed in.")
    if result.get("is_error") or result.get("subtype") != "success":
        msg = str(result.get("result") or result.get("subtype") or "unknown error")
        low = msg.lower()
        if "limit" in low:
            msg = f"Claude plan usage limit reached; try again later. ({msg})"
        elif "login" in low or "api key" in low or "auth" in low:
            msg = f"Claude Code isn't signed in. Run `claude` in Terminal and log in. ({msg})"
        raise RuntimeError(f"Claude Code: {msg}")
    answer = result.get("structured_output")
    if not isinstance(answer, dict):
        answer = _final_json([type("B", (), {"type": "text", "text": result.get("result") or ""})()])
    return answer, seen


def ask(cfg: Config, t: Track, client=None, cands=None) -> tuple[dict | None, set[str]]:
    if client is None and backend(cfg) == "claude-code":
        return ask_claude_code(cfg, t, cands=cands)
    return ask_claude(cfg, t, client, cands=cands)


def _by_name(t: Track, cands) -> list:
    """Beatport results with the same artist and title (any version)."""
    want = TrackKey.of(t.artist, t.title)
    return [c for c in cands if c.artists and similar(want, TrackKey.of(c.artists, c.title))]


def deezer_link(t: Track, opener=None) -> str:
    """Find the track on Deezer via its public API (no account needed)."""
    from .net import urlopen

    opener = opener or urlopen
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


def _take(f: Finding, c, confidence: str, note: str, t: Track) -> Finding:
    title = c.title + (f" ({c.mix})" if c.mix and c.mix.lower() not in c.title.lower() else "")
    f.store, f.store_name, f.url = "beatport.com", "Beatport", _norm_url(c.url)
    f.label, f.confidence, f.note = f"{title} - {c.artists or t.artist}", confidence, note
    return f


def _beatport_id(url: str):
    m = BEATPORT_PAGE.match(_norm_url(url or ""))
    return m.groups() if m else None


def _enrich(f: Finding, t: Track, cands) -> None:
    """Add the Beatport pick's details and the other versions Beatport listed."""
    if not cands:
        return
    picked = _beatport_id(f.url) if f.store == "beatport.com" else None
    for c in cands:
        if picked and _beatport_id(c.url) == picked:
            f.meta = c.meta()
    others = [c for c in _by_name(t, cands) if _beatport_id(c.url) != picked]
    f.alts = [{"url": _norm_url(c.url), "label": c.mix or c.title, "meta": c.meta()}
              for c in others[:4]]


def research(cfg: Config, t: Track, client=None, opener=None) -> Finding:
    from .net import urlopen

    opener = opener or urlopen
    f = Finding()

    # 1. Beatport's own search (its live catalogue).
    cands = []
    if cfg.beatport_direct:
        from . import beatport

        bp = beatport.search(t.artist, t.title, opener)
        cands = bp.candidates
        if t.isrc:
            same = [c for c in cands if c.isrc and c.isrc.upper() == t.isrc.upper()]
            if same:
                # Same recording (ISRC) on Beatport: no need to ask Claude.
                _take(f, same[0], "high", "Same recording as on Spotify (matching ISRC), found "
                      "with Beatport's own search.", t)
                _enrich(f, t, cands)
                f.deezer_url = deezer_link(t, opener)
                return f

    # 2. Claude picks the right version from those results, or searches the web.
    try:
        answer, seen = ask(cfg, t, client, cands=cands)
        seen = seen | {_norm_url(c.url) for c in cands}
    except Exception as e:  # no credentials, network, API error: report, don't crash
        answer, seen = None, set()
        f.error = f"Store search failed: {e}"
        named = _by_name(t, cands)
        if named:
            # Claude isn't available, but Beatport has a track with the same name.
            _take(f, named[0], "low", f"Picked by name from Beatport's search; check the version. ({e})", t)
            f.error = ""
    if answer and answer.get("found"):
        url = _norm_url(answer.get("url", ""))
        domain = store_for(url)
        seen_pages = {u.split("?")[0] for u in seen}
        ok = bool(domain) and url.split("?")[0] in seen_pages
        if domain == "beatport.com":
            # Beatport URLs are /track/<slug>/<id> or /release/<slug>/<id>; the slug
            # varies ("-", "glue", ...) and only the kind + ID matter.
            m = BEATPORT_PAGE.match(url)
            ok = bool(m) and m.groups() in {x.groups() for x in map(BEATPORT_PAGE.match, seen) if x}
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
            f.note = (f"The suggested link ({url or 'none'}) wasn't a known store page from "
                      "the search results, so it was ignored.")
    elif answer and not f.error:
        f.note = answer.get("reason", "") or "Not found in any store."
    elif not answer and not f.error and not f.store:
        f.error = "The store search didn't return an answer."

    _enrich(f, t, cands)
    f.deezer_url = deezer_link(t, opener)
    if not f.store and f.deezer_url:
        f.store, f.store_name, f.url = "deezer", "Deezer", f.deezer_url
        f.label = f"{t.title} - {t.artist}"
    return f


def apply(t: Track, f: Finding) -> None:
    t.buy_store, t.buy_store_name, t.buy_url, t.buy_label = f.store, f.store_name, f.url, f.label
    t.buy_confidence, t.buy_note, t.deezer_url = f.confidence, f.note, f.deezer_url
    t.research_error = f.error
    t.buy_meta, t.buy_alts = f.meta, list(f.alts)
    t.researched_at = datetime.now().isoformat(timespec="seconds")


def research_many(cfg: Config, tracks: list[Track], client=None, workers: int | None = None,
                  on_done=None) -> None:
    """Research several tracks in parallel. ``on_done(track, finding)`` is called for each."""
    def one(t: Track):
        f = research(cfg, t, client)
        if on_done:
            on_done(t, f)
    if workers is None:
        # Claude Code shares your plan's allowance: go easy on it.
        workers = 2 if client is None and backend(cfg) == "claude-code" else 4
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, tracks))
