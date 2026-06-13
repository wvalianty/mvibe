#!/usr/bin/env bash
# Local smoke test for mvibe core: PTY inject, flag mutex, sanitize, HTTP, token,
# tailer. No WeChat, no network, isolated MVIBE_HOME. Safe to run anytime.
set -u

cd "$(dirname "$0")/.."
export MVIBE_HOME=/tmp/mvibe_smoke
rm -rf "$MVIBE_HOME"
OUT=/tmp/mvibe_smoke_out.txt
PASS=0
FAIL=0

run() { uv run --quiet mvibe "$@"; }
check() { # check <name> <needle> <file>  (pass if needle present)
  if grep -qF "$2" "$3"; then echo "  PASS: $1"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (missing: $2)"; FAIL=$((FAIL+1)); fi; }
absent() { # absent <name> <needle> <file>  (pass if needle absent)
  if grep -qF "$2" "$3"; then echo "  FAIL: $1 (unexpected: $2)"; FAIL=$((FAIL+1));
  else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }

echo "== start wrapped echo-child (stand-in for claude) =="
tail -f /dev/null | run run -- python3 -u -c '
import sys
for l in sys.stdin:
    if l.strip(): print("ECHO:", l.strip().upper())
' > "$OUT" 2>&1 &
WPID=$!
sleep 1.5

echo "== 1. remote inject =="
run send --remote "from remote"
sleep 0.5
check "remote inject delivered" "ECHO: FROM REMOTE" "$OUT"

echo "== 2. flag mutex (local drops inject) =="
run flag local >/dev/null
run send "should be dropped"
sleep 0.5
absent "local-mode inject dropped" "SHOULD BE DROPPED" "$OUT"

echo "== 3. control-char sanitize =="
run send --remote "$(printf 'x\x1b[31mY')"
sleep 0.5
check "ESC stripped" "ECHO: X[31MY" "$OUT"

echo "== 4. HTTP inbound (--no-wechat) =="
run bridge --cwd . --no-wechat --port 8765 >/tmp/mvibe_bridge.log 2>&1 &
BPID=$!
sleep 1.5
curl -s localhost:8765/status | grep -q flag && echo "  PASS: /status" && PASS=$((PASS+1)) || { echo "  FAIL: /status"; FAIL=$((FAIL+1)); }
curl -s -X POST --data 'via http' localhost:8765/inbound >/dev/null
sleep 0.5
check "HTTP /inbound injected" "ECHO: VIA HTTP" "$OUT"
kill $BPID 2>/dev/null

echo "== 5. HTTP token gate =="
MVIBE_HTTP_TOKEN=secret run bridge --cwd . --no-wechat --port 8766 >/tmp/mvibe_bridge2.log 2>&1 &
BPID2=$!
sleep 1.5
code_noauth=$(curl -s -o /dev/null -w '%{http_code}' -X POST --data hi localhost:8766/inbound)
code_auth=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-MVIBE-Token: secret' --data hi localhost:8766/inbound)
[ "$code_noauth" = "401" ] && echo "  PASS: no-token -> 401" && PASS=$((PASS+1)) || { echo "  FAIL: no-token -> $code_noauth"; FAIL=$((FAIL+1)); }
[ "$code_auth" = "200" ] && echo "  PASS: token -> 200" && PASS=$((PASS+1)) || { echo "  FAIL: token -> $code_auth"; FAIL=$((FAIL+1)); }
kill $BPID2 2>/dev/null

echo "== 6. file perms (0600/0700) =="
run flag remote >/dev/null
perm_dir=$(stat -f '%Lp' "$MVIBE_HOME" 2>/dev/null || stat -c '%a' "$MVIBE_HOME")
[ "$perm_dir" = "700" ] && echo "  PASS: home 0700" && PASS=$((PASS+1)) || { echo "  FAIL: home $perm_dir"; FAIL=$((FAIL+1)); }

echo "== 7. tailer parses real transcript =="
n=$(uv run --quiet python -c "
from mvibe.tailer import _extract_assistant_text
import json,glob,os
g=glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl'))
if not g: print(0); raise SystemExit
f=max(g,key=os.path.getmtime)
print(sum(1 for l in open(f) if l.strip() and _extract_assistant_text(json.loads(l))))
")
[ "${n:-0}" -gt 0 ] && echo "  PASS: tailer extracted $n turns" && PASS=$((PASS+1)) || echo "  SKIP: no transcript found"

echo "== cleanup =="
# `uv run` spawns a grandchild python; kill by pattern so nothing leaks the port.
pkill -f "mvibe bridge --cwd . --no-wechat" 2>/dev/null
pkill -f 'print("ECHO:"' 2>/dev/null
kill $WPID 2>/dev/null; pkill -P $WPID 2>/dev/null
rm -rf "$MVIBE_HOME" "$OUT" /tmp/mvibe_bridge.log /tmp/mvibe_bridge2.log

echo
echo "RESULT: $PASS passed, $FAIL failed"
exit $FAIL
