#!/usr/bin/env python3
"""PreToolUse budget gate: ask for a go-ahead once you are spending usage credits.

Plan-limit data comes from, in order:

  1. HEADER PROBE (preferred; works in the desktop app and the CLI). A cached
     1-token Haiku request authenticated with a `claude setup-token` token from
     the Keychain (service "claude-budget-gate") or CLAUDE_BUDGET_GATE_TOKEN.
     Its anthropic-ratelimit-unified-* response headers carry 5h/7d utilization
     and whether usage credits are in use.
  2. STATUS LINE (terminal CLI only; the desktop app never runs status lines).
     statusline-with-limits.sh records Claude Code's rate_limits data to a file.

  With limit data, the hook stays completely silent while you are under your
  plan's 5-hour / 7-day limits. Tokens inside the plan are already paid for. The
  moment a window hits 100% or the API reports usage credits in use, it asks
  once, then again after every $STEP of credit spend. When the window resets it
  goes quiet again.

  NO LIMIT DATA. The hook cannot tell plan usage from credit usage, so by
  default it does nothing except post one non-blocking note per session saying
  how to set up the token. Set
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
  CLAUDE_BUDGET_GATE_TOKEN         setup-token token, instead of the Keychain

Diagnose with:  session-budget-gate.py --check

Fails OPEN on every error. A budget guard must not be able to break a session.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

STATE_DIR = pathlib.Path.home() / ".claude" / "budget-state"
LIMITS_FILE = STATE_DIR / "rate-limits.json"

# HEADER PROBE (preferred; works in the desktop app, which never runs status
# lines). Every Messages API response carries anthropic-ratelimit-unified-*
# headers with 5h/7d utilization and whether usage credits are in use. The hook
# sends a 1-token Haiku request with a long-lived token from `claude setup-token`
# (inference scope is enough), stored in the Keychain under KEYCHAIN_SERVICE, and
# caches the answer account-wide for PROBE_TTL_SECONDS.
PROBE_FILE = STATE_DIR / "probe.json"
PROBE_TTL_SECONDS = 120
PROBE_TIMEOUT_SECONDS = 5
KEYCHAIN_SERVICE = "claude-budget-gate"
PROBE_MODEL = "claude-haiku-4-5"
HDR = "anthropic-ratelimit-unified-"
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


def read_probe_token() -> str | None:
    token = os.environ.get("CLAUDE_BUDGET_GATE_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def run_probe(token: str) -> dict:
    """One tiny request; returns {'headers': {...unified headers}, 'http': code}."""
    body = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "."}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
            "User-Agent": "claude-budget-gate/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            code, headers = resp.status, resp.headers
    except urllib.error.HTTPError as err:  # a 429 still carries the headers
        code, headers = err.code, err.headers
    unified = {k.lower(): v for k, v in headers.items() if k.lower().startswith(HDR)}
    return {"http": code, "headers": unified}


def probe_limits(now: float, over_pct: float, force: bool = False) -> dict | None:
    """Same shape as read_limits(), from the header probe. None when unavailable."""
    cached = None
    try:
        cached = json.loads(PROBE_FILE.read_text())
    except Exception:
        pass
    fresh = (
        not force
        and isinstance(cached, dict)
        and isinstance(cached.get("checked_at"), (int, float))
        and now - cached["checked_at"] < PROBE_TTL_SECONDS
    )
    if not fresh:
        token = read_probe_token()
        if not token:
            return None
        try:
            result = run_probe(token)
        except Exception as err:
            result = {"http": None, "headers": {}, "error": type(err).__name__}
        cached = {"checked_at": now, **result}
        tmp = PROBE_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cached))
        tmp.replace(PROBE_FILE)

    h = cached.get("headers") or {}
    if not h:
        return None  # auth failure, network error, or no plan headers: unknown

    def num(name: str) -> float | None:
        try:
            return float(h[HDR + name])
        except (KeyError, TypeError, ValueError):
            return None

    windows: dict[str, dict] = {}
    over = False
    for name, prefix in (("five_hour", "5h"), ("seven_day", "7d")):
        util = num(f"{prefix}-utilization")
        if util is None:
            continue
        pct = util * 100.0 if util <= 1.5 else util  # fraction 0-1 per the CLI's thresholds
        resets = num(f"{prefix}-reset")
        expired = resets is not None and now >= resets
        windows[name] = {"pct": pct, "resets_at": resets, "expired": expired}
        if not expired and pct >= over_pct:
            over = True
    overage_in_use = str(h.get(HDR + "overage-in-use", "")).lower() == "true"
    status = str(h.get(HDR + "status", "")).lower()
    if overage_in_use or status == "rejected":
        over = True
        windows.setdefault("overage", {"pct": 100.0, "resets_at": num("reset"), "expired": False})
    if not windows and not status:
        return None
    return {"over": over, "windows": windows, "source": "probe", "status": status,
            "overage_in_use": overage_in_use}


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
    limits = probe_limits(now, over_pct) or read_limits(now, over_pct)
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
                live = [w for w in limits["windows"].items() if not w[1]["expired"] and w[0] != "overage"]
                worst = max(live, key=lambda kv: kv[1]["pct"]) if live else None
                where = ""
                if worst:
                    label = {"five_hour": "5-hour", "seven_day": "7-day", "spend_limit": "spend"}.get(worst[0], worst[0])
                    where = f" ({label} window at {worst[1]['pct']:.0f}%, resets {fmt_reset(worst[1]['resets_at'])})"
                if limits.get("overage_in_use"):
                    where += " and the API reports usage credits in use"
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
                "session-budget-gate: no plan-limit data, so usage credits are NOT being gated. "
                "Fix: run `claude setup-token`, then `security add-generic-password -U -s "
                f"{KEYCHAIN_SERVICE} -a \"$USER\" -w` and paste the token; check with "
                "`~/.claude/hooks/session-budget-gate.py --check`."
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


def check() -> int:
    """Manual diagnostic: force a probe and print what the gate sees (no secrets)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not read_probe_token():
        print(f"No token: Keychain service '{KEYCHAIN_SERVICE}' is empty and CLAUDE_BUDGET_GATE_TOKEN is unset.")
        return 1
    over_pct = float(os.environ.get("CLAUDE_SESSION_BUDGET_LIMIT_PCT") or 100.0)
    limits = probe_limits(time.time(), over_pct, force=True)
    probe = json.loads(PROBE_FILE.read_text())
    print(f"HTTP {probe.get('http')} {probe.get('error') or ''}".rstrip())
    for key, value in sorted((probe.get("headers") or {}).items()):
        print(f"  {key}: {value}")
    if limits is None:
        print("Result: UNKNOWN (no usable headers) -> gate falls back to doing nothing")
        return 1
    print("Result:", "OVER PLAN / ON CREDITS -> will ask" if limits["over"] else "within plan -> silent")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:  # tolerate extra args from a multi-match glob
        sys.exit(check())
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
