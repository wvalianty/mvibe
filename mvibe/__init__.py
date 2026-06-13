"""mvibe — mirror a local Claude Code TUI to a remote chat surface.

Single live `claude` process. A PTY wrapper keeps the local terminal identical
to running `claude` directly, while a flag file toggles whether remote (WeChat)
input is injected and remote output is mirrored. No process restart on switch.
"""

__version__ = "0.1.0"
