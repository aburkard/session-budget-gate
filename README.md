# session-budget-gate

A Claude Code hook that stops and asks before a long-running session starts
spending your usage credits.

## Why

If you're on a Claude subscription, everything inside your 5-hour and 7-day
limits is already paid for. Once you hit a limit, Claude Code can keep going by
drawing on usage credits, and that's real money. I wanted an agent that had been
running for a while to stop and check with me at that point instead of quietly
burning through credits.

So: under the limit, this does nothing. When you hit it, it asks once. If you
say yes, it asks again every $50 or so of credit spend.

## Install

Everyone installs it for themselves. Nothing here turns on for anyone else.

```
/plugin marketplace add aburkard/session-budget-gate
/plugin install session-budget-gate@aburkard
```

Then do the status line step below. Without it the hook has nothing to go on.

## The status line step

Claude Code doesn't tell hooks how much of your limit you've used. It does tell
the status line. So this repo includes a small status line script that writes
those numbers to a file, and the hook reads the file.

1. Download the script:

   ```bash
   mkdir -p ~/.claude/hooks && curl -fsSL -o ~/.claude/hooks/statusline-with-limits.sh \
     https://raw.githubusercontent.com/aburkard/session-budget-gate/main/scripts/statusline-with-limits.sh \
     && chmod +x ~/.claude/hooks/statusline-with-limits.sh
   ```

2. Set it as your status line in `~/.claude/settings.json`:

   ```json
   "statusLine": { "type": "command", "command": "/Users/YOU/.claude/hooks/statusline-with-limits.sh" }
   ```

3. If a repo you work in has its own status line (a `statusLine` in its
   `.claude/settings.json`), that one wins over yours. Add the same block to that
   repo's `.claude/settings.local.json`, which is personal and gitignored. The
   script notices the repo's own status line and runs it, so your status bar
   looks the same as before. It just records the numbers on the way through.

Restart Claude Code. If your status bar shows something like `5h 42%`, it's
working.

One caveat. The Claude Code docs say this data shows up for Pro and Max plans.
I'm on a Team plan. If the `5h` number appears in your status bar after
restarting, you're fine. If it never does, your plan isn't getting the data and
the hook will just sit there doing nothing.

## What it does

- **Under your limit:** nothing. Doesn't matter how much the session has spent.
- **You hit a limit:** the next tool call asks you to confirm, and tells you
  which limit and when it resets.
- **You keep going on credits:** it asks again every $50.
- **The limit resets:** it goes quiet until the next time.
- **No status line data:** it does nothing, and after a couple of tool calls it
  prints one reminder about the step above.

It never auto-approves anything. When it has nothing to say it stays out of the
way, and your normal permission prompts work like they always did.

## Settings

These are environment variables. Set them before you start Claude Code.

| Variable | Default | What it does |
|---|---|---|
| `CLAUDE_SESSION_BUDGET_USD` | `50` | How much credit spend between check-ins |
| `CLAUDE_SESSION_BUDGET_LIMIT_PCT` | `100` | What counts as "over". Set `95` to get asked just before you cross |
| `CLAUDE_SESSION_BUDGET_FALLBACK` | off | Set to `dollars` to get asked every $50 of total spend, even without limit data |
| `CLAUDE_SESSION_BUDGET_DEBUG` | off | Set to `1` to print what it's doing to stderr |

## About the dollar amounts

Claude Code logs how many tokens each response used, but not what they cost. So
the hook multiplies tokens by the public API prices. That's an estimate, not
what your subscription is actually charging you. After you've crossed your limit
it only uses the number to space out the check-ins, so it doesn't need to be
exact. The price table is at the top of `scripts/session-budget-gate.py`. If a
model comes out that isn't in it, it gets priced like Opus and the prompt tells
you.

## Speed

About 16ms per tool call once it's warmed up, 76ms the first time in a session.
It only reads the new part of the transcript each time, not the whole thing. If
anything goes wrong it does nothing rather than block you.

## Uninstall

```
/plugin uninstall session-budget-gate@aburkard
```

Then take the `statusLine` entries back out.
