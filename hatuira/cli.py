"""hatuira — Home Assistant lights, switches and covers in the terminal."""

import argparse
import sys
from urllib.parse import urlsplit

from . import app, config, sync, wizard
from .hass import ApiError, Demo, Hass
from .model import DOMAINS
from .theme import Theme


def connect(cfg, timeout=10):
    return Hass(cfg["url"], config.token(cfg), timeout, cfg.get("insecure", False))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hatuira", description=__doc__)
    ap.add_argument("command", nargs="?", default="run",
                    choices=("run", "setup", "sync", "toggle"))
    ap.add_argument("entity", nargs="?", help="entity id for `toggle`, e.g. light.desk")
    ap.add_argument("--demo", action="store_true", help="a made-up home, no network or token")
    ap.add_argument("--snapshot", action="store_true", help="print one frame and exit")
    ap.add_argument("--size", default="80x24", help="frame size for --snapshot")
    ap.add_argument("--colour", action="store_true", help="keep colours in --snapshot")
    args = ap.parse_args(argv)

    cfg = config.load()
    if args.command == "setup":
        return 0 if wizard.setup(cfg) else 1

    if args.demo:
        client, save, host = Demo(), (lambda _: None), "demo"
        cfg.update(hidden=[], shown=[], sync=["light.tv_strip"])
    else:
        interactive = args.command == "run" and not args.snapshot and sys.stdin.isatty()
        if not cfg["url"] and not (interactive and wizard.first_run(cfg)):
            if args.command == "sync":  # the theme hook runs this: stay quiet until set up
                return 0
            print("not connected — run `hatuira setup`", file=sys.stderr)
            return 1
        try:
            client = connect(cfg, timeout=5 if args.command == "sync" else 10)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1
        save, host = config.save, urlsplit(cfg["url"]).netloc

    try:
        if args.command == "sync":
            lit = sync.apply(client, cfg, Theme())
            print(f"{len(lit)} of {len(cfg['sync'])} synced lights are on and took the theme colour")
            return 0
        if args.command == "toggle":
            domain = (args.entity or "").split(".", 1)[0]
            if domain not in DOMAINS or "." not in args.entity:
                print("usage: hatuira toggle <light|switch|cover>.<name>", file=sys.stderr)
                return 1
            client.call(domain, "toggle", {"entity_id": args.entity})
            return 0
        if args.snapshot:
            w, h = (int(v) for v in args.size.lower().split("x"))
            app.snapshot(client, cfg, (w, h), args.colour, host)
            return 0
    except ApiError as e:
        print(f"Home Assistant: {e}", file=sys.stderr)
        return 1

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("hatuira needs a terminal", file=sys.stderr)
        return 1
    try:
        app.run(client, cfg, save, host)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
