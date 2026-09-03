# Session Budget Gate

Pauses a Claude Code session for an explicit go-ahead once its estimated spend
passes a threshold, so a long autonomous run checks in with you instead of
quietly drawing down usage credits.

Opt-in. Installing it is a per-person choice and it does nothing until you
install it yourself.

## Install

```
/plugin marketplace add aburkard/session-budget-gate
/plugin install session-budget-gate@aburkard
```

Adding the marketplace only makes the plugin *appear in the list*. It is not
enabled until the second command, and installing any other plugin from this
marketplace does not pull this one in.

> **Don't add this to `enabledPlugins` in managed settings.** That force-enables
> a plugin for every org member, which is the opposite of what this one is for.
> Leave it out and let people install it themselves.

## Behavior

| Session spend | What happens |
|---|---|
| Under the threshold | Nothing. The hook exits silently and your normal permission rules apply untouched. |
| At or past it | The next tool call asks for confirmation, naming the running total. |
| After you approve | The bar moves up one increment, so you are asked once per increment rather than on every tool call. |

The default threshold is $50, and it is also the step size: you get asked at
roughly $50, $100, $150, and so on. The budget is per session — a new session
starts from zero.

The hook never returns an `allow` decision, because that would auto-approve the
call and bypass your own permission rules. Under budget it stays silent, which
leaves default behavior exactly as it was.

## Configure

```bash
export CLAUDE_SESSION_BUDGET_USD=25   # first ask, and the step size
export CLAUDE_SESSION_BUDGET_DEBUG=1  # print the running total to stderr
```

Set these before launching Claude Code — the hook inherits its environment from
the session.

## What the number actually is

Claude Code's transcript records token usage per response but no dollar figure,
so this prices tokens itself at API list rates. **It is an estimate, not your
plan's usage-credit ledger.** Treat the threshold as a proportional tripwire
rather than an invoice.

A model released after the price table was written is priced at Opus rates, and
the confirmation prompt names it so you know to add the real row. The table
lives at the top of `scripts/session-budget-gate.py`.

Two smaller caveats: the transcript is written asynchronously and can lag the
current turn, so the total trails slightly and catches up on the next call; and
spend is attributed by session ID, so resumed sessions continue their own total.

## Cost

Measured on a 7.8 MB transcript: about 16 ms per tool call in steady state, and
76 ms on the first call of a session. It reads only the bytes appended since the
previous call rather than reparsing the transcript.

It fails open on every error path — malformed input, a missing transcript, an
unreadable state file. A budget guard must not be able to break a session.

## Remove

```
/plugin uninstall session-budget-gate@aburkard
```

If you also wired this script into your own `~/.claude/settings.json` before
installing the plugin, remove that entry so the hook is not registered twice.
