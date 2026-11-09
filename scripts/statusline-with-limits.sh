#!/bin/bash
# Status line wrapper: persist Claude Code's plan-limit data for the budget hook,
# then delegate to the project's own status line so the display is unchanged.
#
# Why this exists: hooks never receive rate_limits, but the status line does
# (rate_limits.five_hour / seven_day / spend_limit, each with used_percentage
# 0-100 and a resets_at epoch). Writing it here lets session-budget-gate.py
# gate only once you are actually over the plan and drawing on usage credits.
#
# Must stay fast: Claude Code cancels an in-flight status line when a new update
# arrives. Write the file first, then delegate.
set -u
input=$(cat)
state_dir="$HOME/.claude/budget-state"
mkdir -p "$state_dir"

# Account-level, not per-session: rate limits belong to the account, so the most
# recent write from any session is the right answer. Atomic rename so a reader
# never sees a half-written file.
if command -v jq >/dev/null 2>&1; then
  tmp="$state_dir/.rate-limits.$$.tmp"
  # Skip writes without limit data so a fresh session can't null out good data.
  printf '%s' "$input" | jq -ce --arg now "$(date +%s)" 'select(.rate_limits != null) | {
      updated_at: ($now | tonumber),
      session_id: .session_id,
      total_cost_usd: .cost.total_cost_usd,
      rate_limits: .rate_limits
    }' > "$tmp" 2>/dev/null && mv -f "$tmp" "$state_dir/rate-limits.json" || rm -f "$tmp"
fi

# Delegate: the project's status line if it has one (the monorepo does), else a
# minimal default that also shows the limits so nothing is lost.
project_dir=$(printf '%s' "$input" | jq -r '.workspace.project_dir // .workspace.current_dir // empty' 2>/dev/null)
delegate=""
for d in "${CLAUDE_PROJECT_DIR:-}" "$project_dir"; do
  [ -n "$d" ] && [ -x "$d/.claude/statusline-command.sh" ] && { delegate="$d/.claude/statusline-command.sh"; break; }
  [ -n "$d" ] && [ -f "$d/.claude/statusline-command.sh" ] && { delegate="bash $d/.claude/statusline-command.sh"; break; }
done
if [ -n "$delegate" ]; then
  printf '%s' "$input" | (cd "${project_dir:-.}" 2>/dev/null; eval "$delegate")
else
  printf '%s' "$input" | jq -r '
    def pct(v): if (v|type)=="number" then (v+0.5|floor|tostring)+"%" else empty end;
    "[\(.model.display_name // "Claude")] ctx \(pct(.context_window.used_percentage) // "?")"
    + ((.rate_limits.five_hour.used_percentage | pct(.)) as $f
       | (.rate_limits.seven_day.used_percentage | pct(.)) as $s
       | if $f or $s then " | 5h " + ($f // "-") + " · 7d " + ($s // "-") else "" end)'
fi
