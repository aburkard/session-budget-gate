# Session Budget Gate

Pauses a Claude Code session for an explicit go-ahead **once you are over your
plan's usage limit and spending usage credits.** While you are under the limit
it does nothing at all: those tokens are already paid for. It exists so a long
autonomous run checks in with you before quietly drawing down credits.

Opt-in. Installing it is a per-person choice; nothing runs until you install it.

## Install

```
/plugin marketplace add aburkard/session-budget-gate
/plugin install session-budget-gate@aburkard
```

The first command only lists the plugin. Nothing is enabled until the second.

> Don't add this to `enabledPlugins` in managed settings. That force-enables it
> for a whole org, which is the opposite of what it is for.

## Enable plan-limit mode (the point of the plugin)

Hooks never receive your plan-limit data, but Claude Code's **status line** does:
`rate_limits.five_hour` and `rate_limits.seven_day`, each with `used_percentage`
(0 to 100) and a `resets_at` time. This plugin ships a small status line wrapper
that records those numbers to a file the hook reads. **Without it the hook gates
nothing** and posts one note per session reminding you.

1. Put the wrapper somewhere stable:

   ```bash
   mkdir -p ~/.claude/hooks && curl -fsSL -o ~/.claude/hooks/statusline-with-limits.sh \
     https://raw.githubusercontent.com/aburkard/session-budget-gate/main/scripts/statusline-with-limits.sh \
     && chmod +x ~/.claude/hooks/statusline-with-limits.sh
   ```

2. Point your status line at it in `~/.claude/settings.json`:

   ```json
   "statusLine": { "type": "command", "command": "/Users/YOU/.claude/hooks/statusline-with-limits.sh" }
   ```

3. **If a project you work in has its own status line** (a `statusLine` in the
   project's `.claude/settings.json`), that setting outranks your user one. Add
   the same `statusLine` block to that project's `.claude/settings.local.json`,
   which is yours alone and gitignored. The wrapper detects the project's
   `.claude/statusline-command.sh` and delegates to it, so what you see does
   not change; it just records the limits on the way through.

Restart Claude Code. You can confirm it is working when your status bar shows a
`5h NN%` figure and `~/.claude/budget-state/rate-limits.json` exists.

> **Plan caveat.** Claude Code's docs say `rate_limits` appears for Pro and Max
> subscribers. Team seats share the same 5-hour and 7-day windows and the
> author's Team-plan status line renders them, so it is expected to work there
> too, but check for the `5h` figure in your status bar after restarting. If it
> never appears, your account is not receiving the data and the hook will stay
> in its do-nothing state.

## Behavior

| Situation | What happens |
|---|---|
| Under the plan limit | Nothing. Silent, no matter how much the session has spent. Your normal permission rules apply untouched. |
| A window reaches 100% | The next tool call asks for confirmation, once, naming the window and when it resets. |
| Still over, spending credits | Asks again after every $50 (estimated) of credit spend, not on every call. |
| The window resets | Quiet again. The next crossing asks afresh. |
| No limit data available | Nothing is gated. One non-blocking note per session says how to enable it. |

The hook never returns an `allow` decision, because that would auto-approve the
call and bypass your own permission rules. When it has nothing to say it exits
silently, leaving default behavior exactly as it was.

## Configure

Set these before launching Claude Code; the hook inherits the session's environment.

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_SESSION_BUDGET_USD` | `50` | Credit-spend increment between check-ins |
| `CLAUDE_SESSION_BUDGET_LIMIT_PCT` | `100` | Plan usage that counts as "over". Set `95` to be asked just before crossing |
| `CLAUDE_SESSION_BUDGET_FALLBACK` | unset | `dollars` to gate on raw estimated spend when no limit data exists (the old tripwire) |
| `CLAUDE_SESSION_BUDGET_DEBUG` | unset | `1` to print mode, spend, and limit state to stderr |

## What the dollar figure actually is

The transcript records token usage per response but no dollar amount, so the
hook prices tokens itself at API list rates. **It is an estimate, not your
plan's credit ledger.** In plan-limit mode the only dollar figure that matters
is the increment between check-ins after you cross, so the estimate's absolute
accuracy is not load-bearing. A model released after the price table was
written is priced at Opus rates and named in the prompt; the table is at the
top of `scripts/session-budget-gate.py`.

Two smaller caveats: the transcript is written asynchronously and can lag the
current turn slightly, and limit data older than six hours, or a window whose
`resets_at` has passed, is treated as unknown rather than trusted.

## Cost

About 16 ms per tool call in steady state, 76 ms on the first call of a session
(measured on a 7.8 MB transcript). It reads only bytes appended since the
previous call. The status line wrapper adds one `jq` pass and an atomic file
write before delegating.

Fails open on every error path. A budget guard must not be able to break a session.

## Remove

```
/plugin uninstall session-budget-gate@aburkard
```

Then remove the `statusLine` entries you added (or point them back at your
previous status line) and delete `~/.claude/budget-state/` if you like.
