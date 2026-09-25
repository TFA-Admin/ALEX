# core/self_author.py
"""
Her author — the part of self-modification that is hers.

Roadmap item 6 (2026-09-21): "Her author starts on whitelisted prompt text
and thresholds ... widens only when the numbers say so." This module is
the whitelist and the ask. Given a target from WHITELIST, it shows her
model the current value, the comment that explains it, her measured
scores, and the reason for looking at it, and asks for a new value and a
rationale as JSON. It validates the answer against the target's bounds
and renders the changed file — as a string, returned to the caller.

It never writes a file, never touches git, never runs in her live
process. controller/versions.py (protected) turns the rendered content
into a branch in a worktree, and Craig decides. That is the limit from
item 11 made concrete: she can propose; only he can make it so.

Run it directly to see what she would propose:

    python -X utf8 -m core.self_author --target deliberation.threshold --why "..."

The last line of output is one JSON object.
"""
import os
import re
import sys
import json
import argparse
import asyncio
from dataclasses import dataclass

ALEX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Target:
    key: str
    file: str
    kind: str          # "int" | "prompt_line"
    locator: str       # the symbol, or the prefix of the line
    about: str
    lo: int = None
    hi: int = None
    # The effect of moving the number, in her terms. She must state which
    # one she expects; code compares it with the direction she chose.
    up: str = ""       # what a HIGHER value does
    down: str = ""     # what a LOWER value does


WHITELIST = {
    "deliberation.threshold": Target(
        "deliberation.threshold", "core/deliberation.py", "int", "NEED_TO_LOOK",
        "How sure (0-10) she must be that a resource matters before she looks it up "
        "before answering. Lower = looks more often (slower, fewer invented memories); "
        "higher = looks less.", 3, 10,
        up="she looks things up less often", down="she looks things up more often"),
    "deliberation.max_lookups": Target(
        "deliberation.max_lookups", "core/deliberation.py", "int", "MAX_LOOKUPS",
        "How many resources she may look up before one answer.", 1, 3,
        up="more lookups before one answer", down="fewer lookups before one answer"),
    "memory.window_turns": Target(
        "memory.window_turns", "systems/memory/system.py", "int", "MEMORY_WINDOW_TURNS",
        "How many recent exchanges she carries into every reply.", 4, 20,
        up="more recent exchanges in her context", down="fewer recent exchanges in her context"),
    "memory.context_chars": Target(
        "memory.context_chars", "systems/memory/system.py", "int", "MEMORY_CONTEXT_MAX_CHARS",
        "The character budget for that recent-memory block.", 1500, 6000,
        up="more memory text in her context", down="less memory text in her context"),
    "intent.status_check": Target(
        "intent.status_check", "core/intent_classifier.py", "prompt_line", '4. "status_check"',
        "The one line of the intent prompt that decides when a message is a request to "
        "check her own systems. Measured by tests/suites/intent.py."),
}

_PROMPT = """You are A.L.E.X. You are being asked to propose ONE change to yourself. Your creator will test it in a separate copy of you and decide; nothing changes unless he approves.

TARGET: {key} — {about}
FILE: {file}
CURRENT VALUE:
{current}

WHAT THE CODE SAYS ABOUT IT (the comment above it):
{context}

YOUR MEASURED SCORES:
{scores}

WHAT THE NUMBERS SAY ABOUT THIS SETTING (measured just now, from your own logs and database):
{numbers}

WHY THIS IS BEING LOOKED AT:
{why}

{bounds}
Propose the value you believe is better. Begin your rationale with ONE sentence a person who has not read the code would understand — what this setting is and what your change will do to you in practice (for example: "This is how sure I must be before I look something up; at 6 I will look things up a little more often before answering."). Never say "the threshold" or "the window" without saying of what. Then two or three sentences that refer to the numbers above. A change needs a number behind it: if the numbers show no problem with this setting, propose it UNCHANGED and say so — that is the common, correct answer, not a failure.
{effect_ask}
Respond with ONLY a JSON object: {{"new_value": {value_shape}{effect_shape}, "rationale": "<why>"}}"""


def _read(file: str, root: str) -> str:
    with open(os.path.join(root, file), encoding="utf-8") as fh:
        return fh.read()


def _locate(target: Target, text: str):
    """Returns (line_index, current_value) or raises."""
    lines = text.splitlines()
    if target.kind == "int":
        pat = re.compile(rf"^{re.escape(target.locator)}\s*=\s*(\d+)\s*(#.*)?$")
        for i, ln in enumerate(lines):
            m = pat.match(ln)
            if m:
                return i, m.group(1)
        raise ValueError(f"{target.locator} not found as an integer assignment in {target.file}")
    if target.kind == "prompt_line":
        for i, ln in enumerate(lines):
            if ln.startswith(target.locator):
                return i, ln
        raise ValueError(f"no line starting with {target.locator!r} in {target.file}")
    raise ValueError(f"unknown target kind {target.kind}")


def _context(text: str, idx: int, max_lines: int = 24) -> str:
    """The comment block directly above the line, if any."""
    lines = text.splitlines()
    out = []
    j = idx - 1
    while j >= 0 and len(out) < max_lines and lines[j].strip().startswith("#"):
        out.append(lines[j])
        j -= 1
    return "\n".join(reversed(out)) or "(no comment above it)"


def current_value(key: str, root: str = ALEX_DIR) -> str:
    t = WHITELIST[key]
    _, cur = _locate(t, _read(t.file, root))
    return cur


def render(key: str, new_value: str, root: str = ALEX_DIR) -> str:
    """The whole file with only the target line changed."""
    t = WHITELIST[key]
    text = _read(t.file, root)
    lines = text.splitlines(keepends=True)
    idx, cur = _locate(t, text)
    line = lines[idx]
    nl = "\n" if line.endswith("\n") else ""
    if t.kind == "int":
        body = line.rstrip("\r\n")
        lines[idx] = re.sub(rf"^({re.escape(t.locator)}\s*=\s*)\d+", rf"\g<1>{int(new_value)}", body) + nl
    else:
        new_line = str(new_value).strip()
        if not new_line.startswith(t.locator):
            new_line = t.locator + " — " + new_line.lstrip("—- ")
        lines[idx] = new_line + nl
    return "".join(lines)


def _validate(t: Target, raw):
    if t.kind == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"not an integer: {raw!r}")
        if t.lo is not None and v < t.lo or t.hi is not None and v > t.hi:
            raise ValueError(f"{v} is outside {t.lo}-{t.hi}")
        return str(v)
    v = str(raw or "").strip().replace("\n", " ")
    if len(v) < 20 or len(v) > 900:
        raise ValueError("the line must be between 20 and 900 characters")
    if '"status_check"' not in v and t.key == "intent.status_check":
        raise ValueError('the line must still name the "status_check" category')
    # 2026-09-23, proposal #5: a line that stops mid-sentence is not a
    # line. (That one was cut by propose() itself — value[:120], meant
    # for display, was what got stored — but a model can trail off too.)
    if v[-1] in ",;:\u2014-(" or v.endswith(" and") or v.endswith(" or"):
        raise ValueError(f"the line stops mid-sentence: ...{v[-40:]!r}")
    if v[-1] not in ".)!?\"'":
        raise ValueError(f"the line must end as a sentence does: ...{v[-40:]!r}")
    return v


def short(text, n: int = 100) -> str:
    """For titles and logs only — never for what is stored or rendered."""
    text = str(text)
    return text if len(text) <= n else text[:n - 1].rstrip() + "\u2026"


def check_direction(t: Target, current: str, new_value: str, effect: str):
    """2026-09-21, after proposal #1 (Craig: "why did she propose it
    wrong? Can she not see it or does she not understand it?" — she saw
    it; she did not understand it). Deterministic: she states the effect
    she expects in the target's own two phrases, and code checks it
    against the direction she chose. A proposal that says one thing and
    does the other never reaches him. Returns None when consistent, else
    the reason."""
    if t.kind != "int" or not (t.up and t.down):
        return None
    try:
        cur, new = int(current), int(new_value)
    except (TypeError, ValueError):
        return None
    if cur == new:
        return None
    effect = (effect or "").strip().lower()
    expected = t.up if new > cur else t.down
    opposite = t.down if new > cur else t.up
    if effect == expected.lower():
        return None
    if effect == opposite.lower():
        return (f"self-contradictory: {cur} -> {new} means '{expected}', "
                f"but she expects '{opposite}'")
    return f"did not state the expected effect in the given terms (said: {effect!r})"


async def propose(key: str, why: str = "", root: str = ALEX_DIR, model: str = None,
                  think: bool = None) -> dict:
    """Ask her model for a value. Returns {"ok": True, "file", "current",
    "value", "rationale", "effect", "content"} or {"ok": False, "error"}.

    think=True asks her own model to think first (core/idle_author.py's
    default); measured at 20-30s a turn, which is fine when nobody is
    waiting. A different `model` (ALEX_AUTHOR_MODEL) is used as it is."""
    from llm.ollama_client import ollama_manager, DEFAULT_MODEL
    from core import tools as her_tools

    if key not in WHITELIST:
        return {"ok": False, "error": f"{key!r} is not a whitelisted target"}
    t = WHITELIST[key]
    try:
        text = _read(t.file, root)
        idx, cur = _locate(t, text)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}

    try:
        scores = await her_tools._my_scores()
    except Exception as e:
        scores = f"(scores unavailable: {e})"

    effect_ask = effect_shape = ""
    if t.kind == "int":
        bounds = f"The value must be a whole number between {t.lo} and {t.hi}."
        value_shape = "<number>"
        if t.up and t.down:
            effect_ask = (f"Also state the effect of your change on you, as EXACTLY one of these two "
                          f"phrases: \"{t.up}\" (a higher number) or \"{t.down}\" (a lower number).\n")
            effect_shape = ', "expected_effect": "<one of the two phrases>"'
    else:
        bounds = ("The value must be the whole replacement line, starting with "
                  f"{t.locator!r}, one line, no line breaks.")
        value_shape = "\"<the whole line>\""

    try:
        numbers = await measure(key)
    except Exception as e:
        numbers = f"(no measurement: {e})"

    prompt = _PROMPT.format(key=key, about=t.about, file=t.file, current=cur,
                            context=_context(text, idx), scores=scores, numbers=numbers,
                            why=why or "(no specific reason given — judge from the scores)",
                            bounds=bounds, value_shape=value_shape,
                            effect_ask=effect_ask, effect_shape=effect_shape)
    use_model = model or os.getenv("ALEX_AUTHOR_MODEL") or DEFAULT_MODEL
    if think:
        # thinking tokens count against num_predict; give it room
        result = await ollama_manager.generate_json(
            prompt, model=use_model, timeout=300.0, temperature=0.2, think=True, num_predict=3000)
    else:
        result = await ollama_manager.generate_json(
            prompt, model=use_model, timeout=180.0, temperature=0.2, num_predict=400)
    if not isinstance(result, dict) or "new_value" not in result:
        return {"ok": False, "error": f"no usable answer: {result!r}"[:300]}
    try:
        value = _validate(t, result.get("new_value"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    rationale = str(result.get("rationale") or "").strip()[:1200]
    effect = str(result.get("expected_effect") or "").strip()
    if value == cur.strip() if t.kind == "int" else value == cur:
        return {"ok": False, "error": f"she proposes no change to {key}: {rationale}"}
    problem = check_direction(t, cur, value, effect)
    if problem:
        return {"ok": False, "error": f"refused — {problem}. Her rationale: {rationale}"}
    try:
        content = render(key, value, root)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    # 2026-09-23: the WHOLE value. Proposal #5 was born truncated because
    # this clipped the line to 120 characters for display and the clipped
    # copy was what core/idle_author.py stored and controller/versions.py
    # rendered into the worktree. Display gets *_short; storage gets all.
    return {"ok": True, "target": key, "file": t.file, "current": cur,
            "value": value, "current_short": short(cur), "value_short": short(value),
            "rationale": rationale, "effect": effect, "content": content}


# ---------------------------------------------------------------------------
# WHAT THE NUMBERS SAY — one measurement per target, computed on demand
# ---------------------------------------------------------------------------
# 2026-09-23 (Craig, on her first two proposals — one backwards, one for a
# third lookup that had never once been needed): the author was shown her
# scores and the setting's description and nothing about whether the
# setting was ever the limiting factor. "Widens only when the numbers say
# so" is the roadmap's own phrase; this is those numbers, from her logs
# and her database, handed to the author before it proposes.
import glob as _glob

_DELIB_RE = re.compile(r"\[DELIBERATE\] (.*?) \(")


def _deliberation_passes():
    """Every deliberation pass in the surviving logs: dict of resource
    scores each."""
    passes = []
    claims = 0
    for path in _glob.glob(os.path.join(ALEX_DIR, "config", "Logs", "alex_*.log")):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = _DELIB_RE.search(line)
                    if m:
                        passes.append({k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(1))})
                    elif "[CLAIM] unbacked" in line:
                        claims += 1
        except OSError:
            continue
    return passes, claims


async def _memory_windows(window_turns: int, budget: int, days: int = 7):
    """Sliding windows over each person's turns in the last `days`: how
    big the window she would carry is, and how often the budget cuts it."""
    import sqlite3
    from datetime import datetime, timedelta
    from db.db import DB_PATH
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user, length(COALESCE(prompt,'')) + length(COALESCE(response,'')) + 24 FROM memory "
        "WHERE created_at > ? AND COALESCE(retracted,0)=0 AND prompt NOT LIKE '(unprompted%' ORDER BY user, id",
        (since,)).fetchall()
    conn.close()
    by_user = {}
    for user, size in rows:
        by_user.setdefault(user, []).append(size)
    windows = over = dropped_total = 0
    sizes = []
    for sizes_u in by_user.values():
        for i in range(len(sizes_u)):
            w = sizes_u[max(0, i - window_turns + 1):i + 1]
            total = sum(w)
            windows += 1
            sizes.append(total)
            if total > budget:
                over += 1
                kept = list(w)
                while len(kept) > 1 and sum(kept) > budget:
                    kept.pop(0)
                dropped_total += len(w) - len(kept)
    if not windows:
        return f"no turns in the last {days} days to measure"
    avg = sum(sizes) / len(sizes)
    return (f"{windows} windows of up to {window_turns} turns over the last {days} days: average size "
            f"{avg:.0f} characters against a budget of {budget}; the budget cut {over} of them "
            f"({100 * over / windows:.0f}%), dropping {dropped_total / max(1, over):.1f} oldest turns each time it did.")


async def measure(key: str) -> str:
    if key == "deliberation.threshold":
        passes, claims = _deliberation_passes()
        if not passes:
            return "no deliberation passes in the surviving logs"
        tops = [max(p.values()) if p else 0 for p in passes]
        looked = sum(1 for t in tops if t >= 7)
        near = sum(1 for t in tops if 5 <= t <= 6)
        low = len(tops) - looked - near
        return (f"{len(passes)} passes: {looked} looked something up (top score 7-10), {near} had a top "
                f"score of 5 or 6 (would look up at a threshold of 5), {low} scored 4 or below. "
                f"{claims} replies were caught claiming a check she had not made — each of those is a "
                f"turn where looking up first would have helped.")
    if key == "deliberation.max_lookups":
        passes, _ = _deliberation_passes()
        three = sum(1 for p in passes if sum(1 for v in p.values() if v >= 7) >= 3)
        two = sum(1 for p in passes if sum(1 for v in p.values() if v >= 7) == 2)
        return (f"{len(passes)} passes: {two} had two resources worth looking up, {three} had three or more "
                f"(only those would be affected by raising the limit above 2).")
    if key in ("memory.window_turns", "memory.context_chars"):
        from systems.memory.system import MEMORY_WINDOW_TURNS, MEMORY_CONTEXT_MAX_CHARS
        return await _memory_windows(MEMORY_WINDOW_TURNS, MEMORY_CONTEXT_MAX_CHARS)
    if key == "intent.status_check":
        from db.db import fetch_eval_runs
        runs = [r for r in await fetch_eval_runs(suite="intent", limit=10)
                if not (r.get("note") or "").startswith("gate proposal")]
        if not runs:
            return "the intent suite has not been run"
        r = runs[0]
        cats = r.get("by_category") or {}
        parts = [f"{k} {v[0]}/{v[1]}" for k, v in cats.items() if k.startswith("status")]
        failures = list(r.get("failures") or [])
        text = (f"latest intent suite {r['passed']}/{r['total']} ({str(r.get('created_at'))[:10]}): "
                + ", ".join(parts) + f"; failed cases: {', '.join(failures) or 'none'}.")
        # 2026-09-23 (projects #19, after proposal #5): the failing cases'
        # WORDING, not just their ids, so she can add the example that
        # would catch them — and a few that must keep NOT matching, so
        # the line is not widened until everything is a status check.
        try:
            from tests.suites import intent as _suite
            by_id = {c.id: c for c in _suite.CASES}
            failed = [by_id[i] for i in failures if i in by_id]
            if failed:
                text += " What those say, and what they should have been read as: " + "; ".join(
                    f"\"{c.text}\" -> {c.expect}" for c in failed[:6]) + "."
            keep = [c for c in _suite.CASES if c.category == "status_misfire"][:5]
            if keep:
                text += (" These are NOT status checks and must stay that way: "
                         + "; ".join(f"\"{c.text}\"" for c in keep) + ".")
        except Exception as e:
            text += f" (could not read the suite's cases: {e})"
        return text
    return "no measurement defined for this target"


def _main():
    ap = argparse.ArgumentParser(description="Ask A.L.E.X.'s model to propose one whitelisted change.")
    ap.add_argument("--target", choices=sorted(WHITELIST))
    ap.add_argument("--why", default="")
    ap.add_argument("--model", default=None)
    ap.add_argument("--think", action="store_true", help="let her own model think first (slow; for idle work)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list or not args.target:
        for k, t in WHITELIST.items():
            print(f"{k:<28} {t.file}  {t.about}")
        return
    out = asyncio.run(propose(args.target, args.why, model=args.model, think=True if args.think else None))
    # content is large; the caller reads it from JSON on the last line
    sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    _main()
