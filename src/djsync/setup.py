"""First-run checks: is everything djsync needs set up on this computer?

Each check says whether it's done and, if not, what to do. They're cheap enough
to run when the setup page loads (the rekordbox one opens the library once).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .config import Config


@dataclass
class Check:
    key: str
    title: str
    ok: bool
    detail: str = ""
    optional: bool = False


def spotify_app(cfg: Config) -> Check:
    if cfg.client_id:
        return Check("spotify_app", "Spotify app", True, f"Client ID {cfg.client_id[:6]}…")
    return Check("spotify_app", "Spotify app", False,
                 "Create a free Spotify developer app and paste its Client ID below.")


def spotify_login(cfg: Config) -> Check:
    if cfg.spotify_cache.exists():
        return Check("spotify_login", "Spotify login", True, "Connected.")
    return Check("spotify_login", "Spotify login", False,
                 "Connect djsync to your Spotify account (read-only).")


def rekordbox_library(cfg: Config, read=None) -> Check:
    from . import rekordbox

    read = read or rekordbox.read_library
    try:
        n = len(read(cfg))
    except Exception as e:
        msg = str(e)
        hint = ""
        low = msg.lower()
        if "key" in low or "sqlcipher" in low or "encrypt" in low or "not a database" in low:
            hint = " rekordbox 7 needs a one-time key download: click Fix below."
        elif "no rekordbox" in low or "not found" in low or "does not exist" in low:
            hint = " Is rekordbox installed, and have you opened it at least once?"
        return Check("rekordbox", "rekordbox library", False, f"Couldn't open it: {msg}.{hint}")
    return Check("rekordbox", "rekordbox library", True, f"Found {n:,} tracks.")


def folders(cfg: Config) -> Check:
    missing = [d for d in cfg.search_dirs if not d.exists()]
    if not cfg.watch_dir.exists():
        return Check("folders", "Folders", False, f"Downloads folder {cfg.watch_dir} doesn't exist.")
    note = f" ({', '.join(str(d) for d in missing)} not found, skipped)" if missing else ""
    return Check("folders", "Folders", True,
                 f"Looking in {', '.join(str(d) for d in cfg.search_dirs if d.exists())}; "
                 f"imports go to {cfg.library_dir}.{note}")


def research_agent(cfg: Config, which=shutil.which, run=subprocess.run) -> Check:
    from . import research

    if not cfg.agent_enabled:
        return Check("agent", "Research agent", True, "Turned off in Settings.", optional=True)
    if research.backend(cfg) == "api":
        ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        return Check("agent", "Research agent", ok,
                     "Using the Claude API key." if ok else
                     "Set to use the Claude API, but ANTHROPIC_API_KEY isn't set.", optional=True)
    exe = which(cfg.claude_path or "claude")
    if not exe:
        return Check("agent", "Research agent", False,
                     "Install Claude Code (comes with Claude Pro/Max) to find the best place to buy "
                     "each track. Optional: without it you get plain search links.", optional=True)
    try:
        v = run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        v = ""
    return Check("agent", "Research agent", True,
                 f"Claude Code {v or 'found'}. If it says 'Not logged in', run `claude` once and type /login.",
                 optional=True)


def all_checks(cfg: Config, read=None) -> list[Check]:
    return [spotify_app(cfg), spotify_login(cfg), rekordbox_library(cfg, read), folders(cfg),
            research_agent(cfg)]


def needs_setup(cfg: Config) -> bool:
    """Cheap check for the board's 'finish setting up' bar (no rekordbox access)."""
    return not cfg.client_id or not cfg.spotify_cache.exists()
