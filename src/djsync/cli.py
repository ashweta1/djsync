"""Command line: djsync init | scan | review | import."""

from __future__ import annotations

import argparse
import sys
import time

from . import config, core
from .state import APPROVED, PENDING, State


def cmd_init(cfg: config.Config, args) -> int:
    if config.write_default(config.CONFIG_PATH):
        print(f"Wrote {config.CONFIG_PATH}. Add your Spotify client_id, then run: djsync review")
    else:
        print(f"{config.CONFIG_PATH} already exists.")
    return 0


def cmd_scan(cfg, args) -> int:
    st = State.load(cfg.state_path)
    r = core.scan(cfg, st)
    print(f"Checked {r.liked_checked} liked songs: {r.new_missing} new, "
          f"{r.already_owned} already in rekordbox.")
    pending = st.by_status(PENDING)
    for t in pending:
        print(f"  - {t.artist} - {t.title}")
    if pending:
        print("Run `djsync review` to choose which to buy.")
    return 0


def cmd_review(cfg, args) -> int:
    from .web import serve

    if args.scan:
        cmd_scan(cfg, args)
    serve(cfg, open_browser=not args.no_browser)
    return 0


def cmd_research(cfg, args) -> int:
    from . import research

    st = State.load(cfg.state_path)
    _, waiting = core.match_files(cfg, st)
    todo = [t for t in waiting if args.again or not t.researched_at]
    if not todo:
        print("Nothing to research: every approved track without a file already has a link.")
        return 0
    print(f"Researching {len(todo)} track(s)...")

    def done(track, f):
        s = State.load(cfg.state_path)
        research.apply(s.tracks[track.id], f)
        s.save()
        where = f"{f.store_name}: {f.url}" if f.url else "not found"
        print(f"  {track.artist} - {track.title}\n      {where}")
        if f.error:
            print(f"      ({f.error})")

    research.research_many(cfg, todo, on_done=done)
    return 0


def cmd_retry(cfg) -> int:
    st = State.load(cfg.state_path)
    targets = core.retry_targets(st)
    if not targets:
        print("Every imported playlist is already in rekordbox.")
        return 0
    failed = False
    for name in targets:
        r = core.retry_db(cfg, st, name)
        if r.error:
            print(f"{name}: {r.error}", file=sys.stderr)
            failed = True
        else:
            print(f"{name}: added {r.added} tracks to rekordbox.")
            for t in r.missing:
                print(f"  missing file, left out: {t.artist} - {t.title} ({t.file})")
    return 1 if failed else 0


def cmd_import(cfg, args) -> int:
    if args.retry:
        return cmd_retry(cfg)
    while True:
        st = State.load(cfg.state_path)
        r = core.do_import(cfg, st, dry_run=args.dry_run)
        for t, p in r.matched:
            print(f"  ✓ {t.artist} - {t.title}  <-  {p}")
        if r.error:
            print(r.error, file=sys.stderr)
        if r.method == "db":
            print(f"Added {len(r.matched)} tracks to rekordbox playlist '{r.playlist}'.")
        elif r.method == "xml":
            print(f"Wrote {r.xml_path} (playlist '{r.playlist}'). Import it in rekordbox via "
                  "Preferences > Advanced > Database > rekordbox xml.")
        if r.waiting:
            dirs = ", ".join(str(d) for d in cfg.search_dirs)
            print(f"{len(r.waiting)} approved tracks not found yet in {dirs}.")
        if not args.watch or not State.load(cfg.state_path).by_status(APPROVED):
            return 1 if r.error and not r.method else 0
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="djsync", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create ~/.djsync/config.toml")
    sub.add_parser("scan", help="find liked songs missing from rekordbox")
    r = sub.add_parser("review", help="open the review page in your browser")
    r.add_argument("--scan", action="store_true", help="scan Spotify first")
    r.add_argument("--no-browser", action="store_true")
    rs = sub.add_parser("research", help="find the best place to buy each approved track")
    rs.add_argument("--again", action="store_true", help="also redo tracks already researched")
    i = sub.add_parser("import", help="import purchased files into today's djsync-downloads-<date> playlist")
    i.add_argument("--dry-run", action="store_true", help="only show which files match")
    i.add_argument("--retry", action="store_true",
                   help="add playlists that were only saved as XML (rekordbox was open) into rekordbox")
    i.add_argument("--watch", action="store_true", help="keep checking until all approved are imported")
    i.add_argument("--interval", type=int, default=30, help="seconds between checks with --watch")
    args = p.parse_args(argv)

    cfg = config.load()
    handler = {"init": cmd_init, "scan": cmd_scan, "review": cmd_review, "import": cmd_import,
               "research": cmd_research}
    try:
        return handler[args.cmd](cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
