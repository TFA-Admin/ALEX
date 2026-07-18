import aiosqlite
import os
import pickle
import json
import re

DB_PATH = os.path.join(os.path.dirname(__file__), "memory.db")


# -------------------------
# INIT DB
# -------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute('''
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            prompt TEXT,
            response TEXT,
            category TEXT DEFAULT 'conversation',
            embedding BLOB,
            weight INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        await db.execute('''
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            key TEXT,
            value TEXT,
            importance INTEGER DEFAULT 5,
            expires_at TEXT,
            UNIQUE(user, key)
        )''')

        await db.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified INTEGER DEFAULT 1
        )''')

        # 🧠 SYSTEM LEARNING
        await db.execute('''
        CREATE TABLE IF NOT EXISTS system_learning (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')

        # 🧠 MODEL LEARNING
        await db.execute('''
        CREATE TABLE IF NOT EXISTS model_usage (
            user TEXT,
            model TEXT,
            count INTEGER DEFAULT 1,
            PRIMARY KEY (user, model)
        )''')

        # 🔐 SECURITY EVENTS (sandbox rejections, surfaced to creator)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            event_type TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            acknowledged INTEGER DEFAULT 0
        )''')

        # 🎙️ VOICE PROFILES (speaker verification embeddings)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS voice_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            embedding BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # 🎭 PERSONALITY CHANGE LOG (self-reflection visibility, not a gate)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS personality_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT,
            new_value TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            acknowledged INTEGER DEFAULT 0
        )''')

        # 🤔 CURIOSITY QUEUE (2026-07-16) — self-initiated curiosity
        # trigger, Component 11: core/self_reflection.py's hourly pass
        # queues a real, nameable knowledge gap it noticed on its own,
        # delivered as part of the same creator-verified-connect briefing
        # personality_log already uses, not mid-conversation.
        await db.execute('''
        CREATE TABLE IF NOT EXISTS curiosity_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            question TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            delivered INTEGER DEFAULT 0
        )''')

        # 🧩 MODULE STATE (per-user state blob for generated modules)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS module_state (
            user TEXT,
            module TEXT,
            state TEXT,
            PRIMARY KEY (user, module)
        )''')

        # 🧩 MODULE BUILD REQUESTS (creator approval queue — she never
        # builds anything a non-creator user asked for without this being
        # approved first; a creator-initiated request still gets a row for
        # the audit trail, just auto-approved)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS module_build_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_by TEXT,
            module_name TEXT,
            prompt TEXT,
            status TEXT DEFAULT 'pending',
            result TEXT,
            requested_access TEXT,
            access_approved INTEGER DEFAULT 0,
            origin TEXT DEFAULT 'live_conversation',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP
        )''')

        # 🧩 MODULE REGISTRY (Phase 1: the module interface/contract's
        # bookkeeping — what's installed, what's enabled, current version,
        # where it came from. Enable/disable is enforced by
        # systems/modules/system.py checking this at invocation time, not
        # by unloading code from module_loader's cache — same pattern the
        # systems/* tier already uses for its own disabled_systems set.)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS module_registry (
            name TEXT PRIMARY KEY,
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'enabled',
            language TEXT DEFAULT 'python',
            source TEXT,
            access_scope TEXT,
            requested_by TEXT,
            build_request_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # 🧩 MODULE VERSIONS (full code snapshot per version — what
        # rollback restores from)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS module_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT,
            version INTEGER,
            code TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # 🔎 QUERY REPORTS (2026-07-16) — the gated-research state machine
        # (Component 4/5). A real, going-online search costs two separate
        # creator approvals: one to actually search, a second to retain
        # what was found. States: pending_search_approval ->
        # search_approved | search_denied -> (search runs, findings
        # attached) -> pending_retain_approval -> retained | retain_denied.
        # `findings`/`sources` hold what was found so the creator can see
        # it BEFORE deciding whether it's worth keeping — nothing gets
        # written to learned_knowledge until 'retained'.
        await db.execute('''
        CREATE TABLE IF NOT EXISTS query_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_by TEXT,
            query TEXT,
            reason TEXT,
            status TEXT DEFAULT 'pending_search_approval',
            findings TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            search_resolved_at TIMESTAMP,
            retain_resolved_at TIMESTAMP
        )''')

        # 🧠 LEARNED KNOWLEDGE (2026-07-16) — real, citable, searched
        # knowledge, distinct from `facts` (user-specific, not her own
        # world-knowledge) and `memory` (raw conversation history, not
        # distilled). Only ever written once a query_report reaches
        # 'retained'. `supersedes` links a correction to what it replaces
        # so revision actually propagates (a superseded row stays in the
        # table for history, just excluded from retrieval) instead of two
        # contradictory active entries sitting side by side.
        await db.execute('''
        CREATE TABLE IF NOT EXISTS learned_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            content TEXT,
            source_url TEXT,
            query_report_id INTEGER,
            embedding BLOB,
            status TEXT DEFAULT 'active',
            supersedes INTEGER,
            user TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        await db.commit()
        await ensure_memory_vector_merge(db)
        await ensure_user_column_naming(db)
        await ensure_access_tier_columns(db)

    print(f"✅ Initialized database at {DB_PATH}")


# -------------------------
# MIGRATION FIX
# -------------------------
async def ensure_memory_vector_merge(db):
    """One-time migration (2026-07-16): `memory` and `vector_memory` held
    the exact same conversation data in two parallel tables — every real
    turn was written to both (plain in one, embedded in the other), pure
    duplication rather than a deliberate design split (unlike
    learned_knowledge, which really is a different kind of thing —
    beliefs with provenance/revision, not a conversation log). Verified
    before merging, not assumed: every (user, prompt, response) tuple in
    `memory` had at least as many matching rows in `vector_memory`, so
    `vector_memory` is a confirmed superset, safe to use as the source of
    truth. `category` was never anything but its own default in any real
    row, so nothing meaningful is lost using the default for the merged
    rows. Idempotent — checks whether `vector_memory` still exists before
    doing anything, so this is a no-op after the first run (or on a
    fresh DB that never had the split in the first place)."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vector_memory'"
    ) as cursor:
        exists = await cursor.fetchone()

    if not exists:
        return

    async with db.execute("PRAGMA table_info(memory)") as cursor:
        cols = [row[1] for row in await cursor.fetchall()]

    if "embedding" not in cols:
        await db.execute("ALTER TABLE memory ADD COLUMN embedding BLOB")
    if "weight" not in cols:
        await db.execute("ALTER TABLE memory ADD COLUMN weight INTEGER DEFAULT 1")

    await db.execute("DELETE FROM memory")
    await db.execute("""
        INSERT INTO memory (user, prompt, response, category, embedding, weight, created_at)
        SELECT user, prompt, response, 'conversation', embedding, weight, created_at
        FROM vector_memory
    """)
    await db.execute("DROP TABLE vector_memory")
    await db.commit()

    print("✅ Merged vector_memory into memory (schema consolidation)")


async def ensure_user_column_naming(db):
    """One-time migration: facts.owner, voice_profiles.owner, and
    module_state.user_id all meant the same thing every other table's
    'user' column already means — standardizing on 'user' everywhere
    (module_build_requests.requested_by / module_registry.requested_by
    stay as-is, a genuinely distinct concept: who requested the build,
    not just row ownership). The CREATE TABLE...IF NOT EXISTS statements
    above only define the new column name for a brand-new database file
    — this handles the live one, which already has data under the old
    names. Idempotent, same pattern as ensure_weight_column above."""
    renames = [
        ("facts", "owner", "user"),
        ("voice_profiles", "owner", "user"),
        ("module_state", "user_id", "user"),
    ]

    for table, old, new in renames:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]

        if old in cols and new not in cols:
            await db.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")

    await db.commit()


async def ensure_access_tier_columns(db):
    """Supports the privilege-tier system (2026-07-16): a build request
    can be flagged with what elevated access it actually needs
    (`requested_access` — set by Claude when it reviews the request, not
    a classifier guess) and whether the creator has explicitly signed off
    on that specific grant (`access_approved`) — a real second decision,
    separate from "yes, build this." `module_registry.access_scope`
    records what was ultimately granted, for audit/visibility (e.g. in
    `list modules`). Idempotent, same pattern as the migrations above."""
    additions = [
        ("module_build_requests", "requested_access", "TEXT"),
        ("module_build_requests", "access_approved", "INTEGER DEFAULT 0"),
        ("module_registry", "access_scope", "TEXT"),
        # 2026-07-16: Craig — "it's hard to tell what's her or you making
        # requests" — requested_by is always the creator either way (their
        # own "yes" IS the approval, same person regardless of origin), so
        # it never actually distinguished a request she proposed live in
        # conversation from one Claude created directly while working with
        # him. Explicit origin closes that.
        ("module_build_requests", "origin", "TEXT DEFAULT 'live_conversation'"),
        # 2026-07-16: found live — a casual LLM-fallback reply included
        # Craig's real override code (fact_context had no exclusion for
        # LOCKED_KEYS, fixed separately in systems/facts/system.py), and
        # that response got auto-cached in learned_knowledge with no
        # owner — meaning it would have replayed verbatim to ANY user
        # whose message embedded similarly, not just Craig. NULL = a
        # genuinely universal entry (real web search findings — those
        # never see fact_context at all); a real user value scopes
        # retrieval to that person only. Root cause fixed upstream too;
        # this is the second, structural layer — closes the class of
        # leak even if something unexpected slips into a cached response
        # again in the future.
        ("learned_knowledge", "user", "TEXT"),
        # 2026-07-18 (Craig, after a web-search finding got retained
        # saying race results "aren't posted yet": "what happens when
        # that information changes... isn't that an issue?") — yes: this
        # table had no concept of a retained belief going stale, so a
        # snapshot-in-time answer (today's live-web-search result) would
        # have replayed verbatim forever, indistinguishable from a
        # timeless fact. NULL = no expiration (the existing behavior,
        # unchanged for ordinary conversational auto-caching); a real
        # value excludes it from matching once past — see
        # fetch_active_knowledge()'s filter and retain_report()'s real
        # expiration for search-derived entries specifically.
        ("learned_knowledge", "expires_at", "TEXT"),
    ]

    for table, col, col_type in additions:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]

        if col not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")

    await db.commit()


# -------------------------
# MEMORY
# -------------------------
async def add_memory(user, prompt, response, category="conversation", embedding=None):
    """embedding is optional — a plain-text-only caller (e.g. the legacy
    api/routes.py path) still works with no change, and a caller with a
    real embedding (systems/memory/system.py's after_response()) writes
    both in one row instead of two separate tables (2026-07-16 merge —
    memory and vector_memory held identical data in two places)."""
    emb_blob = pickle.dumps(embedding) if embedding is not None else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO memory(user,prompt,response,category,embedding) VALUES(?,?,?,?,?)",
            (user, prompt, response, category, emb_blob)
        )
        await db.commit()


async def fetch_recent_memory(user, limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT prompt, response, created_at FROM memory WHERE user=? ORDER BY id DESC LIMIT ?",
            (user, limit)
        )
        rows = await cursor.fetchall()

    return [{"prompt": r[0], "response": r[1], "created_at": r[2]} for r in reversed(rows)]


# -------------------------
# FACTS
# -------------------------
async def update_fact(user, key, value, importance=5, expires_at=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO facts(user, key, value, importance, expires_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user, key)
            DO UPDATE SET
                value=excluded.value,
                importance=excluded.importance,
                expires_at=excluded.expires_at
            """,
            (user, key, value, importance, expires_at)
        )
        await db.commit()


# 2026-07-18 (Craig: adding a casual fact never required a code, so
# removing one shouldn't either) — mirrors update_fact() above; only ever
# called for systems/facts/system.py's ALLOWED_FACT_KEYS (favorite_color/
# alias/job), the explicitly casual/low-stakes tier — real identity
# fields (role/user_name/edit_code/override_code) never go through this
# system at all, so this can't touch them.
async def delete_fact(user, key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM facts WHERE user=? AND key=?", (user, key))
        await db.commit()


async def fetch_user_facts(user):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT key,value FROM facts WHERE user=?",
            (user,)
        )
        rows = await cursor.fetchall()

    return {r[0]: r[1] for r in rows}


# -------------------------
# USER MIGRATION (FIXED)
# -------------------------
async def migrate_user(old_user, new_user):
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            "UPDATE memory SET user=? WHERE user=?",
            (new_user, old_user)
        )

        await db.execute(
            "UPDATE facts SET user=? WHERE user=?",
            (new_user, old_user)
        )

        await db.execute(
            "UPDATE voice_profiles SET user=? WHERE user=?",
            (new_user, old_user)
        )

        await db.commit()

    print(f"🔄 Migrated {old_user} → {new_user}")


DEFAULT_PERSONALITY = (
    "You default to being clear and helpful, but you're not required to be "
    "neutral or flat about it — dry wit, mild sarcasm, or bluntness are all "
    "fine when they fit naturally. You don't force jokes."
)


async def get_personality():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM system_learning WHERE key='personality_description'"
        )
        row = await cursor.fetchone()

    return row[0] if row else DEFAULT_PERSONALITY


async def set_personality(description: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO system_learning(key, value)
            VALUES('personality_description', ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (description,))
        await db.commit()


async def get_personality_hard_rules() -> list:
    """2026-07-16: found live — merge_personality_change() re-summarizes
    the WHOLE personality_description from scratch on every new creator
    instruction, and confirmed via the actual personality_log that this
    silently DROPPED a real instruction ("without using emojis") the very
    next time a different instruction was merged in, even though the
    merge prompt asks it to preserve everything. An LLM-based paraphrase
    is never guaranteed to preserve every detail across repeated passes —
    same lesson as "reset" phrasing staying a deterministic list instead
    of trusting classifier judgment. Hard rules are stored here VERBATIM,
    never re-summarized or touched by any LLM call, and rendered into the
    system prompt as their own always-included section — belt-and-
    suspenders on top of (not instead of) the flowing personality
    description, which still exists for general "vibe" that's fine to
    evolve loosely."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM system_learning WHERE key='personality_hard_rules'"
        )
        row = await cursor.fetchone()

    if not row:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


async def add_personality_hard_rule(rule: str):
    """Appends verbatim, skipping an exact (case-insensitive) duplicate —
    repeating the same instruction shouldn't pile up copies of it."""
    rules = await get_personality_hard_rules()

    normalized = rule.strip().lower()
    if any(r.strip().lower() == normalized for r in rules):
        return

    rules.append(rule.strip())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO system_learning(key, value)
            VALUES('personality_hard_rules', ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (json.dumps(rules),))
        await db.commit()


async def clear_personality_hard_rules():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM system_learning WHERE key='personality_hard_rules'")
        await db.commit()


RESPONSE_TIMING_HISTORY_LIMIT = 30


async def record_response_timing(duration: float):
    """2026-07-16: Craig asked for her to actually notice when her own
    response time goes bad, after a real live incident (orphaned Ollama
    runner processes silently exhausted GPU VRAM, and every turn just
    hung with no error) — same rolling-JSON-list-in-system_learning shape
    as personality_hard_rules above, capped so this never grows
    unbounded. Called once per turn from core/response_handler.py right
    after its own [TIMING] TOTAL turn log line."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM system_learning WHERE key='response_timings'"
        )
        row = await cursor.fetchone()

        try:
            timings = json.loads(row[0]) if row else []
        except (json.JSONDecodeError, TypeError):
            timings = []

        timings.append(duration)
        timings = timings[-RESPONSE_TIMING_HISTORY_LIMIT:]

        await db.execute("""
            INSERT INTO system_learning(key, value)
            VALUES('response_timings', ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (json.dumps(timings),))
        await db.commit()


async def get_response_timings() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM system_learning WHERE key='response_timings'"
        )
        row = await cursor.fetchone()

    if not row:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


async def get_learned_phrase(key: str, default: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM system_learning WHERE key=?",
            (f"phrase:{key}",)
        )
        row = await cursor.fetchone()

    return row[0] if row else default


async def set_learned_phrase(key: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO system_learning(key, value)
            VALUES(?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (f"phrase:{key}", text))
        await db.commit()


async def reset_all_phrases():
    """Clears every stored phrase override — get_learned_phrase() then
    falls back to each hardcoded default again."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM system_learning WHERE key LIKE 'phrase:%'")
        await db.commit()


# -------------------------
# PERSONALITY CHANGE LOG (visibility, not a gate — she doesn't need
# creator approval to change, but the creator can always see what changed)
# -------------------------
async def log_personality_change(new_value: str, reason: str, kind: str = "personality"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO personality_log(kind, new_value, reason) VALUES(?,?,?)",
            (kind, new_value, reason)
        )
        await db.commit()


async def get_last_personality_log_value() -> str:
    """2026-07-17: for diagnostic_tool's data-consistency check — the
    last logged 'personality' kind entry SHOULD match what get_personality()
    currently returns; if it doesn't, a write silently failed partway
    (logged but never actually stored, or vice versa). Returns None if
    there's no personality-kind entry yet."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT new_value FROM personality_log WHERE kind='personality' "
            "ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()

    return row[0] if row else None


async def fetch_unacknowledged_personality_changes():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT kind, new_value, reason, created_at FROM personality_log "
            "WHERE acknowledged=0 ORDER BY id ASC"
        )
        rows = await cursor.fetchall()

    return [
        {"kind": r[0], "new_value": r[1], "reason": r[2], "created_at": r[3]}
        for r in rows
    ]


async def acknowledge_personality_changes():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE personality_log SET acknowledged=1 WHERE acknowledged=0")
        await db.commit()


async def queue_curiosity_question(topic: str, question: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO curiosity_queue(topic, question) VALUES(?,?)",
            (topic, question)
        )
        await db.commit()


async def fetch_undelivered_curiosity_questions():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT topic, question, created_at FROM curiosity_queue "
            "WHERE delivered=0 ORDER BY id ASC"
        )
        rows = await cursor.fetchall()

    return [
        {"topic": r[0], "question": r[1], "created_at": r[2]}
        for r in rows
    ]


async def mark_curiosity_questions_delivered():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE curiosity_queue SET delivered=1 WHERE delivered=0")
        await db.commit()


async def fetch_recent_memory_all(limit=20, since_id=0):
    """Recent conversation turns across all users — used by the
    self-reflection loop. `since_id` (2026-07-17, added after Craig found
    personality drifting every 15-30 min with no real interaction behind
    it): only rows with id > since_id are returned, so a caller can avoid
    re-reflecting on the exact same stale conversation window on every
    pass. Each returned dict now includes 'id' so the caller can bookmark
    the newest one it actually saw. Default since_id=0 preserves the
    original "just the most recent N, unconditionally" behavior for any
    other caller."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, user, prompt, response FROM memory WHERE id > ? ORDER BY id DESC LIMIT ?",
            (since_id, limit)
        )
        rows = await cursor.fetchall()

    return [{"id": r[0], "user": r[1], "prompt": r[2], "response": r[3]} for r in reversed(rows)]


async def get_seconds_since_last_activity():
    """2026-07-17 (Craig: "have her know if there's room for her to make
    changes, like a downtime"): real conversational activity, not a
    guess — the most recent row in `memory` (written every turn by
    systems/memory/system.py's after_response hook). Returns None if
    there's no conversation history at all yet (fresh install), which
    callers should treat as "never idle enough to tell" rather than
    "infinitely idle"."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT (julianday('now') - julianday(created_at)) * 86400.0 "
            "FROM memory ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()

    return row[0] if row else None


async def get_seconds_since_last_personality_change():
    """2026-07-17 (Craig: "there has been [activity] — I modified her
    personality via the controller"): a real gap in
    get_seconds_since_last_activity() above — it only ever looked at the
    `memory` table (real conversation turns), which a Controller-driven
    or chat-based personality override never touches at all, so that kind
    of real engagement was completely invisible to the idle gate. Same
    query shape, against personality_log's created_at instead. Returns
    None if there's no personality_log entry yet."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT (julianday('now') - julianday(created_at)) * 86400.0 "
            "FROM personality_log ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()

    return row[0] if row else None


async def get_last_reflection_memory_id() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM system_learning WHERE key='last_reflection_memory_id'"
        )
        row = await cursor.fetchone()

    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


async def set_last_reflection_memory_id(memory_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO system_learning(key, value)
            VALUES('last_reflection_memory_id', ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (str(memory_id),))
        await db.commit()


# -------------------------
# MODEL LEARNING
# -------------------------
async def log_model_usage(user, model):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO model_usage (user, model, count)
            VALUES (?, ?, 1)
            ON CONFLICT(user, model)
            DO UPDATE SET count = count + 1
        """, (user, model))
        await db.commit()


async def get_preferred_model(user):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT model FROM model_usage
            WHERE user=?
            ORDER BY count DESC LIMIT 1
        """, (user,))
        row = await cursor.fetchone()

    return row[0] if row else None


# -------------------------
# VECTOR MEMORY (2026-07-16: reads from the merged `memory` table —
# vector_memory was dropped, see ensure_memory_vector_merge)
# -------------------------
async def fetch_vector_memories(user):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT prompt,response,embedding,weight,created_at FROM memory WHERE user=? AND embedding IS NOT NULL",
            (user,)
        )
        rows = await cursor.fetchall()

    results = []
    for r in rows:
        results.append({
            "prompt": r[0],
            "response": r[1],
            "embedding": pickle.loads(r[2]),
            "weight": r[3] or 1,
            "created_at": r[4]
        })

    return results


async def reinforce_response(user, prompt, response):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE memory
            SET weight = MIN(10, weight + 1)
            WHERE user=? AND prompt=? AND response=?
        """, (user, prompt, response))
        await db.commit()


# -------------------------
# MEMORY DECAY
# -------------------------
async def decay_memory():
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            UPDATE facts
            SET importance = MAX(1, importance - 1)
            WHERE importance > 1
        """)

        await db.execute("""
            UPDATE memory
            SET weight = MAX(1, weight - 1)
            WHERE weight > 1
        """)

        await db.commit()

    print("🧹 Memory decay applied")

# -------------------------
# ROLE HELPERS
# -------------------------
async def get_user_role(user: str):
    facts = await fetch_user_facts(user)
    return facts.get("role", "user")


async def get_creator_identity():
    """2026-07-17 (Craig: "I'd like to be able to use my override code at
    any point... and have her pull my creator account"): finds whichever
    profile actually holds role='creator' (there's exactly one) and
    returns (user_id, override_code) — the override code by itself is
    now sufficient proof of creator identity for require_creator(),
    independent of whatever identity the current session/voice-match
    resolved to. Returns (None, None) if no creator profile exists yet
    (fresh install) or that profile never set an override code."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user FROM facts WHERE key='role' AND value='creator' LIMIT 1"
        )
        row = await cursor.fetchone()

    if not row:
        return None, None

    creator_id = row[0]
    facts = await fetch_user_facts(creator_id)
    return creator_id, facts.get("override_code")

# -------------------------
# SECURITY EVENTS
# -------------------------
async def log_security_event(user, event_type, detail):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO security_events(user, event_type, detail) VALUES(?,?,?)",
            (user, event_type, detail)
        )
        await db.commit()


async def fetch_unacknowledged_security_events():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user, event_type, detail, created_at FROM security_events "
            "WHERE acknowledged=0 ORDER BY id ASC"
        )
        rows = await cursor.fetchall()

    return [
        {"user": r[0], "event_type": r[1], "detail": r[2], "created_at": r[3]}
        for r in rows
    ]


async def acknowledge_security_events():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE security_events SET acknowledged=1 WHERE acknowledged=0")
        await db.commit()

# -------------------------
# VOICE PROFILES
# -------------------------
async def add_voice_sample(user, embedding):
    async with aiosqlite.connect(DB_PATH) as db:
        emb_blob = pickle.dumps(embedding)
        await db.execute(
            "INSERT INTO voice_profiles(user, embedding) VALUES(?,?)",
            (user, emb_blob)
        )
        await db.commit()


MAX_VOICE_SAMPLES = 15


async def reinforce_voice_sample(user, embedding, max_samples=MAX_VOICE_SAMPLES):
    """
    Adds a confirmed-genuine voice sample (from a successful verification or
    recognition) and prunes to the most recent max_samples for that user —
    a rolling window so the profile keeps adapting instead of staying frozen
    at whatever the initial enrollment happened to sound like, without
    growing the table forever.
    """
    await add_voice_sample(user, embedding)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM voice_profiles
            WHERE user=? AND id NOT IN (
                SELECT id FROM voice_profiles WHERE user=? ORDER BY id DESC LIMIT ?
            )
            """,
            (user, user, max_samples)
        )
        await db.commit()


async def fetch_voice_samples(user):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT embedding FROM voice_profiles WHERE user=?",
            (user,)
        )
        rows = await cursor.fetchall()

    return [pickle.loads(r[0]) for r in rows]


async def fetch_all_voice_profiles():
    """Returns {user: [embedding, ...]} for every enrolled profile."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user, embedding FROM voice_profiles")
        rows = await cursor.fetchall()

    profiles = {}
    for user, emb_blob in rows:
        profiles.setdefault(user, []).append(pickle.loads(emb_blob))

    return profiles


async def find_profile_by_prefix(prefix: str):
    """
    Resolves a short/partial name ('craig') to a full profile ('craignorton')
    only when exactly one profile matches — ambiguous prefixes return None so
    callers fall back to full onboarding rather than guessing.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT username FROM profiles WHERE username LIKE ?",
            (prefix + "%",)
        )
        rows = await cursor.fetchall()

    matches = [r[0] for r in rows]
    return matches[0] if len(matches) == 1 else None

# -------------------------
# PROFILE FINDER
# -------------------------
async def profile_exists(username: str):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM profiles WHERE username=?",
                (username,)
            )
            row = await cursor.fetchone()

        return row is not None

async def create_profile(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO profiles(username) VALUES(?)",
            (username,)
        )
        await db.commit()

async def get_module_state(user, module_name):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT state FROM module_state WHERE user=? AND module=?",
            (user, module_name)
        )
        row = await cursor.fetchone()

    if row:
        return json.loads(row[0])
    return {}


async def set_module_state(user, module_name, state):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO module_state (user, module, state)
            VALUES (?, ?, ?)
        """, (user, module_name, json.dumps(state)))
        await db.commit()


# -------------------------
# MODULE BUILD REQUESTS (creator approval queue)
# -------------------------
async def create_module_build_request(requested_by, module_name, prompt, status="pending", origin="live_conversation"):
    """origin distinguishes a request she proposed live during a real
    conversation ('live_conversation', the default — e.g. systems/
    modules/system.py's gap-detection flow) from one Claude created
    directly while working with the creator in an active session
    ('claude_session' — e.g. a scope-expansion request for an already-
    built module). requested_by alone can't tell these apart, since the
    creator's own "yes" is the approval either way."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO module_build_requests (requested_by, module_name, prompt, status, origin)
            VALUES (?, ?, ?, ?, ?)
        """, (requested_by, module_name, prompt, status, origin))
        await db.commit()
        return cursor.lastrowid


async def fetch_approved_module_build_requests():
    """Approved but not yet built — the live server polls this to actually
    perform the build (the Controller can mark approval, but building
    needs the live process's in-memory module_runtime state)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, requested_by, module_name, prompt
            FROM module_build_requests WHERE status='approved'
            ORDER BY created_at
        """)
        rows = await cursor.fetchall()

    return [
        {"id": r[0], "requested_by": r[1], "module_name": r[2], "prompt": r[3]}
        for r in rows
    ]


async def fetch_recent_module_build_requests(limit=15):
    """Newest-first, any status — lets the Controller show a build that
    went straight from 'approved' to 'built' (the creator's own confirmed
    requests never sit in 'pending', so the Requests table alone would
    never show them completing). Includes requested_access/access_approved
    so the Controller can show elevated-access grants explicitly rather
    than a plain "built" that hides what was actually authorized."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, requested_by, module_name, status, result, created_at, resolved_at,
                   requested_access, access_approved, origin
            FROM module_build_requests
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()

    return [
        {
            "id": r[0], "requested_by": r[1], "module_name": r[2], "status": r[3],
            "result": r[4], "created_at": r[5], "resolved_at": r[6],
            "requested_access": r[7], "access_approved": r[8], "origin": r[9]
        }
        for r in rows
    ]


async def resolve_module_build_request(request_id, status, result=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE module_build_requests
            SET status=?, result=?, resolved_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (status, result, request_id))
        await db.commit()


# -------------------------
# PRIVILEGE TIERS (2026-07-16) — a build request can be flagged with what
# elevated access it actually needs, and that grant requires its own
# explicit creator approval, separate from "yes, build this." Claude (the
# one now authoring modules directly, see SELF_MODIFICATION_ARCHITECTURE.md)
# sets requested_access after reviewing the request — not a classifier
# guess, since it's the one writing the code and knows what it actually
# needs.
# -------------------------
async def set_requested_access(request_id, access_description):
    """Claude calls this after reviewing an approved build request, if
    (and only if) it determines real elevated access is needed. Leaves
    `status` untouched (still 'approved') — access_approved staying 0 is
    what actually blocks installation; this just records what's being
    asked for so the creator sees a real, specific description, not a
    generic access-level label."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE module_build_requests
            SET requested_access=?, access_approved=0
            WHERE id=?
        """, (access_description, request_id))
        await db.commit()


async def approve_elevated_access(request_id):
    """Creator-only (gated in systems/controller/system.py, same as any
    other privileged command) — the second, explicit approval for the
    specific access a module requested, distinct from the original
    build approval."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE module_build_requests
            SET access_approved=1
            WHERE id=?
        """, (request_id,))
        await db.commit()


async def fetch_requests_needing_access_approval():
    """Approved builds where Claude has flagged a real access need that
    the creator hasn't signed off on yet — what "approve elevated access
    for request N" and the Controller's visibility into this should be
    checking against."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, requested_by, module_name, prompt, requested_access, created_at
            FROM module_build_requests
            WHERE status='approved' AND requested_access IS NOT NULL AND access_approved=0
            ORDER BY created_at
        """)
        rows = await cursor.fetchall()

    return [
        {
            "id": r[0], "requested_by": r[1], "module_name": r[2], "prompt": r[3],
            "requested_access": r[4], "created_at": r[5]
        }
        for r in rows
    ]


# -------------------------
# MODULE REGISTRY (Phase 1)
# -------------------------
async def register_module_version(name, code, requested_by, source="generated",
                                    language="python", build_request_id=None,
                                    access_scope=None):
    """
    Registers a new version of a module — first install (version 1) or an
    update (version N+1). Always snapshots the code into module_versions
    so rollback has something real to restore. Returns the new version
    number. `access_scope` records what was actually granted (e.g.
    "os_process", "db_read", None for a plain sandboxed module) — audit
    trail for the privilege-tier system, not enforcement itself (the
    sandbox/check_safety() and the elevated-access approval gate are what
    actually enforce it; this is just what got recorded as granted).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT version FROM module_registry WHERE name=?", (name,))
        row = await cursor.fetchone()
        new_version = (row[0] + 1) if row else 1

        await db.execute("""
            INSERT INTO module_registry (name, version, status, language, source, access_scope, requested_by, build_request_id, updated_at)
            VALUES (?, ?, 'enabled', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                version=excluded.version,
                status='enabled',
                language=excluded.language,
                source=excluded.source,
                access_scope=excluded.access_scope,
                requested_by=excluded.requested_by,
                build_request_id=excluded.build_request_id,
                updated_at=CURRENT_TIMESTAMP
        """, (name, new_version, language, source, access_scope, requested_by, build_request_id))

        await db.execute("""
            INSERT INTO module_versions (module_name, version, code)
            VALUES (?, ?, ?)
        """, (name, new_version, code))

        await db.commit()

    return new_version


async def get_module_registry_entry(name):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT name, version, status, language, source, access_scope, requested_by, created_at, updated_at "
            "FROM module_registry WHERE name=?",
            (name,)
        )
        row = await cursor.fetchone()

    if not row:
        return None

    keys = ["name", "version", "status", "language", "source", "access_scope", "requested_by", "created_at", "updated_at"]
    return dict(zip(keys, row))


async def list_module_registry():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT name, version, status, language, source, access_scope, requested_by, created_at, updated_at "
            "FROM module_registry ORDER BY name"
        )
        rows = await cursor.fetchall()

    keys = ["name", "version", "status", "language", "source", "access_scope", "requested_by", "created_at", "updated_at"]
    return [dict(zip(keys, r)) for r in rows]


async def set_module_status(name, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE module_registry SET status=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
            (status, name)
        )
        await db.commit()


async def fetch_module_versions(name):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT version, created_at FROM module_versions WHERE module_name=? ORDER BY version DESC",
            (name,)
        )
        rows = await cursor.fetchall()

    return [{"version": r[0], "created_at": r[1]} for r in rows]


async def get_module_version_code(name, version):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT code FROM module_versions WHERE module_name=? AND version=?",
            (name, version)
        )
        row = await cursor.fetchone()

    return row[0] if row else None


# -------------------------
# DATABASE CAPABILITY (gated, creator-only — see
# systems/controller/system.py's "list/show/edit/delete database ..."
# commands). Allowlist-based, not blocklist-based: a generic "edit any
# table" capability would bypass every safeguard already built elsewhere
# (e.g. writing facts.value where key='role' would grant creator role
# outside the dedicated grant/revoke-with-override-code flow; editing
# voice_profiles could let a forged embedding pass voice verification).
# New tables added later are excluded from writes by default, not
# accidentally exposed.
# -------------------------
DB_READ_EXCLUDE = {"voice_profiles", "security_events"}
DB_WRITE_ALLOWLIST = {"module_state"}

# Mirrors ALEX_Controller.py's own DB_BLOB_COLUMNS — kept as a separate
# definition rather than a shared import since the Controller is a
# distinct process/UI with its own editing surface; duplicated on purpose
# to avoid coupling the two, not an oversight.
DB_BLOB_COLUMNS = {
    ("memory", "embedding"),
    ("voice_profiles", "embedding"),
    ("learned_knowledge", "embedding"),
}

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def list_db_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        rows = await cursor.fetchall()

    return [r[0] for r in rows if r[0] not in DB_READ_EXCLUDE]


async def get_db_table_rows(table, limit=50):
    if table in DB_READ_EXCLUDE or not _VALID_IDENTIFIER.match(table):
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        columns = [r[1] for r in await cursor.fetchall()]

        if not columns:
            return None

        blob_cols = {c for t, c in DB_BLOB_COLUMNS if t == table}

        cursor = await db.execute(
            f"SELECT rowid, {', '.join(columns)} FROM {table} LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()

    results = []
    for row in rows:
        record = {"rowid": row[0]}
        for i, col in enumerate(columns):
            value = row[i + 1]
            record[col] = f"<{len(value) if value else 0} bytes>" if col in blob_cols else value
        results.append(record)

    return results


async def update_db_row(table, rowid, column, value):
    if table not in DB_WRITE_ALLOWLIST:
        return False, f"'{table}' isn't editable through this capability."

    if not _VALID_IDENTIFIER.match(table) or not _VALID_IDENTIFIER.match(column):
        return False, "Invalid table or column name."

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        columns = [r[1] for r in await cursor.fetchall()]

        if column not in columns:
            return False, f"'{table}' has no column '{column}'."

        await db.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (value, rowid))
        await db.commit()

    return True, None


async def delete_db_row(table, rowid):
    if table not in DB_WRITE_ALLOWLIST:
        return False, f"'{table}' isn't editable through this capability."

    if not _VALID_IDENTIFIER.match(table):
        return False, "Invalid table name."

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
        await db.commit()


# -------------------------
# QUERY REPORTS (gated research pipeline, 2026-07-16)
# -------------------------

async def create_query_report(requested_by, query, reason):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO query_reports (requested_by, query, reason) VALUES (?, ?, ?)",
            (requested_by, query, reason)
        )
        await db.commit()
        return cursor.lastrowid


async def fetch_pending_search_approvals():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, requested_by, query, reason, created_at FROM query_reports "
            "WHERE status='pending_search_approval' ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
    keys = ["id", "requested_by", "query", "reason", "created_at"]
    return [dict(zip(keys, r)) for r in rows]


async def resolve_search_approval(report_id, approved: bool):
    status = "search_approved" if approved else "search_denied"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE query_reports SET status=?, search_resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, report_id)
        )
        await db.commit()


async def attach_search_findings(report_id, findings, sources):
    """Search ran — findings are ready for the creator to review before
    deciding whether to keep them. Nothing is written to
    learned_knowledge until the separate retain approval."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE query_reports SET status='pending_retain_approval', findings=?, sources=? WHERE id=?",
            (findings, sources, report_id)
        )
        await db.commit()


async def resolve_retain_approval(report_id, approved: bool):
    status = "retained" if approved else "retain_denied"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE query_reports SET status=?, retain_resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, report_id)
        )
        await db.commit()


async def get_query_report(report_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, requested_by, query, reason, status, findings, sources, created_at, "
            "search_resolved_at, retain_resolved_at FROM query_reports WHERE id=?",
            (report_id,)
        )
        row = await cursor.fetchone()
    if not row:
        return None
    keys = ["id", "requested_by", "query", "reason", "status", "findings", "sources",
            "created_at", "search_resolved_at", "retain_resolved_at"]
    return dict(zip(keys, row))


async def fetch_recent_query_reports(limit=15):
    """Newest-first, any status — Controller-facing visibility, mirrors
    fetch_recent_module_build_requests. Deliberately excludes
    findings/sources — those can be sizable, and every periodic refresh
    (every 5s) fetching them for every row would be wasteful when only a
    single selected row is ever actually inspected. See
    get_query_report_findings() for that on-demand lookup."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, requested_by, query, status, created_at, search_resolved_at, retain_resolved_at "
            "FROM query_reports ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
    keys = ["id", "requested_by", "query", "status", "created_at", "search_resolved_at", "retain_resolved_at"]
    return [dict(zip(keys, r)) for r in rows]


# -------------------------
# LEARNED KNOWLEDGE (the belief store, 2026-07-16)
# -------------------------

async def create_learned_knowledge(topic, content, source_url, query_report_id, embedding, supersedes=None, user=None, expires_at=None):
    """Only ever called once a query_report reaches 'retained', or when
    the LLM-fallback path auto-stores a fresh answer. If this supersedes
    an existing belief, that belief is marked superseded in the same
    pass so retrieval never surfaces both the old and new version at
    once — a real correction propagates instead of just piling up a
    second, contradictory entry.

    user=None means a genuinely universal entry (real web search
    findings — those never see fact_context, so there's nothing
    person-specific to leak). A real user value scopes retrieval to
    that person only — added 2026-07-16 after a casual LLM-fallback
    reply was found to have included a real security code (root cause
    fixed in systems/facts/system.py) and gotten cached with no owner,
    meaning it would have replayed to ANY user, not just the one it was
    generated for. Every LLM-fallback auto-store now passes a real
    user.

    expires_at (2026-07-18, ISO datetime string or None) — a snapshot-
    in-time answer (e.g. a web search result about something still in
    progress) shouldn't be treated as timelessly true; see
    fetch_active_knowledge()'s filter and systems/inquiry/system.py's
    retain_report() for where a real value actually gets set."""
    emb_blob = pickle.dumps(embedding)
    async with aiosqlite.connect(DB_PATH) as db:
        if supersedes is not None:
            await db.execute(
                "UPDATE learned_knowledge SET status='superseded', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (supersedes,)
            )
        cursor = await db.execute(
            "INSERT INTO learned_knowledge (topic, content, source_url, query_report_id, embedding, supersedes, user, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (topic, content, source_url, query_report_id, emb_blob, supersedes, user, expires_at)
        )
        await db.commit()
        return cursor.lastrowid


async def fetch_active_knowledge(user=None):
    """Every currently-active (not superseded/retracted, not expired)
    belief visible to this user, for retrieval — same shape as
    fetch_vector_memories, so the retrieval path can reuse the exact
    same cosine-similarity pattern already proven there.

    user=None (the default) returns only universal entries (user IS
    NULL) — safe for any caller that doesn't have a real user_id in
    hand. Passing a real user returns that user's own entries PLUS
    universal ones, but never another user's — the actual security
    boundary (see create_learned_knowledge's docstring for why this
    exists).

    expires_at filter (2026-07-18): a snapshot-in-time answer that's
    passed its expiration is excluded here rather than deleted — the row
    stays for audit/history, it just stops being offered as a live
    cached answer. A later question that no longer matches anything
    falls through to an honest "I don't have that stored" instead of
    confidently repeating stale information."""
    async with aiosqlite.connect(DB_PATH) as db:
        if user is None:
            cursor = await db.execute(
                "SELECT id, topic, content, source_url, embedding, created_at "
                "FROM learned_knowledge WHERE status='active' AND user IS NULL "
                "AND (expires_at IS NULL OR expires_at > datetime('now'))"
            )
        else:
            cursor = await db.execute(
                "SELECT id, topic, content, source_url, embedding, created_at "
                "FROM learned_knowledge WHERE status='active' AND (user IS NULL OR user=?) "
                "AND (expires_at IS NULL OR expires_at > datetime('now'))",
                (user,)
            )
        rows = await cursor.fetchall()
    results = []
    for r in rows:
        results.append({
            "id": r[0],
            "topic": r[1],
            "content": r[2],
            "source_url": r[3],
            "embedding": pickle.loads(r[4]),
            "created_at": r[5],
        })
    return results


async def find_related_knowledge(topic):
    """Substring topic match — used at retain-approval time to check
    whether a new finding is about the same thing as something already
    stored (for supersede detection). Not the retrieval path itself —
    that's fetch_active_knowledge's embeddings, real semantic search."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, topic, content FROM learned_knowledge WHERE status='active' AND topic LIKE ?",
            (f"%{topic}%",)
        )
        rows = await cursor.fetchall()
    return [{"id": r[0], "topic": r[1], "content": r[2]} for r in rows]

    return True, None