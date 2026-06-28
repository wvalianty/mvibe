"""mvibe command line.

mvibe                             == `mvibe up` in the current directory
mvibe up [-- claude args...]      claude TUI + bridge (background) in one terminal
mvibe run [-- claude args...]     launch the wrapped Claude TUI (local-identical)
mvibe bridge [--cwd DIR] [...]    run the remote mirror + WeChat inbound poll
mvibe login                       bind a WeChat bot via QR
mvibe remote on|off|status        enable/disable remote takeover
mvibe send "text"                 inject keystrokes into the live session
mvibe flag [local|remote]         get/set the routing flag
mvibe status                      show flag + active transcript
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import paths, wrapper


def _claude_cmd(yolo: bool) -> list[str]:
    """Default claude launch. Confirmation prompts are forwarded to the phone
    (screen-scraped), so we keep them ON by default. `--yolo` bypasses them
    entirely. Explicit `-- <cmd>` overrides this."""
    cmd = ["claude"]
    if yolo:
        cmd.append("--dangerously-skip-permissions")
    return cmd


def _cmd_run(args: argparse.Namespace) -> int:
    import os

    if args.cwd:
        os.chdir(args.cwd)  # claude's session dir is keyed by cwd; match the bridge
    cmd = args.cmd or _claude_cmd(args.yolo)
    # Debug capture: with MVIBE_DEBUG_SCREEN set, attach the screen watcher (with a
    # no-op callback) so snapshots are dumped without starting the WeChat bridge.
    on_prompt = (lambda _p: None) if os.environ.get("MVIBE_DEBUG_SCREEN") else None
    return wrapper.run(cmd, on_prompt=on_prompt)


def _cmd_bridge(args: argparse.Namespace) -> int:
    from . import bridge

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    return bridge.serve(cwd, always=args.always, user=args.user, wechat=not args.no_wechat)


def _cmd_login(_args: argparse.Namespace) -> int:
    from . import wechat_login

    return wechat_login.login()


def _cmd_up(args: argparse.Namespace) -> int:
    """One terminal: bridge in background threads + claude TUI in foreground."""
    import os

    from . import bridge

    if args.cwd:
        os.chdir(args.cwd)
    cwd = Path.cwd()
    log_path = paths.MVIBE_HOME / "bridge.log"
    # Fresh session starts ready for remote unless explicitly disabled.
    paths.set_remote_enabled(not args.no_remote)

    def on_ready() -> None:
        bridge.start_background(
            cwd,
            always=args.always,
            user=args.user,
            wechat=not args.no_wechat,
            log_path=log_path,  # keep bridge logs off the TUI screen
        )

    cmd = args.cmd or _claude_cmd(args.yolo)
    return wrapper.run(cmd, on_ready=on_ready, on_prompt=bridge.on_prompt_change)


def _cmd_remote(args: argparse.Namespace) -> int:
    if args.action == "on":
        paths.set_remote_enabled(True)
        paths.write_flag("remote")
    elif args.action == "off":
        paths.set_remote_enabled(False)
        paths.write_flag("local")
    print(f"remote: {'on' if paths.remote_enabled() else 'off'} | flag: {paths.read_flag()}")
    return 0


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


def _cmd_keys(args: argparse.Namespace) -> int:
    if args.remote:
        paths.write_flag("remote")
    names = args.keys.split()
    try:
        wrapper.inject_keys(names, delay_ms=args.delay)
    except (FileNotFoundError, RuntimeError) as exc:
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
    print(
        f"inject:     {paths.INJECT_FIFO} ({'present' if paths.INJECT_FIFO.exists() else 'absent'})"
    )
    print(f"transcript: {t or '(none)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # `mvibe -- <claude args>` (no subcommand) is shorthand for `mvibe up -- ...`.
    if argv and argv[0] == "--":
        argv = ["up", *argv]

    parser = argparse.ArgumentParser(prog="mvibe", description=__doc__)
    # Subcommand optional: bare `mvibe` == `mvibe up` in the current directory.
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="launch the wrapped Claude TUI")
    p_run.add_argument("--cwd", help="chdir here before launching (match the bridge's --cwd)")
    p_run.add_argument(
        "--yolo",
        action="store_true",
        help="bypass claude's permission prompts (--dangerously-skip-permissions)",
    )
    p_run.add_argument(
        "cmd", nargs=argparse.REMAINDER, help="command to run (default: claude); prefix with --"
    )
    p_run.set_defaults(func=_cmd_run)

    p_bridge = sub.add_parser("bridge", help="remote mirror + WeChat inbound poll")
    p_bridge.add_argument("--cwd", help="session cwd to mirror (default: current)")
    p_bridge.add_argument(
        "--always", action="store_true", help="push output even when remote gate is off"
    )
    p_bridge.add_argument("--user", help="target WeChat user_id (default: most recent)")
    p_bridge.add_argument(
        "--no-wechat", action="store_true", help="disable the WeChat inbound poll loop"
    )
    p_bridge.set_defaults(func=_cmd_bridge)

    p_login = sub.add_parser("login", help="bind a WeChat bot via QR (writes mvibe config)")
    p_login.set_defaults(func=_cmd_login)

    p_up = sub.add_parser("up", help="one terminal: claude TUI + bridge (background)")
    p_up.add_argument("--cwd", help="chdir here before launching")
    p_up.add_argument("--always", action="store_true")
    p_up.add_argument("--user", help="target WeChat user_id (default: most recent)")
    p_up.add_argument("--no-wechat", action="store_true")
    p_up.add_argument(
        "--no-remote",
        action="store_true",
        help="start with remote takeover disabled (toggle later with /mvibe-on)",
    )
    p_up.add_argument(
        "--yolo",
        action="store_true",
        help="bypass claude's permission prompts (--dangerously-skip-permissions)",
    )
    p_up.add_argument(
        "cmd", nargs=argparse.REMAINDER, help="command to run (default: claude); prefix with --"
    )
    p_up.set_defaults(func=_cmd_up)

    p_remote = sub.add_parser("remote", help="enable/disable remote takeover")
    p_remote.add_argument("action", choices=["on", "off", "status"])
    p_remote.set_defaults(func=_cmd_remote)

    p_send = sub.add_parser("send", help="inject text into the live session")
    p_send.add_argument("text", nargs="?", help="text to inject; omit to read stdin")
    p_send.add_argument("--remote", action="store_true", help="also set flag=remote")
    p_send.add_argument("--no-submit", action="store_true", help="do not append CR")
    p_send.set_defaults(func=_cmd_send)

    p_keys = sub.add_parser(
        "keys", help="inject navigation keys (up/down/left/right/enter/space/tab/esc)"
    )
    p_keys.add_argument("keys", help="space-separated key names, e.g. 'down down enter'")
    p_keys.add_argument("--remote", action="store_true", help="also set flag=remote")
    p_keys.add_argument("--delay", type=int, default=40, help="ms between keys (default 40)")
    p_keys.set_defaults(func=_cmd_keys)

    p_flag = sub.add_parser("flag", help="get/set routing flag")
    p_flag.add_argument("mode", nargs="?", choices=["local", "remote"])
    p_flag.set_defaults(func=_cmd_flag)

    p_status = sub.add_parser("status", help="show flag + active transcript")
    p_status.add_argument("--cwd")
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        # bare `mvibe` -> launch claude + bridge in the current directory
        return _cmd_up(
            argparse.Namespace(
                cwd=None,
                always=False,
                user=None,
                no_wechat=False,
                no_remote=False,
                yolo=False,
                cmd=[],
            )
        )
    # REMAINDER keeps a leading "--"; strip it so `mvibe run -- claude` works.
    if getattr(args, "cmd", None) and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
