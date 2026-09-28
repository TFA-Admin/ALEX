# controller/review.py
"""
Ask Claude to look at one of her proposals, from the Controller.

2026-09-28, Craig: "is there a way so that when she does a proposal you are
pinged automatically so you can check her work and provide me with your notes
on her proposed change rather than having to go through here each time?" Then,
on being offered a scheduled headless run: "I don't want a headless session
running. We can connect you to her controller and when I see a proposal just
hit an 'ask claude' button and have it hit you with the proposal that way?"

So: a button, not a daemon. Nothing runs until he presses it, and what it
starts exits when it is done.

TWO PATHS, because the machine decides which is available:

  the CLI      If Claude Code is installed (`claude` on PATH, or a path saved
               in controller_settings.json as "claude_cli"), the button runs
               it once in print mode against the packet below and stores what
               comes back. One process, one job, gone.

  the queue    If it is not installed, the packet is written to
               config/review_requests/ and the button says so. Nothing is
               lost: the next session that is open picks the queue up and the
               review lands in the same place. That is strictly better than
               him pasting a proposal in by hand, and it needs nothing
               installed.

WHAT IS IN THE PACKET. A reviewer arriving cold needs what she was given and
what she did with it, or the review is just a second opinion on a title:

  * the proposal: target, current value, proposed value, her rationale, the
    effect she said she expected
  * the measurement she was shown when she wrote it (core/self_author.measure)
  * what Craig has decided before on that same setting, with his reasons
  * the gate results, if it has been gated
  * the real diff from her worktree, so the change is read rather than trusted
  * a pointer to the handoff in SELF_MODIFICATION_ARCHITECTURE.md

WHAT THE REVIEW MAY NOT DO. It never approves and never rejects — there is no
code path here that writes a status. It writes notes onto the row for him to
read while he decides. Approving stays a thing he does on purpose, which is
the whole point of the container she proposes into.
"""
import json
import os
import shutil
import subprocess
import time

from controller.common import ALEX_DIR, load_controller_settings

REQUEST_DIR = os.path.join(ALEX_DIR, "config", "review_requests")
CLI_TIMEOUT_S = 600.0


def find_cli() -> str:
    """The Claude Code executable, or "" if it is not installed. A path saved
    in controller_settings.json wins, so a non-standard install needs no code
    change."""
    saved = (load_controller_settings().get("claude_cli") or "").strip()
    if saved and os.path.isfile(saved):
        return saved
    for name in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _run(cmd: list, cwd: str = None) -> str:
    try:
        out = subprocess.run(cmd, cwd=cwd or ALEX_DIR, capture_output=True,
                             text=True, timeout=30, encoding="utf-8", errors="replace")
        return (out.stdout or "") + (("\n" + out.stderr) if out.returncode else "")
    except Exception as e:
        return f"(could not run {' '.join(cmd[:3])}: {e})"


def build_packet(p: dict) -> str:
    """Everything a cold reviewer needs about one proposal, as plain text."""
    import asyncio

    lines = [f"# Proposal #{p['id']}: {p.get('title')}",
             "",
             f"target      : {p.get('target') or '(none — this is one of her worded proposals)'}",
             f"status      : {p.get('status')}",
             f"author      : {p.get('author')}",
             f"proposed at : {p.get('created_at')}",
             f"value asked : {p.get('value')}",
             "",
             "## Her rationale, verbatim",
             (p.get("rationale") or "(none)").strip(),
             ""]

    target = p.get("target")
    if target:
        try:
            from core.self_author import WHITELIST, current_value, measure, decisions_on
            t = WHITELIST.get(target)
            if t:
                lines += [f"## The setting", f"file: {t.file}   locator: {t.locator}   bounds: {t.lo}-{t.hi}",
                          f"what it is: {t.about}", f"current value on disk: {current_value(target)}", ""]
            lines += ["## The measurement she was shown when she wrote this",
                      asyncio.run(measure(target)), ""]
            history, _rejected, merged = asyncio.run(decisions_on(target, days=120))
            lines += ["## What Craig has decided before on this same setting", history,
                      f"values he has accepted, oldest first: {merged or 'none'}", ""]
        except Exception as e:
            lines += [f"(could not gather the setting's context: {e})", ""]

    if p.get("gate"):
        lines += ["## Gate results (her test suites run against the proposed version)",
                  str(p["gate"]), ""]
    else:
        lines += ["## Gate results", "not gated yet — the suites have not been run against this version.", ""]

    tree = p.get("worktree")
    if tree and os.path.isdir(tree):
        lines += ["## The actual change, as a diff from her worktree",
                  _run(["git", "diff", "HEAD~1", "--unified=3"], cwd=tree).strip() or "(no diff)", ""]
    else:
        lines += ["## The actual change", "no worktree exists yet, so there is no diff to read. "
                  "Judge the value and the reasoning only, and say that is what you did.", ""]

    lines += ["## What to write",
              "A short note for Craig, who decides. Say whether the reasoning holds against the "
              "measurement above and against what he has already decided; name what evidence is "
              "missing; say plainly if you cannot tell from what is here. Do not approve and do not "
              "reject — that is his. Be brief: he is reading this next to the proposal.",
              "",
              "## Background, if you need it",
              "SELF_MODIFICATION_ARCHITECTURE.md, the section 'HANDOFF' under '## Work in progress', "
              "is the current state of this project. ANOMALIES.md is every odd behaviour and its cause.",
              ]
    return "\n".join(lines)


def queue(p: dict) -> str:
    """Write the packet where a session can find it. Returns the path."""
    os.makedirs(REQUEST_DIR, exist_ok=True)
    path = os.path.join(REQUEST_DIR, f"proposal_{p['id']}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_packet(p))
    return path


def pending() -> list:
    """Queued review requests nobody has answered yet."""
    try:
        return sorted(os.path.join(REQUEST_DIR, f) for f in os.listdir(REQUEST_DIR) if f.endswith(".md"))
    except OSError:
        return []


def ask_cli(p: dict, cli: str) -> tuple:
    """One invocation, one review. Returns (review_text, error). Blocking —
    the caller runs it off the UI thread."""
    path = queue(p)          # the packet is a file either way, so a failed run leaves it for a session
    prompt = (f"Review one of A.L.E.X.'s proposed changes to herself and write Craig a short note. "
              f"The whole packet is in {path} — read it first. Write ONLY the note, as plain prose, "
              f"no preamble and no markdown headings. Do not change any files. Do not approve or "
              f"reject the proposal.")
    try:
        out = subprocess.run([cli, "-p", prompt], cwd=ALEX_DIR, capture_output=True, text=True,
                             timeout=CLI_TIMEOUT_S, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "", f"Claude did not answer within {CLI_TIMEOUT_S / 60:.0f} minutes; the packet is queued at {path}"
    except Exception as e:
        return "", f"could not run {cli}: {e}; the packet is queued at {path}"
    text = (out.stdout or "").strip()
    if out.returncode != 0 and not text:
        return "", f"{os.path.basename(cli)} exited {out.returncode}: {(out.stderr or '').strip()[:300]}"
    if not text:
        return "", f"{os.path.basename(cli)} returned nothing; the packet is queued at {path}"
    try:
        os.remove(path)      # answered, so it is not also waiting in the queue
    except OSError:
        pass
    return text, ""
