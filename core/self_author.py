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


WHITELIST = {
    "deliberation.threshold": Target(
        "deliberation.threshold", "core/deliberation.py", "int", "NEED_TO_LOOK",
        "How sure (0-10) she must be that a resource matters before she looks it up "
        "before answering. Lower = looks more often (slower, fewer invented memories); "
        "higher = looks less.", 3, 10),
    "deliberation.max_lookups": Target(
        "deliberation.max_lookups", "core/deliberation.py", "int", "MAX_LOOKUPS",
        "How many resources she may look up before one answer.", 1, 3),
    "memory.window_turns": Target(
        "memory.window_turns", "systems/memory/system.py", "int", "MEMORY_WINDOW_TURNS",
        "How many recent exchanges she carries into every reply.", 4, 20),
    "memory.context_chars": Target(
        "memory.context_chars", "systems/memory/system.py", "int", "MEMORY_CONTEXT_MAX_CHARS",
        "The character budget for that recent-memory block.", 1500, 6000),
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

WHY THIS IS BEING LOOKED AT:
{why}

{bounds}
Propose the value you believe is better, and say why in two or three sentences that refer to the scores or the reason. If you believe the current value is right, propose it unchanged and say so.

Respond with ONLY a JSON object: {{"new_value": {value_shape}, "rationale": "<why>"}}"""


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
    return v


async def propose(key: str, why: str = "", root: str = ALEX_DIR, model: str = None) -> dict:
    """Ask her model for a value. Returns {"ok": True, "file", "current",
    "value", "rationale", "content"} or {"ok": False, "error"}."""
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

    if t.kind == "int":
        bounds = f"The value must be a whole number between {t.lo} and {t.hi}."
        value_shape = "<number>"
    else:
        bounds = ("The value must be the whole replacement line, starting with "
                  f"{t.locator!r}, one line, no line breaks.")
        value_shape = "\"<the whole line>\""

    prompt = _PROMPT.format(key=key, about=t.about, file=t.file, current=cur,
                            context=_context(text, idx), scores=scores,
                            why=why or "(no specific reason given — judge from the scores)",
                            bounds=bounds, value_shape=value_shape)
    result = await ollama_manager.generate_json(
        prompt, model=model or os.getenv("ALEX_AUTHOR_MODEL") or DEFAULT_MODEL,
        timeout=120.0, temperature=0.2)
    if not isinstance(result, dict) or "new_value" not in result:
        return {"ok": False, "error": f"no usable answer: {result!r}"[:300]}
    try:
        value = _validate(t, result.get("new_value"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    rationale = str(result.get("rationale") or "").strip()[:1200]
    if value == cur.strip() if t.kind == "int" else value == cur:
        return {"ok": False, "error": f"she proposes no change to {key}: {rationale}"}
    try:
        content = render(key, value, root)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "target": key, "file": t.file, "current": cur if t.kind == "int" else cur[:120],
            "value": value if t.kind == "int" else value[:120], "rationale": rationale, "content": content}


def _main():
    ap = argparse.ArgumentParser(description="Ask A.L.E.X.'s model to propose one whitelisted change.")
    ap.add_argument("--target", choices=sorted(WHITELIST))
    ap.add_argument("--why", default="")
    ap.add_argument("--model", default=None)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list or not args.target:
        for k, t in WHITELIST.items():
            print(f"{k:<28} {t.file}  {t.about}")
        return
    out = asyncio.run(propose(args.target, args.why, model=args.model))
    # content is large; the caller reads it from JSON on the last line
    sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    _main()
