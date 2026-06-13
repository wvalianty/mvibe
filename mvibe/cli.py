"""mvibe command line.

  mvibe run [-- claude args...]     launch the wrapped Claude TUI (local-identical)
  mvibe bridge [--cwd DIR] [...]    run the remote mirror + inbound HTTP receiver
  mvibe send "text"                 inject keystrokes into the live session
  mvibe flag [local|remote]         get/set the routing flag
  mvibe status                      show flag + active transcript
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import paths, wrapper


def _cmd_run(args: argparse.Namespace) -> int:
    import os

    if args.cwd:
        os.chdir(args.cwd)  # claude's session dir is keyed by cwd; match the bridge
    cmd = args.cmd or ["claude"]
    return wrapper.run(cmd)


def _cmd_bridge(args: argparse.Namespace) -> int:
    from . import bridge

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    return bridge.serve(
        cwd,
        host=args.host,
        port=args.port,
        always=args.always,
        user=args.user,
        wechat=not args.no_wechat,
    )


def _cmd_login(_args: argparse.Namespace) -> int:
    from . import wechat_login

    return wechat_login.login()


def _cmd_send(args: argparse.Namespace) -> int:
    text = args.text if args.text is not None else sys.stdin.read()
    if args.remote:
        paths.write_flag("remote")
    try:
        wrapper.inject(text.rstrip("\n"), submit=not args.no_submit)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def _cmd_flag(args: argparse.Namespace) -> int:
    if args.mode:
        paths.write_flag(args.mode)
    print(paths.read_flag())
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    t = paths.newest_transcript(cwd)
    print(f"flag:       {paths.read_flag()}")
    print(f"home:       {paths.MVIBE_HOME}")
    print(f"inject:     {paths.INJECT_FIFO} ({'present' if paths.INJECT_FIFO.exists() else 'absent'})")
    print(f"transcript: {t or '(none)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mvibe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="launch the wrapped Claude TUI")
    p_run.add_argument("--cwd", help="chdir here before launching (match the bridge's --cwd)")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER,
                       help="command to run (default: claude); prefix with --")
    p_run.set_defaults(func=_cmd_run)

    p_bridge = sub.add_parser("bridge", help="remote mirror + inbound receiver")
    p_bridge.add_argument("--cwd", help="session cwd to mirror (default: current)")
    p_bridge.add_argument("--host", default="127.0.0.1")
    p_bridge.add_argument("--port", type=int, default=8765)
    p_bridge.add_argument("--always", action="store_true",
                          help="push output even in local mode")
    p_bridge.add_argument("--user", help="target WeChat user_id (default: most recent)")
    p_bridge.add_argument("--no-wechat", action="store_true",
                          help="disable the WeChat inbound poll loop (HTTP only)")
    p_bridge.set_defaults(func=_cmd_bridge)

    p_login = sub.add_parser("login", help="bind a WeChat bot via QR (writes mvibe config)")
    p_login.set_defaults(func=_cmd_login)

    p_send = sub.add_parser("send", help="inject text into the live session")
    p_send.add_argument("text", nargs="?", help="text to inject; omit to read stdin")
    p_send.add_argument("--remote", action="store_true", help="also set flag=remote")
    p_send.add_argument("--no-submit", action="store_true", help="do not append CR")
    p_send.set_defaults(func=_cmd_send)

    p_flag = sub.add_parser("flag", help="get/set routing flag")
    p_flag.add_argument("mode", nargs="?", choices=["local", "remote"])
    p_flag.set_defaults(func=_cmd_flag)

    p_status = sub.add_parser("status", help="show flag + active transcript")
    p_status.add_argument("--cwd")
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    # REMAINDER keeps a leading "--"; strip it so `mvibe run -- claude` works.
    if getattr(args, "cmd", None) and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
