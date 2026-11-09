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

On Team plans, only an Owner can cap or turn off a member's extra usage. This is
the do-it-yourself version: it only affects you.

## Install

Everyone installs it for themselves. Nothing here turns on for anyone else.

```
/plugin marketplace add aburkard/session-budget-gate
/plugin install session-budget-gate@aburkard
```

Then do the token step below. Without it the hook has nothing to go on.

## The token step

Hooks aren't told how much of your plan you've used. But every API response
carries rate-limit headers with exactly that: your 5-hour and 7-day usage, and
whether usage credits are in use. So the hook sends a tiny request (one Haiku
token) every couple of minutes while you're working, and reads the headers.

It needs its own login token for that:

1. Make a long-lived token. This opens a browser login and prints a token that
   lasts a year:

   ```bash
   claude setup-token
   ```

2. Save it in your Keychain. This prompts for a "password": paste the token
   there, so it stays out of your shell history:

   ```bash
   security add-generic-password -U -s claude-budget-gate -a "$USER" -w
   ```

   Not on a Mac? Set `CLAUDE_BUDGET_GATE_TOKEN` to the token in your environment
   instead.

3. Check it:

   ```bash
   ~/.claude/plugins/cache/aburkard/session-budget-gate/*/scripts/session-budget-gate.py --check
   ```

   You should see `HTTP 200`, a list of `anthropic-ratelimit-unified-*` headers,
   and `Result: within plan -> silent` (unless you really are over).

Works in both the terminal and the desktop app. When the token expires, the hook
goes back to doing nothing and prints one reminder per session; redo steps 1–2.

### Optional: the status line (terminal only)

The older way to get the numbers still works as a fallback in the terminal CLI:
Claude Code passes your limits to the status line, and
`scripts/statusline-with-limits.sh` writes them to a file the hook reads. The
desktop app never runs status lines, which is why the token step is now the main
path. To use it, point `statusLine` in `~/.claude/settings.json` at that script.
If a repo has its own status line, put the same block in that repo's
`.claude/settings.local.json`; the script runs the repo's status line for you.

## What it does

- **Under your limit:** nothing. Doesn't matter how much the session has spent.
- **You hit a limit, or the API says you're on credits:** the next tool call
  asks you to confirm, and tells you which limit and when it resets.
- **You keep going on credits:** it asks again every $50.
- **The limit resets:** it goes quiet until the next time.
- **No limit data:** it does nothing, and after a couple of tool calls it prints
  one reminder about the token step.

It never auto-approves anything. When it has nothing to say it stays out of the
way, and your normal permission prompts work like they always did. The prompt
shows up in auto mode too.

Two gaps to know about. It only runs before tool calls, so a reply that's only
text can still bill a little. And it checks at most every two minutes, so it can
be up to two minutes late noticing you crossed.

## Settings

These are environment variables. Set them before you start Claude Code.

| Variable | Default | What it does |
|---|---|---|
| `CLAUDE_BUDGET_GATE_TOKEN` | unset | The setup-token, if you'd rather not use the Keychain |
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

## Speed and cost

About 60ms per tool call normally. Once every two minutes (shared across all
your sessions) a call also does the header check, which adds about half a
second. That check is a one-token Haiku request, so it costs next to nothing.
If anything goes wrong (no token, no network, odd response) the hook does
nothing rather than block you.

## Uninstall

```
/plugin uninstall session-budget-gate@aburkard
security delete-generic-password -s claude-budget-gate
```

If you set up the status line, take the `statusLine` entries back out.
