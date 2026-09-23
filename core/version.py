"""
What version of her is running — read from the repository's own metadata.

2026-09-23 (Craig: "When we start adjusting her avatar, can we also add a
A.L.E.X. Version number in the webui?"). There is no version file to keep
in step; the commit she was started from is the version. It is read
straight from .git (HEAD, the ref, the commit object or the reflog) rather
than by running git.exe: no console window, no dependency on git being on
PATH inside a service, and it works in a staging worktree (whose .git is a
file pointing at the main repository).

describe() -> {"commit": "b16bb6a", "date": "2026-09-23", "branch": "main",
               "label": "2026-09-23 · b16bb6a"}
Everything is best-effort: an unreadable repository gives label "dev".
"""
import os
import zlib
from datetime import datetime, timezone

ALEX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_cache = None


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _git_dirs(root):
    """(gitdir, commondir) for a normal checkout or a worktree."""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot, dot
    head = _read(dot)
    if head.startswith("gitdir:"):
        gitdir = head[len("gitdir:"):].strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.normpath(os.path.join(root, gitdir))
        common = _read(os.path.join(gitdir, "commondir"))
        if common:
            if not os.path.isabs(common):
                common = os.path.normpath(os.path.join(gitdir, common))
            return gitdir, common
        return gitdir, gitdir
    return None, None


def _resolve_ref(commondir, ref):
    loose = _read(os.path.join(commondir, ref))
    if loose:
        return loose
    for line in _read(os.path.join(commondir, "packed-refs")).splitlines():
        if line.endswith(" " + ref):
            return line.split(" ", 1)[0]
    return ""


def _commit_time(commondir, sha):
    """Committer time from the loose object; the reflog if it is packed."""
    obj = os.path.join(commondir, "objects", sha[:2], sha[2:])
    try:
        with open(obj, "rb") as f:
            raw = zlib.decompress(f.read())
        header, _, body = raw.partition(b"\0")
        for line in body.decode("utf-8", "replace").splitlines():
            if line.startswith("committer "):
                parts = line.split()
                return int(parts[-2]), parts[-1]
            if not line:
                break
    except (OSError, zlib.error, ValueError, IndexError):
        pass
    return None, None


def _reflog_time(gitdir, sha):
    for line in reversed(_read(os.path.join(gitdir, "logs", "HEAD")).splitlines()):
        if line.startswith("%s " % sha[:7]) or ("%s " % sha) in line or line.split(" ")[1:2] == [sha]:
            try:
                head, _, _ = line.partition("\t")
                parts = head.split()
                return int(parts[-2]), parts[-1]
            except (ValueError, IndexError):
                return None, None
    return None, None


def _fmt(ts, tz):
    if ts is None:
        return ""
    try:
        sign = -1 if tz.startswith("-") else 1
        offset = sign * (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60)
        return datetime.fromtimestamp(ts + offset, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def describe(root: str = ALEX_DIR) -> dict:
    global _cache
    if _cache is not None:
        return _cache
    info = {"commit": "", "date": "", "branch": "", "label": "dev"}
    gitdir, commondir = _git_dirs(root)
    if gitdir:
        head = _read(os.path.join(gitdir, "HEAD"))
        sha, branch = "", ""
        if head.startswith("ref:"):
            ref = head[len("ref:"):].strip()
            branch = ref.replace("refs/heads/", "", 1)
            sha = _resolve_ref(commondir, ref)
        else:
            sha = head
        if sha:
            ts, tz = _commit_time(commondir, sha)
            if ts is None:
                ts, tz = _reflog_time(gitdir, sha)
            info.update(commit=sha[:7], branch=branch, date=_fmt(ts, tz))
            info["label"] = " · ".join(x for x in (info["date"], info["commit"]) if x)
            if branch and branch != "main":
                info["label"] += " (%s)" % branch
    _cache = info
    return info


def label() -> str:
    return describe()["label"]
