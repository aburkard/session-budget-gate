#!/usr/bin/env python3
"""PreToolUse budget gate: ask for a go-ahead once you are spending usage credits.

Two modes, chosen automatically:

  PLAN-LIMIT MODE (preferred). If the companion status line wrapper
  (statusline-with-limits.sh) has recorded Claude Code's rate_limits data, the
  hook stays completely silent while you are under your plan's 5-hour / 7-day
  limits. Tokens inside the plan are already paid for. The moment a window hits
  100% and the session is drawing on usage credits, it asks once, then again
  after every $STEP of credit spend. When the window resets it goes quiet again.

  NO LIMIT DATA. Without the wrapper the hook cannot tell plan usage from
  credit usage, so by default it does nothing except post one non-blocking note
  per session saying how to enable plan-limit mode. Set
  CLAUDE_SESSION_BUDGET_FALLBACK=dollars to opt into a plain tripwire instead:
  ask at $STEP of estimated session spend, then every $STEP after.

Dollar figures are priced from the transcript's token counts at API list rates.
The transcript has no cost field, so this is an ESTIMATE, not the credit ledger.

Under budget in either mode the hook exits 0 with NO output, which leaves your
normal permission handling untouched. It never emits "allow": that would
auto-approve the call and bypass your own permission rules.

Config
  CLAUDE_SESSION_BUDGET_USD        step size in dollars (default 50)
  CLAUDE_SESSION_BUDGET_LIMIT_PCT  plan usage % that counts as "over" (default 100;
                                   set 95 to be asked just before crossing)
  CLAUDE_SESSION_BUDGET_FALLBACK   "dollars" to gate on raw spend when no limit data exists
  CLAUDE_SESSION_BUDGET_DEBUG      "1" to print state to stderr

Fails OPEN on every error. A budget guard must not be able to break a session.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from datetime import datetime

STATE_DIR = pathlib.Path.home() / ".claude" / "budget-state"
LIMITS_FILE = STATE_DIR / "rate-limits.json"
# Limit data older than this is treated as unknown. The status line rewrites it
# on every message during an active session, so this only bites after a long
# idle gap, and a window whose resets_at has passed is ignored regardless.
LIMITS_MAX_AGE_SECONDS = 6 * 3600

# USD per million tokens: (input, output, cache_write_5m, cache_read).
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
FALLBACK = (5.0, 25.0, 6.25, 0.5)
MILLION = 1_000_000


def price_line(raw: str) -> tuple[float, str | None]:
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
    in_rate, out_rate, write_rate, read_rate = rates or FALLBACK

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) else 0

    usd = (
        count("input_tokens") * in_rate
        + count("output_tokens") * out_rate
        + count("cache_read_input_tokens") * read_rate
    ) / MILLION
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


def read_limits(now: float, over_pct: float) -> dict | None:
    """Return {'over': bool, 'windows': {...}} or None when no usable data."""
    try:
        data = json.loads(LIMITS_FILE.read_text())
    except Exception:
        return None
    updated = data.get("updated_at")
    if not isinstance(updated, (int, float)) or now - updated > LIMITS_MAX_AGE_SECONDS:
        return None
    limits = data.get("rate_limits")
    if not isinstance(limits, dict) or not limits:
        return None
    windows: dict[str, dict] = {}
    over = False
    for name in ("five_hour", "seven_day", "spend_limit"):
        win = limits.get(name)
        if not isinstance(win, dict):
            continue
        pct = win.get("used_percentage")
        resets = win.get("resets_at")
        if not isinstance(pct, (int, float)):
            continue
        # A window whose reset time has passed no longer applies, whatever the
        # recorded percentage says.
        expired = isinstance(resets, (int, float)) and now >= resets
        windows[name] = {"pct": float(pct), "resets_at": resets, "expired": expired}
        if not expired and pct >= over_pct:
            over = True
    if not windows:
        return None
    return {"over": over, "windows": windows}


def fmt_reset(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%-I:%M %p %a")
    except Exception:
        return "unknown"


def emit_ask(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason,
    }}))


def emit_note(text: str) -> None:
    """Non-blocking note to the user; carries no permission decision."""
    print(json.dumps({"systemMessage": text}))


def main() -> None:
    event = json.load(sys.stdin)
    session_id = event.get("session_id") or event.get("sessionId") or ""
    transcript = event.get("transcript_path") or event.get("transcriptPath") or ""
    if not session_id or not transcript:
        return
    path = pathlib.Path(transcript).expanduser()
    if not path.is_file():
        return

    step = float(os.environ.get("CLAUDE_SESSION_BUDGET_USD") or 50.0)
    over_pct = float(os.environ.get("CLAUDE_SESSION_BUDGET_LIMIT_PCT") or 100.0)
    fallback_on = (os.environ.get("CLAUDE_SESSION_BUDGET_FALLBACK") or "").lower() in ("dollars", "on", "1", "true")
    debug = os.environ.get("CLAUDE_SESSION_BUDGET_DEBUG") == "1"
    if step <= 0:
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / f"{session_id}.json"
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}

    # ---- incremental transcript pricing (unchanged) ----
    offset = state.get("offset") or 0
    cost = state.get("cost") or 0.0
    unknown_models = set(state.get("unknown_models") or [])
    if path.stat().st_size < offset:
        offset, cost = 0, 0.0
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
        offset = handle.tell()
    tail = chunk.rfind(b"\n")
    if tail == -1:
        offset -= len(chunk)
        chunk = b""
    else:
        offset -= len(chunk) - (tail + 1)
        chunk = chunk[: tail + 1]
    for raw in chunk.decode("utf-8", "replace").splitlines():
        if raw.strip():
            usd, unknown = price_line(raw)
            cost += usd
            if unknown:
                unknown_models.add(unknown)

    # ---- gating ----
    now = time.time()
    limits = read_limits(now, over_pct)
    boundary = state.get("boundary_cost")          # session cost when we first saw "over"
    next_ask_limit = state.get("next_ask_limit")   # plan-limit mode threshold
    next_ask_dollar = state.get("next_ask") or step  # dollar-mode threshold
    nudged = bool(state.get("nudged"))
    # The status line only records limit data after the first API response, so a
    # fresh session legitimately has none for a call or two. Only remark on its
    # absence once that grace period is clearly over.
    calls = int(state.get("calls") or 0) + 1
    note = ""
    ask = False
    reason = ""
    mode = "limit" if limits is not None else "dollar"

    if mode == "limit":
        if not limits["over"]:
            # Under the plan: silent, and forget any earlier crossing so the
            # next one asks again.
            boundary = None
            next_ask_limit = None
        else:
            if boundary is None:
                boundary = cost
                next_ask_limit = boundary + step
                ask = True
                credit_spend = 0.0
            else:
                credit_spend = max(0.0, cost - boundary)
                if cost >= (next_ask_limit or float("inf")):
                    ask = True
                    next_ask_limit = boundary + (int(credit_spend / step) + 1) * step
            if ask:
                live = [w for w in limits["windows"].items() if not w[1]["expired"]]
                worst = max(live, key=lambda kv: kv[1]["pct"]) if live else None
                where = ""
                if worst:
                    label = {"five_hour": "5-hour", "seven_day": "7-day", "spend_limit": "spend"}[worst[0]]
                    where = f" ({label} window at {worst[1]['pct']:.0f}%, resets {fmt_reset(worst[1]['resets_at'])})"
                reason = (
                    f"Plan limit reached{where}. Everything from here draws on usage credits; "
                    f"roughly ${credit_spend:.2f} of this session has been on credits so far "
                    f"(estimated at API list prices). Approve to continue; next check-in after "
                    f"another ${step:g} of credit spend."
                )
    else:
        if fallback_on:
            if cost >= next_ask_dollar:
                ask = True
                next_ask_dollar = (int(cost / step) + 1) * step
                reason = (
                    f"Session has used roughly ${cost:.2f} (estimated at API list prices). No plan-limit "
                    f"data is available, so this cannot tell plan usage from credit usage. Approve to "
                    f"continue; next check-in at ${next_ask_dollar:g}."
                )
        elif not nudged and calls >= 3:
            nudged = True
            note = (
                "session-budget-gate: no plan-limit data found, so nothing is being gated. To be "
                "asked only once you are over your plan and spending usage credits, install the "
                "statusline-with-limits.sh wrapper (see the session-budget-gate README). "
                "Set CLAUDE_SESSION_BUDGET_FALLBACK=dollars for a plain spend tripwire instead."
            )

    if ask and unknown_models:
        reason += "  Priced at Opus rates for unrecognized model(s): " + ", ".join(sorted(unknown_models)) + "."

    state_path.write_text(json.dumps({
        "offset": offset,
        "cost": cost,
        "next_ask": next_ask_dollar,
        "boundary_cost": boundary,
        "next_ask_limit": next_ask_limit,
        "mode": mode,
        "nudged": nudged,
        "calls": calls,
        "unknown_models": sorted(unknown_models),
    }))

    if debug:
        summary = f"mode={mode} cost=${cost:.2f}"
        if limits is not None:
            summary += " over=" + str(limits["over"]) + " " + " ".join(
                f"{k}={v['pct']:.0f}%{'(expired)' if v['expired'] else ''}" for k, v in limits["windows"].items())
            if boundary is not None:
                summary += f" boundary=${boundary:.2f} next=${next_ask_limit:.2f}"
        else:
            summary += f" next=${next_ask_dollar:g}"
        print(f"[budget] {summary}", file=sys.stderr)

    if ask:
        emit_ask(reason)
    elif note:
        emit_note(note)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
