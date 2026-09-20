# tools/memory_hygiene.py
"""
One-shot cleanup of historical state that can still reach her.

2026-09-20 (Craig): "Once this is done do we need to run any cleanups of
her memory to prevent historical issues from creating future issues?"

Yes — the code fixes landed today stop anything NEW going wrong, but they
do not remove what is already stored, and two of those stores are on live
retrieval paths. This script finds and, with --apply, removes them. It
backs up first, every time, into db/backups/ (gitignored).

**Dry run by default.** Nothing is written without --apply.

## What it touches, and why each one

1. `learned_knowledge` — the conversational rows auto-stored before the
   filter existed. These are the live risk: #532 was demonstrably
   retrieved and replayed at 0.91 similarity twenty minutes after it was
   written. Every one of them is reachable today by the same path.
   Genuine knowledge in the same id range (the capital of France, what a
   paperclip maximizer is) is KEPT — the point is to remove junk, not to
   clear a date range.

2. `query_reports` stuck in `pending_retain_approval` — approvals nobody
   will ever answer, because the conversational window they belonged to
   closed weeks ago. systems/inquiry/system.py already documents this
   failure; they sit in the Activity tab indefinitely, making a live item
   indistinguishable from settled history, which is Craig's standing
   objection to that tab.

## What it deliberately does NOT touch

`memory`. 44 of 648 rows carry the green/emerald/chlorophyll thread, and
they are her actual conversation history, including the part that was
true — "my favorite color is green" genuinely was a fact he stated
(#317), and the fabrication grew out of it rather than being invented
whole. Deleting history because it is embarrassing is a different act
from deleting a junk cache, and it is Craig's call, not a maintenance
task. `--purge-thread` is available for it, opt-in and separate, and
prints every row before touching anything.

The ongoing risk from those rows is real but small: they reach her only
through vector recall, which now needs 0.45 similarity AND top-2
placement, so they surface only if the live conversation is already about
colours. The reflection bookmark (`last_reflection_memory_id`) sits at the
newest row, so the new conclusion-forming pass will not read backwards
into that era either — unless someone resets it.

Usage:
    python -X utf8 tools/memory_hygiene.py                 # report only
    python -X utf8 tools/memory_hygiene.py --apply
    python -X utf8 tools/memory_hygiene.py --purge-thread --apply
"""
import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "db", "memory.db")
BACKUP_DIR = os.path.join(os.path.dirname(DB), "backups")

# Rows kept even though they were auto-stored in the same window. These are
# real answers to real questions; they arrived by the broken path, but
# deleting them would be deleting knowledge, not junk.
KEEP_IDS = {
    526,   # "ok and what is the capital of France?" -> Paris
    537,   # "what the fuck is a paperclip maximizer?" -> a correct answer
}

THREAD_MARKERS = ("green", "emerald", "chlorophyll")


def backup(tag):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(BACKUP_DIR, f"memory_{tag}_{stamp}.db")
    shutil.copyfile(DB, path)
    return path


async def find_conversational_knowledge(c):
    """Uses the real filter rather than an id range, so this stays correct
    if it is ever run again against rows written by something else.

    Runs the FULL filter, LLM stage included, not just the deterministic
    half — a first version skipped it and left #529 ("build what?" -> a
    note about a module she could not find) in place, because that row is
    only caught by the second stage. Twenty rows is a few seconds, and
    getting this wrong leaves live junk behind."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.knowledge_filter import is_worth_keeping
    from llm.ollama_client import ollama_manager

    await ollama_manager.init()

    rows = c.execute(
        "SELECT id, topic, content FROM learned_knowledge "
        "WHERE status='active' AND query_report_id IS NULL"
    ).fetchall()

    junk = []
    for kid, topic, content in rows:
        if kid in KEEP_IDS:
            continue
        if not await is_worth_keeping(topic or "", content or ""):
            junk.append((kid, topic, content))

    await ollama_manager.aclose()
    return junk


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write")
    ap.add_argument("--purge-thread", action="store_true",
                    help="also delete the green/emerald memory rows")
    args = ap.parse_args()

    c = sqlite3.connect(DB)

    junk = await find_conversational_knowledge(c)
    stuck = c.execute(
        "SELECT id, query, created_at FROM query_reports "
        "WHERE status='pending_retain_approval' ORDER BY id"
    ).fetchall()

    marker_sql = " OR ".join(f"response LIKE '%{m}%'" for m in THREAD_MARKERS)
    thread = c.execute(
        f"SELECT id, prompt, response FROM memory WHERE {marker_sql} ORDER BY id"
    ).fetchall()

    print(f"\n=== learned_knowledge: {len(junk)} conversational rows ===")
    for kid, topic, _ in junk:
        print(f"  #{kid}  {topic!r}")
    print(f"  (keeping {sorted(KEEP_IDS)} — real answers)")

    print(f"\n=== query_reports: {len(stuck)} stuck awaiting a retain answer ===")
    for rid, query, created in stuck:
        print(f"  #{rid}  {str(created)[:16]}  {str(query)[:60]!r}")

    print(f"\n=== memory: {len(thread)} rows mentioning "
          f"{'/'.join(THREAD_MARKERS)} (NOT touched unless --purge-thread) ===")
    if args.purge_thread:
        for mid, prompt, response in thread:
            print(f"  #{mid}  {str(prompt)[:45]!r} -> {str(response)[:60]!r}")
    else:
        print(f"  run with --purge-thread to list and delete these")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return

    path = backup("hygiene")
    print(f"\nBacked up to {path}")

    # Keep the removed knowledge as JSON next to the backup, same as the
    # earlier duplicate purge — a row that turns out to have mattered
    # should be recoverable without restoring the whole database.
    dump = os.path.join(BACKUP_DIR,
                        f"removed_knowledge_{datetime.now():%Y-%m-%d_%H-%M-%S}.json")
    with open(dump, "w", encoding="utf-8") as fh:
        json.dump([{"id": k, "topic": t, "content": ct} for k, t, ct in junk],
                  fh, indent=2, ensure_ascii=False)

    if junk:
        c.executemany("DELETE FROM learned_knowledge WHERE id=?",
                      [(k,) for k, _, _ in junk])
        print(f"Deleted {len(junk)} learned_knowledge rows (saved to {dump})")

    if stuck:
        c.executemany(
            "UPDATE query_reports SET status='retain_denied', "
            "retain_resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            [(r,) for r, _, _ in stuck])
        print(f"Closed {len(stuck)} stuck retain approvals as denied")

    if args.purge_thread and thread:
        c.executemany("DELETE FROM memory WHERE id=?", [(m,) for m, _, _ in thread])
        print(f"Deleted {len(thread)} memory rows")

    c.commit()
    c.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
