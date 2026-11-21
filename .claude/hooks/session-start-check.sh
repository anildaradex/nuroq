#!/bin/bash
# SessionStart hook: if `## Current session` in session.md has >= $THRESHOLD
# unpromoted bullets, inject a reminder so Claude promotes them BEFORE starting
# new work — otherwise they'd be at risk of being summarized out of the
# conversation by context compaction mid-session.

set -e

SESSION_FILE="$CLAUDE_PROJECT_DIR/session.md"
THRESHOLD=8

[[ -f "$SESSION_FILE" ]] || exit 0
grep -q "^## Current session" "$SESSION_FILE" || exit 0

COUNT=$(awk '
  /^## Current session/ { flag=1; next }
  /^## / && !/^## Current session/ { flag=0 }
  flag && /^- / { count++ }
  END { print count+0 }
' "$SESSION_FILE")

if (( COUNT >= THRESHOLD )); then
  MSG="session.md has ${COUNT} unpromoted bullets under '## Current session' (threshold ${THRESHOLD}). Per CLAUDE.md convention, promote them into a numbered dated Session block BEFORE starting new work — otherwise context compaction mid-session may summarize them away before they're crystallized into curated form."
  # Escape double quotes and backslashes for JSON
  MSG_JSON=$(printf '%s' "$MSG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": ${MSG_JSON}
  }
}
EOF
fi

exit 0
