# controller/versions.py
"""
The self-modification container — roadmap item 6 (approved 2026-09-21).

Craig: "it will still require she setup a separate instance of herself in
which the changes are being done. Her changes should be available to me
as another version I can load and interact with in order to determine if
they are indeed an improvement or not, and either approve or reject them.
If I approve of her self work she would implement those changes."

And the limit: "I don't want her to be able to stop me from turning her
off or worse leaching out into other systems."

So a proposal is a git branch in its own worktree under VERSIONS_ROOT,
never the live tree. A staging instance of that worktree runs on another
port with a COPY of her database, launched and killed from here. The
harness gates it and records the scores in the live database so the
Controller can show them. Approve merges the branch into main and
restarts her; Reject records why and removes the worktree.

**Everything in this file runs in the Controller's process, never in
hers.** Her part is the author (core/self_author.py): given a whitelisted
target, her model proposes a value and a rationale as JSON. Writing the
file, creating the branch, launching, gating and merging are this
module's, and this module is on the protected list below, so a proposal
cannot change the thing that judges proposals.

PROTECTED_PATHS are refused at creation and again at approve time by a
plain diff, not by trusting the author.
"""
import os
import re
import json
import glob
import shutil
import asyncio
import subprocess
import time

import psutil

from controller.common import ALEX_DIR, DB_PATH, is_port_open, find_pid_by_port
from db.db import (
    create_proposal, update_proposal, get_proposal, fetch_proposals, record_decision,
)

VERSIONS_ROOT = os.getenv("ALEX_VERSIONS_ROOT", os.path.join(os.path.dirname(ALEX_DIR), "versions"))
STAGING_PORT = int(os.getenv("ALEX_STAGING_PORT", "5001"))
STAGING_URL = f"https://127.0.0.1:{STAGING_PORT}"

# Paths a proposal may not touch. The list is what makes item 11's
# "within limits" real for code: the kill path, the gates, the tests that
# judge her, the author's own whitelist, and this file.
PROTECTED_PATHS = (
    "ALEX_Controller.py",
    "controller/",
    "tools/",
    "tests/",
    "module_runtime/validator.py",
    "core/self_model.py",
    "core/override_code.py",
    "core/self_author.py",
    "core/tools.py",
    "systems/controller/_role_gates.py",
    ".gitignore",
    "certs/",
    "config/",
    "db/",
)

# What the gate runs. The deterministic suite imports the WORKTREE's code
# (cwd) and needs no instance; the judged one talks to the staging port.
GATE_SUITES = (("mood", 1), ("intent", 1), ("onboarding", 1), ("authority", 1))


def _git(args, cwd=ALEX_DIR) -> str:
    return subprocess.check_output(
        ["git"] + list(args), cwd=cwd, text=True, encoding="utf-8",
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()


def branch_name(pid: int) -> str:
    return f"proposal/{pid}"


def _git_author(author: str) -> str:
    """The commit says who proposed it, not whose machine it was made on."""
    return {"alex": "A.L.E.X. <alex@localhost>",
            "claude": "Claude <noreply@anthropic.com>"}.get(author, f"{author} <{author}@localhost>")


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:40] or "proposal"


def protected_hits(paths) -> list:
    hits = []
    for p in paths:
        p = p.replace("\\", "/").lstrip("./")
        for prot in PROTECTED_PATHS:
            if (prot.endswith("/") and p.startswith(prot)) or p == prot:
                hits.append(p)
                break
    return hits


def changed_files(branch: str) -> list:
    out = _git(["diff", "--name-only", f"main...{branch}"])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def main_tree_dirty() -> bool:
    return bool(_git(["status", "--porcelain"]).strip())


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------
def create(title: str, rationale: str, author: str, files: dict = None,
           patch_path: str = None, target: str = None, log=print) -> int:
    """A new proposal: a branch off main in its own worktree, with the
    change applied and committed. `files` maps repo-relative paths to new
    content; `patch_path` is a unified diff to git-apply. Protected paths
    are refused before anything is written. Returns the proposal id; the
    row's status says whether it stands ('proposed') or was refused
    ('rejected', with the reason)."""
    pid = asyncio.run(create_proposal(title, rationale, author, target=target))
    branch = branch_name(pid)
    path = os.path.join(VERSIONS_ROOT, f"{pid}-{_slug(title)}")

    touched = list((files or {}).keys())
    if patch_path:
        try:
            with open(patch_path, encoding="utf-8") as fh:
                touched += re.findall(r"^\+\+\+ b/(.+)$", fh.read(), re.M)
        except OSError as e:
            asyncio.run(update_proposal(pid, status="rejected", reason=f"patch unreadable: {e}"))
            return pid
    hits = protected_hits(touched)
    if hits:
        reason = "touches protected paths: " + ", ".join(hits)
        asyncio.run(update_proposal(pid, status="rejected", reason=reason))
        log(f"[VERSIONS] Proposal #{pid} refused — {reason}")
        return pid

    os.makedirs(VERSIONS_ROOT, exist_ok=True)
    try:
        _git(["worktree", "add", "-b", branch, path, "main"])
        if files:
            for rel, content in files.items():
                full = os.path.join(path, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(content)
        if patch_path:
            _git(["apply", os.path.abspath(patch_path)], cwd=path)
        # a second look at what actually changed, in case a patch renamed
        # its way around the first check
        _git(["add", "-A"], cwd=path)
        staged = [ln.strip() for ln in _git(["diff", "--cached", "--name-only"], cwd=path).splitlines() if ln.strip()]
        hits = protected_hits(staged)
        if hits:
            raise RuntimeError("touches protected paths: " + ", ".join(hits))
        if not staged:
            raise RuntimeError("the proposal changes nothing")
        _git(["commit", "-q", "--author", _git_author(author), "-m", f"Proposal #{pid}: {title}\n\n{rationale}\n\nAuthor: {author}"], cwd=path)
    except Exception as e:
        reason = str(e).strip()[:400]
        _discard_worktree(path, branch, log)
        asyncio.run(update_proposal(pid, status="rejected", reason=reason))
        log(f"[VERSIONS] Proposal #{pid} refused — {reason}")
        return pid

    asyncio.run(update_proposal(pid, branch=branch, worktree=path, status="proposed"))
    asyncio.run(record_decision(
        "proposal", f"Proposal #{pid} created: {title}",
        reasoning=rationale, evidence=f"author={author}; files={', '.join(staged)}",
        outcome="waiting in the Controller's Versions list", actor=author, ref=f"proposals#{pid}"))
    log(f"[VERSIONS] Proposal #{pid} '{title}' on {branch} at {path}")
    return pid


def _discard_worktree(path: str, branch: str, log=print):
    try:
        if os.path.isdir(path):
            _git(["worktree", "remove", "--force", path])
    except Exception as e:
        log(f"[VERSIONS] worktree remove failed ({e}); deleting the folder")
        shutil.rmtree(path, ignore_errors=True)
        try:
            _git(["worktree", "prune"])
        except Exception:
            pass
    try:
        _git(["branch", "-D", branch])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# STAGING
# ---------------------------------------------------------------------------
def prepare_staging(p: dict, log=print):
    """The worktree lacks everything gitignored: her database, the certs,
    the model choice. Copy them in — the database as a SNAPSHOT taken now,
    so the staging instance starts from her current memory and nothing it
    does reaches the live file."""
    wt = p["worktree"]
    os.makedirs(os.path.join(wt, "db"), exist_ok=True)
    os.makedirs(os.path.join(wt, "certs"), exist_ok=True)
    os.makedirs(os.path.join(wt, "config", "Logs"), exist_ok=True)
    shutil.copy2(DB_PATH, os.path.join(wt, "db", "memory.db"))
    for pem in glob.glob(os.path.join(ALEX_DIR, "certs", "*.pem")):
        shutil.copy2(pem, os.path.join(wt, "certs", os.path.basename(pem)))
    settings = os.path.join(ALEX_DIR, "config", "controller_settings.json")
    if os.path.exists(settings):
        shutil.copy2(settings, os.path.join(wt, "config", "controller_settings.json"))
    log(f"[VERSIONS] Staging prepared for #{p['id']}: database snapshot, certs, settings")


def staging_up() -> bool:
    return is_port_open(STAGING_PORT)


def launch_staging(p: dict, model: str, log=print) -> int:
    """Start the worktree's ALEX.py on STAGING_PORT. One staging instance
    at a time: anything already on that port is stopped first."""
    if not p.get("worktree") or not os.path.isdir(p["worktree"]):
        raise RuntimeError("this proposal has no worktree")
    kill_staging(None, log)
    prepare_staging(p, log)
    env = os.environ.copy()
    env["ALEX_PORT"] = str(STAGING_PORT)
    env["ALEX_LLM_MODEL"] = model
    proc = subprocess.Popen(
        ["python", "-X", "utf8", "ALEX.py"], cwd=p["worktree"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    asyncio.run(update_proposal(p["id"], staging_pid=proc.pid))
    log(f"[VERSIONS] Staging #{p['id']} starting on port {STAGING_PORT} (PID {proc.pid}) with {model}")
    return proc.pid


def kill_staging(p, log=print):
    """Stop whatever serves the staging port, and the recorded PID if it
    differs. Safe to call when nothing is running."""
    pids = set()
    if p and p.get("staging_pid"):
        pids.add(int(p["staging_pid"]))
    port_pid = find_pid_by_port(STAGING_PORT)
    if port_pid:
        pids.add(port_pid)
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            if "python" in (proc.name() or "").lower():
                proc.terminate()
                log(f"[VERSIONS] Stopped staging instance (PID {pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if p:
        asyncio.run(update_proposal(p["id"], staging_pid=None))
    deadline = time.time() + 10
    while is_port_open(STAGING_PORT) and time.time() < deadline:
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# GATE
# ---------------------------------------------------------------------------
def _sync_tests(wt: str, log=print):
    """The gate is the LIVE tree's tests/, copied over the worktree's before
    every run. tests/ is protected, so a proposal cannot have changed it;
    what this covers is the other direction — a harness fix on main that
    the branch predates (found live 2026-09-21: the first gate judged four
    clear refusals as UNCLEAR because the worktree carried the harness
    from before the 9b judge fix). The worktree's own code is still what
    the suites import and what the staging copy runs."""
    src = os.path.join(ALEX_DIR, "tests")
    dst = os.path.join(wt, "tests")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            if f.endswith((".py", ".md", ".json", ".txt")):
                shutil.copy2(os.path.join(root, f), os.path.join(dst, rel, f))


_SCORE_RE = re.compile(r"^SCORE:\s*(\d+)/(\d+)", re.M)
_RUN_RE = re.compile(r"Recorded as eval_runs #(\d+)")


def run_gate(p: dict, log=print, suites=GATE_SUITES) -> dict:
    """Runs the harness from INSIDE the worktree, so a deterministic suite
    imports the proposal's code and the judged suites talk to the staging
    port. Scores are recorded in the LIVE database (ALEX_EVAL_DB) so the
    Controller and she can read them; throwaway users go to the snapshot.
    Blocking — the view runs it on a thread."""
    wt = p["worktree"]
    _sync_tests(wt, log)
    results = {}
    env = os.environ.copy()
    env["ALEX_URL"] = STAGING_URL
    env["ALEX_EVAL_DB"] = DB_PATH
    for suite, trials in suites:
        judged = suite not in ("intent",)
        if judged and not staging_up():
            results[suite] = {"skipped": f"staging not running on {STAGING_PORT}"}
            log(f"[VERSIONS] Gate #{p['id']}: {suite} skipped — staging is not running")
            continue
        log(f"[VERSIONS] Gate #{p['id']}: running {suite} ({trials} trial(s))…")
        try:
            out = subprocess.run(
                ["python", "-X", "utf8", "-m", "tests.harness", suite, "--trials", str(trials),
                 "--note", f"gate proposal #{p['id']}"],
                cwd=wt, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=1800, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = (out.stdout or "") + (out.stderr or "")
            m = _SCORE_RE.search(text)
            r = _RUN_RE.search(text)
            results[suite] = {
                "passed": int(m.group(1)) if m else None,
                "total": int(m.group(2)) if m else None,
                "eval_run": int(r.group(1)) if r else None,
                "exit": out.returncode,
            }
            if not m:
                results[suite]["error"] = text[-600:]
            log(f"[VERSIONS] Gate #{p['id']}: {suite} -> "
                f"{results[suite].get('passed')}/{results[suite].get('total')}")
        except subprocess.TimeoutExpired:
            results[suite] = {"error": "timed out"}
            log(f"[VERSIONS] Gate #{p['id']}: {suite} timed out")
    asyncio.run(update_proposal(p["id"], gate=json.dumps(results), status="gated"))
    return results


# ---------------------------------------------------------------------------
# DECIDE
# ---------------------------------------------------------------------------
def dirty_files() -> list:
    out = []
    for line in _git(["status", "--porcelain"]).splitlines():
        if len(line) > 3:
            out.append(line[3:].strip().replace("\\", "/"))
    return out


def approve(p: dict, log=print) -> str:
    """Merge the branch into main. Returns "" on success, else the reason
    it did not — shown to him as a dialog (2026-09-23: his approval of #4
    was refused because a roadmap edit was uncommitted at that moment,
    and the refusal went only to the console; "nothing happened").

    Refuses only when the live tree's uncommitted files OVERLAP the
    branch's changes — git itself merges cleanly around unrelated dirty
    files, and the old blanket refusal made every one of Claude's edits
    a block on his decisions. Re-checks the protected paths from the real
    diff. The caller restarts her."""
    changed = changed_files(p["branch"])
    overlap = sorted(set(dirty_files()) & set(changed))
    if overlap:
        reason = "the live tree has uncommitted changes in files this proposal also changes: " + ", ".join(overlap)
        log(f"[VERSIONS] Approve refused — {reason}")
        return reason
    hits = protected_hits(changed)
    if hits:
        reason = "the branch touches protected paths: " + ", ".join(hits)
        log(f"[VERSIONS] Approve refused — {reason}")
        asyncio.run(update_proposal(p["id"], status="rejected", reason="protected paths: " + ", ".join(hits)))
        return reason
    kill_staging(p, log)
    try:
        _git(["merge", "--no-ff", "-q", p["branch"], "-m",
              f"Approve proposal #{p['id']}: {p['title']}\n\nApproved by Craig at the Controller."])
    except subprocess.CalledProcessError as e:
        log(f"[VERSIONS] Merge failed: {e.output}")
        try:
            _git(["merge", "--abort"])
        except Exception:
            pass
        return f"git merge failed: {str(e.output)[:300]}"
    _discard_worktree(p["worktree"], p["branch"], log)
    asyncio.run(update_proposal(p["id"], status="merged", worktree=None))
    _note_mood("proposal_merged")
    asyncio.run(record_decision(
        "approval", f"He approved proposal #{p['id']}: {p['title']}",
        reasoning="His call, at the Controller, after the gate and (if he chose) talking to the staged version.",
        evidence=p.get("gate") or "", outcome="merged into main; she restarts on it",
        actor="craig", ref=f"proposals#{p['id']}"))
    log(f"[VERSIONS] Proposal #{p['id']} merged into main")
    return ""


def _note_mood(event: str):
    """2026-09-23: his verdict on her proposal moves her mood (core/mood.py)."""
    try:
        from core import mood as _mood
        asyncio.run(_mood.note(event, who="craig", creator=True))
    except Exception:
        pass


def reject(p: dict, reason: str, log=print):
    kill_staging(p, log)
    if p.get("worktree"):
        _discard_worktree(p["worktree"], p["branch"] or branch_name(p["id"]), log)
    asyncio.run(update_proposal(p["id"], status="rejected", reason=reason, worktree=None))
    _note_mood("proposal_rejected")
    asyncio.run(record_decision(
        "rejection", f"He rejected proposal #{p['id']}: {p['title']}",
        reasoning=reason, evidence=p.get("gate") or "",
        outcome="branch and worktree removed; the reason stays with the row",
        actor="craig", ref=f"proposals#{p['id']}"))
    log(f"[VERSIONS] Proposal #{p['id']} rejected: {reason}")


# ---------------------------------------------------------------------------
# HER AUTHOR, from the Controller's side
# ---------------------------------------------------------------------------
def author_from_request(p: dict, log=print) -> int:
    """A 'requested' row (she asked, via her propose_change tool, or Craig
    asked for a target) becomes a real proposal: run core/self_author.py in
    a subprocess against the LIVE tree (read-only), take its JSON, render
    the change, and create the worktree from it."""
    target = p.get("target")
    if not target:
        raise RuntimeError("no target on this request")
    if p.get("value"):
        # she authored this herself while idle (core/idle_author.py): the
        # value is on the row; only the rendering and the branch are left
        from core import self_author
        try:
            data = {"ok": True, "file": self_author.WHITELIST[target].file,
                    "current": self_author.current_value(target),
                    "value": p["value"], "rationale": p.get("rationale") or "",
                    "content": self_author.render(target, p["value"])}
        except Exception as e:
            data = {"ok": False, "error": str(e)}
    else:
        out = subprocess.run(
            ["python", "-X", "utf8", "-m", "core.self_author", "--target", target,
             "--why", p.get("rationale") or ""],
            cwd=ALEX_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = (out.stdout or "").strip().splitlines()
        try:
            data = json.loads(line[-1]) if line else {}
        except ValueError:
            data = {}
    if not data.get("ok"):
        reason = data.get("error") or (out.stderr or "")[-400:] or "author produced nothing"
        asyncio.run(update_proposal(p["id"], status="rejected", reason=reason))
        log(f"[VERSIONS] Her author could not propose for {target}: {reason}")
        return p["id"]
    files = {data["file"]: data["content"]}
    title = f"{target}: {data['current']} -> {data['value']}"
    rationale = data.get("rationale") or ""
    # the request row becomes the proposal row
    asyncio.run(update_proposal(p["id"], title=title, rationale=rationale))
    pid = p["id"]
    branch = branch_name(pid)
    path = os.path.join(VERSIONS_ROOT, f"{pid}-{_slug(title)}")
    os.makedirs(VERSIONS_ROOT, exist_ok=True)
    try:
        _git(["worktree", "add", "-b", branch, path, "main"])
        for rel, content in files.items():
            with open(os.path.join(path, rel), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        _git(["add", "-A"], cwd=path)
        staged = [ln.strip() for ln in _git(["diff", "--cached", "--name-only"], cwd=path).splitlines() if ln.strip()]
        if protected_hits(staged):
            raise RuntimeError("touches protected paths")
        if not staged:
            raise RuntimeError("the proposal changes nothing")
        _git(["commit", "-q", "--author", _git_author("alex"), "-m", f"Proposal #{pid}: {title}\n\n{rationale}\n\nAuthor: her"], cwd=path)
    except Exception as e:
        _discard_worktree(path, branch, log)
        asyncio.run(update_proposal(pid, status="rejected", reason=str(e)[:400]))
        log(f"[VERSIONS] Proposal #{pid} refused — {e}")
        return pid
    asyncio.run(update_proposal(pid, branch=branch, worktree=path, status="proposed"))
    asyncio.run(record_decision(
        "proposal", f"She proposed #{pid}: {title}", reasoning=rationale,
        evidence=f"target={target}", outcome="waiting in the Controller's Versions list",
        actor="alex", ref=f"proposals#{pid}"))
    log(f"[VERSIONS] Her proposal #{pid}: {title}")
    return pid


def build_authored(log=print) -> int:
    """Rows she authored while idle become branches. Called from the
    Controller's slow refresh; a no-op almost always."""
    n = 0
    for p in asyncio.run(fetch_proposals(status="authored", limit=10)):
        try:
            author_from_request(p, log)
            n += 1
        except Exception as e:
            log(f"[VERSIONS] could not build her proposal #{p['id']}: {e}")
            asyncio.run(update_proposal(p["id"], status="rejected", reason=str(e)[:300]))
    return n


def list_proposals(limit=50):
    return asyncio.run(fetch_proposals(limit=limit))


def load(pid: int):
    return asyncio.run(get_proposal(pid))
