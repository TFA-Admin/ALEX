# tools/propose.py
"""
Claude's (and Craig's) entry point to the self-modification container.

    python -X utf8 tools/propose.py new "<title>" "<rationale>" --file path=content.txt [--file ...]
    python -X utf8 tools/propose.py new "<title>" "<rationale>" --patch change.diff
        A proposal from a Claude session: a branch in its own worktree
        under D:/project_ALEX/versions, ready for Craig to launch, gate
        and decide in the Controller (Inbox → Versions). Protected paths
        are refused here, and again at approve.

    python -X utf8 tools/propose.py ask <target> "<why>"
        Ask HER author for a proposal on a whitelisted target
        (python -m core.self_author --list shows them) and create it.

    python -X utf8 tools/propose.py list
    python -X utf8 tools/propose.py gate <id>       (staging must be running for judged suites)
    python -X utf8 tools/propose.py reject <id> "<reason>"

Launching, talking to and approving a version stay in the Controller —
approve restarts her, and the kill path is hers.
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from controller import versions  # noqa: E402


def _files(pairs):
    files = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--file expects path=content_file, got {pair!r}")
        rel, src = pair.split("=", 1)
        with open(src, encoding="utf-8") as fh:
            files[rel.replace("\\", "/")] = fh.read()
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new")
    new.add_argument("title")
    new.add_argument("rationale")
    new.add_argument("--file", action="append", help="repo-relative path=local file with the new content")
    new.add_argument("--patch", help="a unified diff to apply")
    new.add_argument("--author", default="claude")

    ask = sub.add_parser("ask")
    ask.add_argument("target")
    ask.add_argument("why", nargs="?", default="")

    sub.add_parser("list")
    g = sub.add_parser("gate")
    g.add_argument("id", type=int)
    r = sub.add_parser("reject")
    r.add_argument("id", type=int)
    r.add_argument("reason")

    args = ap.parse_args()

    if args.cmd == "new":
        pid = versions.create(args.title, args.rationale, args.author,
                              files=_files(args.file), patch_path=args.patch)
        p = versions.load(pid)
        print(f"#{pid} {p['status']}" + (f": {p['reason']}" if p.get("reason") else ""))
    elif args.cmd == "ask":
        import asyncio
        from db.db import create_proposal
        pid = asyncio.run(create_proposal(f"(her author) {args.target}", args.why, "alex",
                                          target=args.target, status="requested"))
        versions.author_from_request(versions.load(pid))
        p = versions.load(pid)
        print(f"#{pid} {p['status']}: {p['title']}" + (f" — {p['reason']}" if p.get("reason") else ""))
    elif args.cmd == "list":
        for p in versions.list_proposals():
            print(f"#{p['id']:<4} {p['status']:<10} {p['author']:<7} {p['title'][:70]}"
                  + (f"  [{p['branch']}]" if p.get("branch") else ""))
    elif args.cmd == "gate":
        p = versions.load(args.id)
        if not p:
            raise SystemExit("no such proposal")
        print(versions.run_gate(p))
    elif args.cmd == "reject":
        p = versions.load(args.id)
        if not p:
            raise SystemExit("no such proposal")
        versions.reject(p, args.reason)


if __name__ == "__main__":
    main()
