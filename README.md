# session-budget-gate

Claude Code hook that asks before a session starts spending usage credits.

Under your plan's 5-hour and weekly limits, it does nothing. Once you're over and
on credits, the next tool call asks you to approve, then again every ~$50.

## Install

```
/plugin marketplace add aburkard/session-budget-gate
/plugin install session-budget-gate@aburkard
```

The hook reads your usage from API response headers, so it needs a token:

```bash
claude setup-token
security add-generic-password -U -s claude-budget-gate -a "$USER" -w   # paste the token
```

Not on a Mac: set `CLAUDE_BUDGET_GATE_TOKEN` instead.

Check it works:

```bash
~/.claude/plugins/cache/aburkard/session-budget-gate/*/scripts/session-budget-gate.py --check
```

## Notes

- Without a token it does nothing.
- It only runs before tool calls, and checks usage at most every 2 minutes.
- `CLAUDE_SESSION_BUDGET_USD` changes the $50 step.

## Uninstall

```
/plugin uninstall session-budget-gate@aburkard
security delete-generic-password -s claude-budget-gate
```
