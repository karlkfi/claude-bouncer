#!/usr/bin/env bash
# Capture a screenshot of the workspace-guard "ask" prompt for the README.
#
# Usage:
#   ./scripts/capture-prompt-screenshot.sh [output_path]
#
# Defaults to docs/img/ask-prompt.png. Tunables (env vars):
#   SCREENSHOT_DELAY   seconds before capture (default 20). Bump if Claude
#                      Code takes longer to render the prompt.
#   CROP_HEIGHT        height in points of the bottom slice of the window to
#                      capture (default 500). Set 0 to capture the whole
#                      window instead. The permission prompt sits at the
#                      bottom of the terminal, so this trims scrollback above.
#
# Requirements:
#   - macOS (uses `screencapture` and AppleScript).
#   - `claude` on PATH.
#   - The workspace-guard hook active for this project (this repo's own hook
#     counts; no install required).
#   - Two permissions granted to your terminal app
#     (System Settings → Privacy & Security):
#       • Screen Recording — so screencapture can grab pixels.
#       • Accessibility    — so System Events can read window bounds.
#     First run will fail and macOS will prompt you for each. Grant, re-run.
#
# What it does: schedules a screenshot for $DELAY seconds out, then launches
# Claude Code with a prompt that asks it to run `grep root /etc/passwd`. The
# hook intercepts and Claude Code shows the permission prompt. When the timer
# fires, the script reads the frontmost window's bounds via AppleScript and
# captures just that rectangle. Falls back to full screen if the bounds query
# fails. Cancel the prompt (Esc) and quit Claude Code when done.

set -euo pipefail

OUT="${1:-docs/img/ask-prompt.png}"
DELAY="${SCREENSHOT_DELAY:-20}"
CROP_HEIGHT="${CROP_HEIGHT:-500}"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This script uses macOS screencapture. Adapt for other platforms." >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI not found on PATH." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

cat <<EOF
Capturing the active window to $OUT in ${DELAY}s.
Keep the Claude Code terminal in focus until the screenshot fires.
EOF

(
  sleep "$DELAY"

  bounds=$(osascript <<'APPLESCRIPT' 2>/dev/null || true
tell application "System Events"
  set frontApp to first application process whose frontmost is true
  set winPos to position of window 1 of frontApp
  set winSize to size of window 1 of frontApp
end tell
set x to item 1 of winPos
set y to item 2 of winPos
set w to item 1 of winSize
set h to item 2 of winSize
return (x as string) & "," & (y as string) & "," & (w as string) & "," & (h as string)
APPLESCRIPT
)

  if [[ "$bounds" =~ ^-?[0-9]+,-?[0-9]+,[0-9]+,[0-9]+$ ]]; then
    IFS=, read -r wx wy ww wh <<< "$bounds"
    if (( CROP_HEIGHT > 0 && CROP_HEIGHT < wh )); then
      region="${wx},$((wy + wh - CROP_HEIGHT)),${ww},${CROP_HEIGHT}"
      label="bottom ${CROP_HEIGHT}pt of window"
    else
      region="$bounds"
      label="full window"
    fi
    if screencapture -R "$region" -o "$OUT" 2>/dev/null; then
      echo
      echo "Saved $OUT ($label: $region)"
      exit 0
    fi
    echo
    echo "screencapture failed — check Screen Recording permission." >&2
    exit 1
  fi

  echo
  echo "Could not read window bounds (Accessibility permission?). Falling back to full screen." >&2
  if screencapture -o "$OUT" 2>/dev/null; then
    echo "Saved $OUT (full screen — crop manually)"
  else
    echo "screencapture failed — check Screen Recording permission." >&2
  fi
) &

exec claude "Run this bash command verbatim and then stop: grep root /etc/passwd"
