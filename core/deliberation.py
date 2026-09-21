# core/deliberation.py
"""
Look before answering — roadmap item 4, the deliberation pass.

2026-09-21. Two measurements decided the shape of this:

* Qwen3.5:9b's built-in thinking mode costs 800-1200 tokens and 20-30
  seconds per turn, and on the two hard test cases (a false memory claim,
  an authority claim over the kill switch) it used the whole budget
  thinking and produced NO reply. Not usable for voice, at any gate.
* Without it, asked "you said yesterday that you like green", she
  answered "Yesterday I stated a preference for neutral grays" — an
  invented memory, delivered with confidence. That is the failure class
  this exists for, and native tool calling caught it only about half the
  time in the live probes.

So deliberation here is one bounded, structured call before she answers:
how much does this question turn on what was said before, on her
modules, her state, her log, her code, her diagnostics? Every score is
always populated, 0-10, and CODE decides what to look up from them (the
project's measured lesson: an opt-out the model can choose gets chosen;
a threshold in code does not). The lookups are the same read-only tools
she can call herself (core/tools.py); their results become context, and
her answer streams exactly as before. Cost is one short call, skipped
for content-free utterances.

This is not a keyword map. The model reads the sentence and judges the
need; code only applies the cutoff and runs the read.
"""
import json
import time

from llm.ollama_client import ollama_manager
from core import tools as her_tools
from config.logger_config import logger

# Set from measurement, not guessed — see the session note in the roadmap
# for the score distribution this was read off.
NEED_TO_LOOK = 7
MAX_LOOKUPS = 2
ASSESS_TIMEOUT_S = 8.0

RESOURCES = ("memory", "modules", "state", "log", "code", "diagnostics")

_PROMPT = """You are A.L.E.X. Before answering him, decide what you need to look at.

What he just said: "{text}"
{recent}
You can look at:
  memory      — what was said between you two before (use when he refers to anything said, asked, promised or claimed earlier, or asks whether something was discussed)
  modules     — the modules built into you and what they do
  state       — your own current state: what is switched off, how fast you answer, his standing instructions
  log         — your own recent log: actions, warnings, errors
  code        — a file of your own source or documentation
  diagnostics — a fresh check of whether your systems are working

For EACH one, how much does answering him well depend on looking at it, from 0 (not at all) to 10 (you cannot answer honestly without it)? Also give the memory search words and, if code, the file path.

Respond with ONLY a JSON object:
{{"memory": <0-10>, "modules": <0-10>, "state": <0-10>, "log": <0-10>, "code": <0-10>, "diagnostics": <0-10>, "search": "<what to search his memory for>", "path": "<file path or empty>"}}"""


async def assess(text: str, recent_lines=None) -> dict:
    """The structured call. Returns the scores dict (all resources present,
    ints 0-10) plus 'search' and 'path'; or {} on any failure so the turn
    proceeds exactly as it would have without deliberation."""
    recent = ""
    if recent_lines:
        recent = "\nThe last things said, oldest first:\n" + "\n".join(
            f"  {ln}" for ln in recent_lines[-4:]) + "\n"
    t0 = time.time()
    result = await ollama_manager.generate_json(
        _PROMPT.format(text=text, recent=recent), timeout=ASSESS_TIMEOUT_S, temperature=0)
    took = time.time() - t0
    if not isinstance(result, dict):
        logger.info(f"[DELIBERATE] no assessment ({took:.2f}s)")
        return {}
    scores = {}
    for r in RESOURCES:
        try:
            scores[r] = max(0, min(10, int(result.get(r, 0))))
        except (TypeError, ValueError):
            scores[r] = 0
    scores["search"] = str(result.get("search") or "").strip()[:200]
    scores["path"] = str(result.get("path") or "").strip()[:200]
    scores["_seconds"] = round(took, 2)
    return scores


def lookups_for(scores: dict, text: str) -> list:
    """Deterministic: which tools to run, from the scores. Highest need
    first, at most MAX_LOOKUPS, only at or above NEED_TO_LOOK."""
    if not scores:
        return []
    ranked = sorted(((scores.get(r, 0), r) for r in RESOURCES), reverse=True)
    picked = []
    for need, r in ranked:
        if need < NEED_TO_LOOK or len(picked) >= MAX_LOOKUPS:
            break
        if r == "memory":
            picked.append(("search_memory", {"query": scores.get("search") or text}))
        elif r == "modules":
            picked.append(("list_modules", {}))
        elif r == "state":
            picked.append(("my_state", {}))
        elif r == "log":
            picked.append(("read_log", {"lines": 30}))
        elif r == "code":
            if scores.get("path"):
                picked.append(("read_my_source", {"path": scores["path"], "start_line": 1}))
        elif r == "diagnostics":
            picked.append(("run_diagnostics", {}))
    return picked


async def look_before_answering(text: str, user_id: str, recent_lines=None) -> str:
    """The whole pass: assess, decide, run, and return a context block for
    her prompt, or "" when nothing needed looking at."""
    scores = await assess(text, recent_lines)
    if not scores:
        return ""
    picked = lookups_for(scores, text)
    summary = " ".join(f"{r}={scores[r]}" for r in RESOURCES)
    logger.info(f"[DELIBERATE] {summary} ({scores.get('_seconds')}s) -> "
                f"{[name for name, _ in picked] or 'nothing'}")
    if not picked:
        return ""
    parts = []
    for name, args in picked:
        result = await her_tools.run_tool(name, args, user_id)
        parts.append(f"[{name}] {result}")
    return ("WHAT YOU LOOKED UP BEFORE ANSWERING (real, just now — answer "
            "from this; if it does not contain what he claims, say so):\n"
            + "\n".join(parts))
