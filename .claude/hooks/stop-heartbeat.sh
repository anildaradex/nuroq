#!/bin/bash
# Stop hook: update (not append) a `_Last turn: TS_` heartbeat line directly
# under `## Current session` in session.md. Fires once per turn end; replaces
# the previous heartbeat so session.md stays clean while showing live activity.
# Inserts the anchor line right after the header on first run.

set -e

SESSION_FILE="$CLAUDE_PROJECT_DIR/session.md"
[[ -f "$SESSION_FILE" ]] || exit 0
grep -q "^## Current session" "$SESSION_FILE" || exit 0

TS=$(date -u '+%Y-%m-%d %H:%MZ')
NEW_LINE="_Last turn: ${TS}_"
TMP="${SESSION_FILE}.tmp.$$"

if grep -q "^_Last turn:" "$SESSION_FILE"; then
  awk -v new="$NEW_LINE" '
    /^## Current session/ { flag=1; print; next }
    /^## / && !/^## Current session/ { flag=0 }
    flag && /^_Last turn:/ && !done { print new; done=1; next }
    { print }
  ' "$SESSION_FILE" > "$TMP" && mv "$TMP" "$SESSION_FILE"
else
  awk -v new="$NEW_LINE" '
    /^## Current session/ && !done { print; print ""; print new; done=1; next }
    { print }
  ' "$SESSION_FILE" > "$TMP" && mv "$TMP" "$SESSION_FILE"
fi

exit 0
