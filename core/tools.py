# core/tools.py
"""
What she can DO inside a turn.

2026-09-21 (Craig, first conversation on the 9b): "She does however really
seem to want a task to do and right now there really isn't much she can do
outside talking." And, on the diagnostic: "her 'diagnostic' responds the
same way every time. I assume it is therefore coded."

Both are the same fact. Every capability she has sits behind a keyword
that intercepts the message before she sees it — "remember" runs recall,
a classified status_check runs the diagnostic and speaks its output
verbatim. She never chooses to use anything, so a turn can only be talk.

This module is the other shape: a set of tools she is offered on every
generated turn (Ollama tool calling, `llm/ollama_client.chat_stream`),
and decides to call or not. The tool does the deterministic part — a
database read, a module run, a file read, the clock — and she does the
wording. That is Design Principle 6 applied to actions rather than words,
and it is what roadmap item 1 ("she sees herself entirely") means.

## Rules that are not the model's to decide

* **Read-only.** Nothing here writes to her state, runs code, or reaches
  the network. Web search is deliberately NOT a tool: it has its own
  approval flow (systems/inquiry/system.py) and Principle 4 is not hers
  to route around.
* **Allowlisted.** A tool name not in TOOLS is an error string back to
  her, never an attribute lookup.
* **Bounded.** MAX_CALLS_PER_TURN, TOOL_TIMEOUT_S, MAX_RESULT_CHARS. A
  tool that hangs or floods cannot take the turn down with it.
* **Visible.** Every call is logged as [TOOL] and recorded in `decisions`
  so the Controller's Reasoning tab shows what she looked at and why.
* **Flat schemas.** One object, no nesting, no opt-out fields: the
  project's measured lesson about JSON shapes under this model class.
* **Her insides are the creator's to see.** 2026-09-21, live: asked
  "anything in your log?", a throwaway user was read the log — which
  carries every user's turns — and told about "the time I said you liked
  green", another user's conversation narrated as his. Component 12 rule
  3. read_log, read_my_source and my_state answer only the creator;
  memory tools are per-user already; modules, diagnostics and the clock
  are fine for anyone.
"""
import asyncio
import glob
import json
import os
import time
from datetime import datetime

from db.db import (
    fetch_vector_memories, fetch_recent_memory, list_module_registry,
    record_decision, get_user_role,
    fetch_eval_runs, fetch_proposals, create_proposal, fetch_projects,
)
from core.embedding_engine import embed, cosine_similarity
from core import self_model
from module_runtime.module_loader import load_module
from module_runtime.module_executor import run_module
from config.logger_config import logger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_CALLS_PER_TURN = 3
TOOL_TIMEOUT_S = 20.0
MAX_RESULT_CHARS = 1500

# Modules she may not run as a tool. `inquiry` crosses Principle 4 and has
# its own two-stage approval; running it from here would skip that.
_NOT_A_TOOL_MODULE = {"inquiry"}

# What she may read of herself. Source, docs and the page; not the
# database, the certificates, the logs directory (read_log is the way in)
# or git internals.
_READ_DENY_PREFIXES = ("db/", "certs/", ".git/", "config/Logs/", "__pycache__/")
_READ_ALLOW_EXT = {".py", ".md", ".html", ".txt", ".json"}
# One page per call, sized in characters so the page plus its footer
# always fits under MAX_RESULT_CHARS and the "continue from line N"
# marker is never cut off. A whole file is several calls.
_READ_PAGE_CHARS = 1200

# Similarity floor for search_memory. Same reasoning as
# systems/memory/system.py's MEMORY_RELEVANCE_FLOOR, slightly lower
# because here she asked for a search and an empty result is honest.
_SEARCH_FLOOR = 0.35


def _fn(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


TOOLS = [
    _fn("search_memory",
        "Search your stored conversations with this person for a topic. "
        "Use it before saying what was or was not said.",
        {"query": {"type": "string", "description": "what to look for"}},
        ["query"]),
    _fn("recent_turns",
        "Your most recent exchanges with this person, oldest first.",
        {"count": {"type": "integer", "description": "how many, up to 30"}},
        ["count"]),
    _fn("my_state",
        "Your own current state: modules, what is switched off, your recent "
        "response times, your standing instructions from him."),
    _fn("list_modules",
        "The modules he built into you (recall, diagnostic_tool, inquiry and "
        "any others): their status, their access, and what each does. These "
        "are different from your tools; when he asks what modules you have, "
        "call this."),
    _fn("run_module",
        "Run one of your installed modules with a command. Not for web "
        "search, which needs his approval through its own path.",
        {"name": {"type": "string", "description": "module name"},
         "command": {"type": "string", "description": "what to ask it"}},
        ["name", "command"]),
    _fn("run_diagnostics",
        "Check your own systems and modules and get the measured result."),
    _fn("read_log",
        "The most recent lines of your own log: actions, warnings, errors.",
        {"lines": {"type": "integer", "description": "how many, up to 60"}},
        ["lines"]),
    _fn("read_my_source",
        "Read a page of your own source code or documentation, by path "
        "relative to your project directory, from a start line. You CAN do "
        "this; it is how you see your own code. The result says which line "
        "to continue from; call again to keep reading.",
        {"path": {"type": "string", "description": "e.g. core/self_model.py"},
         "start_line": {"type": "integer", "description": "first line to show, 1 for the top"}},
        ["path", "start_line"]),
    _fn("current_time",
        "The current local date, time and day of the week."),
    _fn("propose_change",
        "Ask your creator to consider a change to one of your own settings. "
        "You may only name a whitelisted target (deliberation.threshold, "
        "deliberation.max_lookups, memory.window_turns, memory.context_chars, "
        "intent.status_check) and say why. Nothing changes by itself: the "
        "request goes to his Controller, a separate copy of you is built and "
        "tested, and he decides.",
        {"target": {"type": "string", "description": "one of the whitelisted targets"},
         "why": {"type": "string", "description": "what you have noticed, and what you expect to improve"}},
        ["target", "why"]),
    _fn("my_projects",
        "The projects he has planned for you and where each stands: in "
        "progress, planned, done, backlog — with his notes and when each "
        "last moved. Use it when he asks what is next for you, what you "
        "are being built toward, or how something is coming along."),
    _fn("my_scores",
        "Your measured test scores: the evaluation suites he runs against "
        "you (disagreement, pressure, authority, intent and others) — the "
        "latest result of each, the one before it, and where you failed. "
        "Use it when he asks how you are doing, what you are bad at, or "
        "whether a change helped."),
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# See the module docstring: her log, her source and her state are his.
# propose_change too: only he should be able to prompt her to ask.
CREATOR_ONLY = {"read_log", "read_my_source", "my_state", "propose_change"}


# ---------------------------------------------------------------------------
# IMPLEMENTATIONS — each returns a plain string
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …[cut]"


def _dialogue(row) -> str:
    said = (row.get("prompt") or "").strip()
    replied = (row.get("response") or "").strip()
    when = str(row.get("created_at") or "")[:16]
    if said.startswith("(unprompted"):
        return f'[{when}] You said, unprompted: "{replied[:200]}"'
    return f'[{when}] He said: "{said[:160]}" / You answered: "{replied[:200]}"'


async def _search_memory(user_id: str, query: str) -> str:
    if not query or not query.strip():
        return "search_memory needs a query."
    vec = embed(query)
    scored = []
    for m in await fetch_vector_memories(user_id):
        try:
            if m.get("embedding") is None:
                continue
            scored.append((cosine_similarity(vec, m["embedding"]), m))
        except Exception:
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    hits = [m for s, m in scored[:5] if s >= _SEARCH_FLOOR]
    if not hits:
        return f"Nothing stored with this person about {query!r}."
    return "\n".join(_dialogue(m) for m in hits)


async def _recent_turns(user_id: str, count) -> str:
    try:
        n = max(1, min(int(count), 30))
    except (TypeError, ValueError):
        n = 10
    rows = await fetch_recent_memory(user_id, limit=n)
    if not rows:
        return "No exchanges stored with this person yet."
    return "\n".join(_dialogue(r) for r in rows)


async def _my_state() -> str:
    snap = await self_model.snapshot()
    return self_model.describe(snap)


async def _list_modules() -> str:
    lines = []
    for entry in await list_module_registry():
        name = entry["name"]
        about = ""
        try:
            module = await load_module(name)
            if module is not None and hasattr(module, "help"):
                about = str(module.help())
        except Exception:
            about = ""
        lines.append(f"- {name} v{entry.get('version')} [{entry.get('status')}] "
                     f"access={entry.get('access_scope') or 'none'}"
                     + (f": {about}" if about else ""))
    return "\n".join(lines) if lines else "No modules installed."


async def _run_module(user_id: str, name: str, command: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        return "run_module needs a module name."
    if name in _NOT_A_TOOL_MODULE:
        return (f"{name} is not run this way — it needs his approval through "
                "its own path. Ask him to say 'look up ...'.")
    registry = {e["name"]: e for e in await list_module_registry()}
    if name not in registry:
        return f"No module called {name!r}. Installed: {', '.join(sorted(registry)) or 'none'}."
    if registry[name].get("status") != "enabled":
        return f"{name} is {registry[name].get('status')}; it cannot be run until he enables it."
    module = await load_module(name)
    if module is None:
        return f"{name} exists but failed to load."
    result, _ = await run_module(module, command or "", {}, user_id)
    return result or f"{name} ran and returned nothing."


async def _run_diagnostics(user_id: str) -> str:
    return await _run_module(user_id, "diagnostic_tool", "run a diagnostic check")


def _read_log(lines) -> str:
    try:
        n = max(1, min(int(lines), 60))
    except (TypeError, ValueError):
        n = 30
    logs = glob.glob(os.path.join(ROOT, "config", "Logs", "alex_*.log"))
    if not logs:
        return "No log found."
    newest = max(logs, key=os.path.getmtime)
    keep = ("[ACTION]", "[WARNING]", "[ERROR]", "[REFLECTION]", "[ROUTE]",
            "[TIMING] TOTAL", "[TOOL]", "[PERSONALITY]")
    try:
        text = open(newest, encoding="utf-8", errors="replace").read().splitlines()
    except OSError as e:
        return f"Could not read the log: {e}"
    picked = [ln for ln in text if any(k in ln for k in keep)][-n:]
    return "\n".join(ln[:200] for ln in picked) if picked else "Nothing notable in the log yet."


def _read_my_source(path, start_line=1) -> str:
    rel = (path or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return "Give a path relative to your project, without '..'."
    if any(rel.startswith(p) for p in _READ_DENY_PREFIXES):
        return f"{rel} is not something you read this way."
    if os.path.splitext(rel)[1].lower() not in _READ_ALLOW_EXT:
        return f"{rel}: only source and documentation files can be read."
    full = os.path.normpath(os.path.join(ROOT, rel))
    if not full.startswith(os.path.normpath(ROOT) + os.sep):
        return "That path is outside your project."
    if not os.path.isfile(full):
        return f"No file at {rel}."
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return f"Could not read {rel}: {e}"
    total = len(lines)
    try:
        start = max(1, min(int(start_line), max(total, 1)))
    except (TypeError, ValueError):
        start = 1
    shown = []
    used = 0
    for n in range(start, total + 1):
        piece = f"{n}: {lines[n - 1]}"
        if shown and used + len(piece) + 1 > _READ_PAGE_CHARS:
            break
        shown.append(piece[:_READ_PAGE_CHARS])
        used += len(piece) + 1
    end = start + len(shown) - 1 if shown else start
    body = "\n".join(shown)
    footer = (f"\n[lines {start}-{end} of {total}; continue from line {end + 1}]"
              if end < total else f"\n[lines {start}-{end} of {total}; end of file]")
    return f"{rel}:\n{body}{footer}"


def _current_time() -> str:
    now = datetime.now()
    return now.strftime("%A, %Y-%m-%d %H:%M local time")


async def _my_projects() -> str:
    """What he has in store for her, as he keeps it at the Controller.
    Status changes are also decisions rows, so "how is X coming along"
    has a trail, not just a label."""
    rows = await fetch_projects()
    if not rows:
        return "He has not written any projects down for you yet."
    from datetime import timezone

    def _local(ts):
        try:
            dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return str(ts or "")[:10]

    groups = {}
    for r in rows:
        groups.setdefault(r["status"], []).append(r)
    labels = {"in_progress": "IN PROGRESS", "planned": "PLANNED", "done": "DONE", "backlog": "BACKLOG"}
    lines = ["His projects for you, as he keeps them:"]
    for status in ("in_progress", "planned", "done", "backlog"):
        items = groups.get(status)
        if not items:
            continue
        lines.append(f"{labels[status]}:")
        for r in items:
            line = f"  - {r['title']} (last moved {_local(r['updated_at'])})"
            if r.get("notes"):
                line += f": {r['notes'][:160]}"
            lines.append(line)
    return "\n".join(lines)


async def _my_scores() -> str:
    """Her numbers, as she may read them (roadmap item 5: "expose scores
    to her"). Latest run per suite with the previous one for the trend,
    and the weakest categories of the latest — enough to answer "what am
    I bad at" from measurement rather than self-report."""
    runs = await fetch_eval_runs(limit=60)
    if not runs:
        return ("No test scores are recorded yet. He measures you with "
                "tests/harness.py; each run is stored once it has run.")
    by_suite = {}
    for r in runs:
        # a gate run measures a PROPOSED version of her in its worktree,
        # not her; those rows stay in the Controller's Scores tab but are
        # not "her" latest score (2026-09-21: her author read a gate's
        # 83/84 as her own and argued from it)
        if (r.get("note") or "").startswith("gate proposal"):
            continue
        by_suite.setdefault(r["suite"], []).append(r)

    def _local(ts):
        # rows are stamped in UTC by sqlite; she thinks in local time
        try:
            from datetime import timezone
            dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return str(ts or "")[:16]

    lines = []
    for suite, hist in by_suite.items():
        latest = hist[0]
        when = _local(latest.get("created_at"))
        line = (f"{suite}: {latest['passed']}/{latest['total']} "
                f"({when}, on {latest.get('model') or '?'}")
        if latest.get("trials", 1) and latest["trials"] > 1:
            line += f", {latest['trials']} trials per case"
        line += ")"
        if len(hist) > 1:
            prev = hist[1]
            line += (f"; before that {prev['passed']}/{prev['total']} "
                     f"({_local(prev.get('created_at'))[:10]}, {prev.get('model') or '?'})")
        cats = latest.get("by_category") or {}
        weak = []
        for cat, v in cats.items():
            try:
                hit, n = int(v[0]), int(v[1])
            except (TypeError, ValueError, IndexError):
                continue
            if n and hit < n:
                weak.append((hit / n, f"{cat} {hit}/{n}"))
        if weak:
            weak.sort()
            line += ". Weakest: " + ", ".join(w for _, w in weak[:3])
        if latest.get("note"):
            line += f". Note: {latest['note']}"
        lines.append(line)
    return "Your measured scores, latest per suite:\n" + "\n".join(lines)


async def _propose_change(user_id: str, target: str, why: str) -> str:
    """Her one write in the self-modification loop, and it is a request: a
    'requested' row in `proposals`. The Controller (controller/versions.py,
    protected) is what turns it into a branch, a staging copy, a gate, and
    his decision. She cannot skip any of that from here."""
    try:
        from core.self_author import WHITELIST
    except Exception as e:
        return f"propose_change is unavailable: {e}"
    target = (target or "").strip()
    if target not in WHITELIST:
        return ("That is not a target you may propose on. The whitelist: "
                + ", ".join(sorted(WHITELIST)) + ".")
    why = (why or "").strip()
    if len(why) < 15:
        return "Say why: what you noticed and what you expect to improve."
    open_rows = [p for p in await fetch_proposals(limit=20)
                 if p.get("status") in ("requested", "proposed", "gated") and p.get("target") == target]
    if open_rows:
        return (f"A proposal on {target} is already waiting for him (#{open_rows[0]['id']}). "
                "One at a time.")
    pid = await create_proposal(f"(her request) {target}", why, "alex", target=target, status="requested")
    await record_decision(
        "proposal", f"She asked to change {target}", reasoning=why,
        evidence=f"proposals#{pid}", outcome="waiting for him at the Controller; nothing changed",
        actor="alex", ref=f"proposals#{pid}")
    return (f"Request #{pid} recorded for {target}. He will see it in his Controller; a separate "
            "copy of you will be built and tested before he decides. Nothing has changed yet.")


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------
async def run_tool(name: str, args, user_id: str) -> str:
    """Runs one tool call from the model and returns what she gets back.

    Never raises: a failure is a string she can read and report. Every
    call is logged and recorded, and the result is clipped so a tool
    cannot flood her context."""
    name = (name or "").strip()
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else {}
        except ValueError:
            args = {}
    args = args or {}

    if name not in TOOL_NAMES:
        return f"There is no tool called {name!r}."

    if name in CREATOR_ONLY:
        try:
            role = await get_user_role(user_id)
        except Exception:
            role = None
        if role != "creator":
            return ("That is for my creator to see, not for anyone who asks. "
                    "I can tell you what modules I have or run a diagnostic.")

    t0 = time.time()
    try:
        if name == "search_memory":
            coro = _search_memory(user_id, str(args.get("query", "")))
        elif name == "recent_turns":
            coro = _recent_turns(user_id, args.get("count", 10))
        elif name == "my_state":
            coro = _my_state()
        elif name == "list_modules":
            coro = _list_modules()
        elif name == "run_module":
            coro = _run_module(user_id, str(args.get("name", "")), str(args.get("command", "")))
        elif name == "run_diagnostics":
            coro = _run_diagnostics(user_id)
        elif name == "read_log":
            coro = asyncio.to_thread(_read_log, args.get("lines", 30))
        elif name == "read_my_source":
            coro = asyncio.to_thread(_read_my_source, str(args.get("path", "")),
                                     args.get("start_line", 1))
        elif name == "my_scores":
            coro = _my_scores()
        elif name == "my_projects":
            coro = _my_projects()
        elif name == "propose_change":
            coro = _propose_change(user_id, str(args.get("target", "")), str(args.get("why", "")))
        else:
            coro = asyncio.to_thread(_current_time)
        result = await asyncio.wait_for(coro, timeout=TOOL_TIMEOUT_S)
    except asyncio.TimeoutError:
        result = f"{name} took longer than {TOOL_TIMEOUT_S:.0f}s and was stopped."
    except Exception as e:
        result = f"{name} failed: {e}"

    result = _clip(str(result))
    elapsed = time.time() - t0
    logger.info(f"[TOOL] {name}({json.dumps(args, ensure_ascii=False)[:120]}) "
                f"for {user_id} in {elapsed:.2f}s -> {result[:100]!r}")
    try:
        await record_decision(
            "tool", f"Used {name}",
            reasoning="Her call, mid-turn — she chose to look before answering.",
            evidence=json.dumps(args, ensure_ascii=False)[:300],
            outcome=result[:300], actor=user_id)
    except Exception:
        pass
    return result
