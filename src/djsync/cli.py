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


def cmd_import(cfg, args) -> int:
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
            print(f"{len(r.waiting)} approved tracks not found yet in {cfg.watch_dir}.")
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
    i = sub.add_parser("import", help="import purchased files into a downloads-<date> playlist")
    i.add_argument("--dry-run", action="store_true", help="only show which files match")
    i.add_argument("--watch", action="store_true", help="keep checking until all approved are imported")
    i.add_argument("--interval", type=int, default=30, help="seconds between checks with --watch")
    args = p.parse_args(argv)

    cfg = config.load()
    handler = {"init": cmd_init, "scan": cmd_scan, "review": cmd_review, "import": cmd_import}
    try:
        return handler[args.cmd](cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
