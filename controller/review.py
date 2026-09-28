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
import re
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

# ---------------------------------------------------------------------------
# THE NOTE THAT NEEDS NOTHING INSTALLED (2026-09-28)
# ---------------------------------------------------------------------------
# Craig: "clicked the button, it failed and wrote a proposal. I'd like it just
# work instead." Fair. A button that answers "I have written a file somewhere"
# is not a button that works.
#
# Most of what a review of one of these actually says is mechanical, and the
# facts are all in her database: how many times she has proposed this same
# setting, what Craig decided last time and why, whether this value retreads
# ground he has already accepted or is a reworded version of something he has
# already refused, whether her own measurement supports a change at all,
# whether the suites have been run against it, and whether the diff is the one
# line it should be. None of that needs a model. It is computed here, it is
# instant, it is offline, and it is the same reasoning that has produced every
# rejection in this table so far.
#
# What a model adds on top is judgement about her RATIONALE — whether the
# argument holds, what she has misread. That part still wants Claude, and it is
# appended when the CLI exists. The note below is never empty.
_MEASURE_NO_EVIDENCE = ("no reply has ever been tied", "the evidence to decide",
                        "can never read zero", "no value of either setting")


def _overlap(a: str, b: str) -> float:
    """Share of the shorter text's content words that the other also has. For
    spotting a reworded repeat of something he already refused."""
    def words(t):
        return {w for w in re.findall(r"[a-z']{4,}", (t or "").lower())}
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def deterministic_note(p: dict) -> str:
    """Everything that can be said about this proposal from her own record,
    with no model involved. Facts first, then one summary line."""
    import asyncio

    from db.db import fetch_proposals
    facts, flags = [], []
    target = p.get("target")
    value = str(p.get("value") or "").strip()

    try:
        rows = asyncio.run(fetch_proposals(limit=400))
    except Exception as e:
        return f"Could not read her proposal history ({e}), so there is nothing to compare this against."

    # ---- how often this setting has come round, and what he did ----------
    if target:
        same = [r for r in rows if r.get("target") == target and r["id"] != p["id"]]
        declined = [r for r in same if r.get("status") == "declined"]
        rejected = [r for r in same if r.get("status") == "rejected"]
        merged = [r for r in same if r.get("status") == "merged"]
        facts.append(f"She has looked at {target} {len(same) + 1} times. "
                     f"{len(declined)} concluded no change, you rejected {len(rejected)}, "
                     f"you accepted {len(merged)}.")
        if rejected:
            last = rejected[0]
            why = (last.get("reason") or "").strip()
            facts.append(f"Your last refusal on it was #{last['id']} ({str(last.get('updated_at'))[:16]})"
                         + (f': "{why[:240]}"' if why else "."))
        # exactly this value, already refused
        folded = " ".join(value.split()).casefold()
        for r in rejected:
            if " ".join(str(r.get("value") or "").split()).casefold() == folded and folded:
                flags.append(f"This is the same value you rejected as #{r['id']}.")
                break
        else:
            # a reworded version of something refused
            for r in rejected:
                ov = _overlap(value, str(r.get("value") or ""))
                if ov >= 0.75 and len(value.split()) > 8:
                    flags.append(f"This is a reworded version of what you rejected as #{r['id']} "
                                 f"({ov * 100:.0f}% of the same words).")
                    break
        # a value inside ground already accepted
        try:
            from core.self_author import _oscillates, current_value
            accepted = [str(r.get("value") or "") for r in reversed(merged) if r.get("value")]
            problem = _oscillates(target, current_value(target), value, accepted)
            if problem:
                flags.append(problem[0].upper() + problem[1:] + ".")
        except Exception:
            pass

    # ---- does her own measurement support a change at all ---------------
    if target:
        try:
            from core.self_author import measure
            m = asyncio.run(measure(target))
            facts.append("The measurement she was shown: " + " ".join(m.split())[:420])
            low = m.lower()
            if any(k in low for k in _MEASURE_NO_EVIDENCE):
                flags.append("Her own measurement says the evidence to decide this does not exist. "
                             "On her own rule the right proposal was no change.")
            for zero in ("0 had two resources", "0 had three or more", "0 looked something up"):
                if zero in low:
                    flags.append("The measurement reports zero cases a change would affect.")
                    break
            m84 = re.search(r"latest intent suite (\d+)/(\d+)", low)
            if m84 and m84.group(1) == m84.group(2):
                flags.append(f"The intent suite is {m84.group(1)}/{m84.group(2)} on the current line: "
                             "there is no failing case for this clause to fix.")
        except Exception as e:
            facts.append(f"(the measurement could not be recomputed: {e})")

    # ---- has it been tested ---------------------------------------------
    if p.get("gate"):
        try:
            g = json.loads(p["gate"]) if isinstance(p["gate"], str) else p["gate"]
            bad = [f"{k} {v.get('passed')}/{v.get('total')}" for k, v in (g or {}).items()
                   if v.get("passed") != v.get("total")]
            facts.append("Gate: " + (", ".join(f"{k} {v.get('passed')}/{v.get('total')}" for k, v in (g or {}).items())
                                     or "empty"))
            if bad:
                flags.append("The gate has failing suites: " + ", ".join(bad) + ".")
        except Exception:
            facts.append(f"Gate: {str(p['gate'])[:200]}")
    else:
        flags.append("Not gated: her suites have not been run against this version yet.")

    # ---- is the change the one line it should be ------------------------
    tree = p.get("worktree")
    if tree and os.path.isdir(tree):
        stat = _run(["git", "diff", "--stat", "HEAD~1"], cwd=tree).strip()
        if stat:
            files = [ln for ln in stat.splitlines() if "|" in ln]
            facts.append(f"The diff touches {len(files)} file(s): " + "; ".join(f.strip()[:70] for f in files[:4]))
            if len(files) > 1:
                flags.append(f"The diff touches {len(files)} files. A setting change should be one line in one file "
                             "— read it before approving.")
    elif p.get("status") in ("proposed", "gated"):
        flags.append("No worktree on disk, so there is no diff to read.")

    # ---- one summary line ------------------------------------------------
    if flags:
        verdict = ("Nothing here is new information for you. " if len(flags) > 1
                   else "There is a reason to be careful here. ")
    else:
        verdict = ("Nothing mechanical argues against this one; what is left is whether her reasoning holds, "
                   "which is a judgement call. ")

    out = [verdict.strip(), ""]
    if flags:
        out.append("What stands out:")
        out += [f"  - {f}" for f in flags]
        out.append("")
    out.append("The record:")
    out += [f"  - {f}" for f in facts]
    out.append("")
    out.append("Written from her database by the Controller, with no model involved. A reading of her "
               "reasoning needs Claude and is appended when it answers.")
    return "\n".join(out)
