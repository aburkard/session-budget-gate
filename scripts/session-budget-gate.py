#!/usr/bin/env python3
"""PreToolUse budget gate: ask for a go-ahead once a session passes $N.

Claude Code's transcript records per-response token usage but NO dollar figure,
so this prices tokens itself from the table below. The result is an
API-list-price ESTIMATE, not your Team plan's usage-credit ledger. Treat it as
a proportional tripwire, not an invoice.

Behavior
  under budget          -> exit 0 with NO output, leaving normal permission
                           handling completely untouched. (Never print
                           "allow" here: that AUTO-APPROVES the call and
                           bypasses your own permission rules.)
  at/over the threshold -> permissionDecision "ask", so you get a prompt
                           naming the running total. After you approve, the
                           bar moves up by one more increment, so you are
                           asked once per $STEP rather than on every call.

Config
  CLAUDE_SESSION_BUDGET_USD   first ask, and the step size (default 50)
  CLAUDE_SESSION_BUDGET_DEBUG set to 1 to print the running total to stderr

Cost is accumulated incrementally: the byte offset into the transcript is
cached per session, so each line is priced exactly once and a PreToolUse call
never re-reads a multi-megabyte file.

Fails OPEN on every error. A budget guard must not be able to break a session.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

STATE_DIR = pathlib.Path.home() / ".claude" / "budget-state"

# USD per million tokens: (input, output, cache_write_5m, cache_read).
# Cache writes are 1.25x input (5m TTL) and 2x input (1h TTL); cache reads are
# 0.1x input, except Fable/Mythos 5.1 which read at a flat $0.25/MTok.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 12.5, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 12.5, 0.25),
    "claude-fable-5": (10.0, 50.0, 12.5, 1.0),
    "claude-mythos-5": (10.0, 50.0, 12.5, 1.0),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.5),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.5),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 0.5),
    "claude-opus-4-6": (5.0, 25.0, 6.25, 0.5),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.2),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.3),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.1),
}
# Used when a model string is not in the table (e.g. a model released after
# this file was written). Opus-tier keeps an unknown model from being counted
# as free; add the real row when you see it named in a prompt.
FALLBACK = (5.0, 25.0, 6.25, 0.5)
MILLION = 1_000_000


def price_line(raw: str) -> tuple[float, str | None]:
    """Return (usd, unknown_model_name) for one transcript line."""
    try:
        entry = json.loads(raw)
    except Exception:
        return 0.0, None
    if entry.get("type") != "assistant":
        return 0.0, None
    message = entry.get("message")
    if not isinstance(message, dict):
        return 0.0, None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return 0.0, None

    model = message.get("model") or ""
    rates = PRICES.get(model)
    unknown = None if rates else (model or "<missing>")
    rates = rates or FALLBACK
    in_rate, out_rate, write_rate, read_rate = rates

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) else 0

    usd = (
        count("input_tokens") * in_rate
        + count("output_tokens") * out_rate
        + count("cache_read_input_tokens") * read_rate
    ) / MILLION

    # Prefer the per-TTL breakdown when present: 1h cache writes cost 2x
    # input, not the 1.25x that the flat total assumes.
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, dict):
        five_min = breakdown.get("ephemeral_5m_input_tokens")
        one_hour = breakdown.get("ephemeral_1h_input_tokens")
        if isinstance(five_min, int) or isinstance(one_hour, int):
            usd += (five_min or 0) * write_rate / MILLION
            usd += (one_hour or 0) * in_rate * 2.0 / MILLION
            return usd, unknown

    usd += count("cache_creation_input_tokens") * write_rate / MILLION
    return usd, unknown


def main() -> None:
    event = json.load(sys.stdin)

    # Accept either naming convention so this keeps working if the hook
    # payload is ever camelCased.
    session_id = event.get("session_id") or event.get("sessionId") or ""
    transcript = event.get("transcript_path") or event.get("transcriptPath") or ""
    if not session_id or not transcript:
        return
    path = pathlib.Path(transcript).expanduser()
    if not path.is_file():
        return

    step = float(os.environ.get("CLAUDE_SESSION_BUDGET_USD") or 50.0)
    if step <= 0:
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / f"{session_id}.json"
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}

    offset = state.get("offset") or 0
    cost = state.get("cost") or 0.0
    next_ask = state.get("next_ask") or step
    unknown_models = set(state.get("unknown_models") or [])

    # A transcript that shrank was rewritten or replaced: reprice from zero.
    if path.stat().st_size < offset:
        offset, cost = 0, 0.0

    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
        offset = handle.tell()

    # Ignore a trailing partial line; its bytes stay unread for the next call.
    tail = chunk.rfind(b"\n")
    if tail == -1:
        offset -= len(chunk)
        chunk = b""
    else:
        offset -= len(chunk) - (tail + 1)
        chunk = chunk[: tail + 1]

    for raw in chunk.decode("utf-8", "replace").splitlines():
        if not raw.strip():
            continue
        usd, unknown = price_line(raw)
        cost += usd
        if unknown:
            unknown_models.add(unknown)

    ask = cost >= next_ask
    if ask:
        # Move the bar to the next multiple above the current total, so an
        # approval buys another full increment instead of re-prompting.
        next_ask = (int(cost / step) + 1) * step

    state_path.write_text(
        json.dumps(
            {
                "offset": offset,
                "cost": cost,
                "next_ask": next_ask,
                "unknown_models": sorted(unknown_models),
            }
        )
    )

    if os.environ.get("CLAUDE_SESSION_BUDGET_DEBUG") == "1":
        print(f"[budget] session ~${cost:.2f}, next ask at ${next_ask:.2f}", file=sys.stderr)

    if not ask:
        return

    note = ""
    if unknown_models:
        note = (
            "  Priced at Opus rates for unrecognized model(s): "
            + ", ".join(sorted(unknown_models))
            + "."
        )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": (
                        f"Session has used roughly ${cost:.2f} (estimated at API list "
                        f"prices, not your credit ledger). Approve to continue; the "
                        f"next check-in is at ${next_ask:g}.{note}"
                    ),
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail open: never let the budget guard break a session
    sys.exit(0)
