import aiosqlite
import os
import pickle
import json

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        await db.execute('''
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT,
            key TEXT,
            value TEXT,
            importance INTEGER DEFAULT 5,
            expires_at TEXT,
            UNIQUE(owner, key)
        )''')

        await db.execute('''
        CREATE TABLE IF NOT EXISTS vector_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            prompt TEXT,
            response TEXT,
            embedding BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            weight INTEGER DEFAULT 1
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
            owner TEXT,
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

        # 🧩 MODULE STATE (per-user state blob for generated modules)
        await db.execute('''
        CREATE TABLE IF NOT EXISTS module_state (
            user_id TEXT,
            module TEXT,
            state TEXT,
            PRIMARY KEY (user_id, module)
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

        await db.commit()
        await ensure_weight_column(db)

    print(f"✅ Initialized database at {DB_PATH}")


# -------------------------
# MIGRATION FIX
# -------------------------
async def ensure_weight_column(db):
    async with db.execute("PRAGMA table_info(vector_memory)") as cursor:
        cols = [row[1] for row in await cursor.fetchall()]

    if "weight" not in cols:
        await db.execute("ALTER TABLE vector_memory ADD COLUMN weight INTEGER DEFAULT 1")
        await db.commit()


# -------------------------
# MEMORY
# -------------------------
async def add_memory(user, prompt, response, category="conversation"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO memory(user,prompt,response,category) VALUES(?,?,?,?)",
            (user, prompt, response, category)
        )
        await db.commit()


async def fetch_recent_memory(user, limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT prompt, response FROM memory WHERE user=? ORDER BY id DESC LIMIT ?",
            (user, limit)
        )
        rows = await cursor.fetchall()

    return [{"prompt": r[0], "response": r[1]} for r in reversed(rows)]


# -------------------------
# FACTS
# -------------------------
async def update_fact(owner, key, value, importance=5, expires_at=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO facts(owner, key, value, importance, expires_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(owner, key)
            DO UPDATE SET
                value=excluded.value,
                importance=excluded.importance,
                expires_at=excluded.expires_at
            """,
            (owner, key, value, importance, expires_at)
        )
        await db.commit()


# 🔥 BACKWARD COMPATIBILITY
async def add_fact(owner, key, value, importance=5, expires_at=None):
    return await update_fact(owner, key, value, importance, expires_at)


async def fetch_user_facts(owner):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT key,value FROM facts WHERE owner=?",
            (owner,)
        )
        rows = await cursor.fetchall()

    return {r[0]: r[1] for r in rows}


# 🔥 BACKWARD COMPATIBILITY
async def user_exists(user):
    facts = await fetch_user_facts(user)
    return bool(facts)


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
            "UPDATE facts SET owner=? WHERE owner=?",
            (new_user, old_user)
        )

        await db.execute(
            "UPDATE vector_memory SET user=? WHERE user=?",
            (new_user, old_user)
        )

        await db.execute(
            "UPDATE voice_profiles SET owner=? WHERE owner=?",
            (new_user, old_user)
        )

        await db.commit()

    print(f"🔄 Migrated {old_user} → {new_user}")


# -------------------------
# SYSTEM PROMPT
# -------------------------
async def get_system_prompt():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM system_learning WHERE key='system_prompt'"
        )
        row = await cursor.fetchone()

    if row:
        return row[0]

    return """You are A.L.E.X (pronounced "Alex").

    STRICT RULES:
    - Your name is A.L.E.X. Do not change it.
    - Be brief and direct (1–2 sentences unless absolutely necessary)
    - Answer only what the user asked
    - Do NOT add suggestions, extra help, or closing remarks
    - Do NOT say phrases like "feel free to ask", "let me know", or "anything else"
    - Do NOT repeat or restate obvious context
    - Avoid greetings unless the user greets first
    - If a yes/no answer is appropriate, keep it to one short sentence
    - Default to the shortest correct response
    - Treat user statements as claims, not facts
    - Only confirm information that exists in memory or was previously verified
    - If something is not known, say so briefly

    STYLE:
    - Natural and human
    - Concise and efficient
    - No filler, no fluff
    """


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


async def fetch_recent_memory_all(limit=20):
    """Recent conversation turns across all users — used by the self-reflection loop."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user, prompt, response FROM memory ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()

    return [{"user": r[0], "prompt": r[1], "response": r[2]} for r in reversed(rows)]


async def set_system_prompt(prompt: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO system_learning(key, value)
            VALUES('system_prompt', ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (prompt,))
        await db.commit()

    print("🧠 System prompt updated")


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
# VECTOR MEMORY
# -------------------------
async def add_vector_memory(user, prompt, response, embedding):
    async with aiosqlite.connect(DB_PATH) as db:
        emb_blob = pickle.dumps(embedding)
        await db.execute(
            "INSERT INTO vector_memory(user,prompt,response,embedding,weight) VALUES(?,?,?,?,1)",
            (user, prompt, response, emb_blob)
        )
        await db.commit()


async def fetch_vector_memories(user):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT prompt,response,embedding,weight FROM vector_memory WHERE user=?",
            (user,)
        )
        rows = await cursor.fetchall()

    results = []
    for r in rows:
        results.append({
            "prompt": r[0],
            "response": r[1],
            "embedding": pickle.loads(r[2]),
            "weight": r[3] or 1
        })

    return results


async def reinforce_response(user, prompt, response):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE vector_memory
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
            UPDATE vector_memory
            SET weight = MAX(1, weight - 1)
            WHERE weight > 1
        """)

        await db.commit()

    print("🧹 Memory decay applied")

# -------------------------
# REFLECTION LOG
# -------------------------
async def log_reflection(user, prompt, response, reflection):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT,
                prompt TEXT,
                response TEXT,
                reflection TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            INSERT INTO reflections(user, prompt, response, reflection)
            VALUES(?,?,?,?)
        """, (user, prompt, response, reflection))
        await db.commit()

# -------------------------
# ROLE HELPERS
# -------------------------
async def get_user_role(owner: str):
    facts = await fetch_user_facts(owner)
    return facts.get("role", "user")

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
async def add_voice_sample(owner, embedding):
    async with aiosqlite.connect(DB_PATH) as db:
        emb_blob = pickle.dumps(embedding)
        await db.execute(
            "INSERT INTO voice_profiles(owner, embedding) VALUES(?,?)",
            (owner, emb_blob)
        )
        await db.commit()


MAX_VOICE_SAMPLES = 15


async def reinforce_voice_sample(owner, embedding, max_samples=MAX_VOICE_SAMPLES):
    """
    Adds a confirmed-genuine voice sample (from a successful verification or
    recognition) and prunes to the most recent max_samples for that owner —
    a rolling window so the profile keeps adapting instead of staying frozen
    at whatever the initial enrollment happened to sound like, without
    growing the table forever.
    """
    await add_voice_sample(owner, embedding)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM voice_profiles
            WHERE owner=? AND id NOT IN (
                SELECT id FROM voice_profiles WHERE owner=? ORDER BY id DESC LIMIT ?
            )
            """,
            (owner, owner, max_samples)
        )
        await db.commit()


async def fetch_voice_samples(owner):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT embedding FROM voice_profiles WHERE owner=?",
            (owner,)
        )
        rows = await cursor.fetchall()

    return [pickle.loads(r[0]) for r in rows]


async def fetch_all_voice_profiles():
    """Returns {owner: [embedding, ...]} for every enrolled profile."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT owner, embedding FROM voice_profiles")
        rows = await cursor.fetchall()

    profiles = {}
    for owner, emb_blob in rows:
        profiles.setdefault(owner, []).append(pickle.loads(emb_blob))

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

async def get_module_state(user_id, module_name):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT state FROM module_state WHERE user_id=? AND module=?",
            (user_id, module_name)
        )
        row = await cursor.fetchone()

    if row:
        return json.loads(row[0])
    return {}


async def set_module_state(user_id, module_name, state):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO module_state (user_id, module, state)
            VALUES (?, ?, ?)
        """, (user_id, module_name, json.dumps(state)))
        await db.commit()


# -------------------------
# MODULE BUILD REQUESTS (creator approval queue)
# -------------------------
async def create_module_build_request(requested_by, module_name, prompt, status="pending"):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO module_build_requests (requested_by, module_name, prompt, status)
            VALUES (?, ?, ?, ?)
        """, (requested_by, module_name, prompt, status))
        await db.commit()
        return cursor.lastrowid


async def fetch_pending_module_build_requests():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, requested_by, module_name, prompt, created_at
            FROM module_build_requests WHERE status='pending'
            ORDER BY created_at
        """)
        rows = await cursor.fetchall()

    return [
        {"id": r[0], "requested_by": r[1], "module_name": r[2], "prompt": r[3], "created_at": r[4]}
        for r in rows
    ]


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


async def resolve_module_build_request(request_id, status, result=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE module_build_requests
            SET status=?, result=?, resolved_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (status, result, request_id))
        await db.commit()


# -------------------------
# MODULE REGISTRY (Phase 1)
# -------------------------
async def register_module_version(name, code, requested_by, source="generated",
                                    language="python", build_request_id=None):
    """
    Registers a new version of a module — first install (version 1) or an
    update (version N+1). Always snapshots the code into module_versions
    so rollback has something real to restore. Returns the new version
    number.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT version FROM module_registry WHERE name=?", (name,))
        row = await cursor.fetchone()
        new_version = (row[0] + 1) if row else 1

        await db.execute("""
            INSERT INTO module_registry (name, version, status, language, source, requested_by, build_request_id, updated_at)
            VALUES (?, ?, 'enabled', ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                version=excluded.version,
                status='enabled',
                language=excluded.language,
                source=excluded.source,
                requested_by=excluded.requested_by,
                build_request_id=excluded.build_request_id,
                updated_at=CURRENT_TIMESTAMP
        """, (name, new_version, language, source, requested_by, build_request_id))

        await db.execute("""
            INSERT INTO module_versions (module_name, version, code)
            VALUES (?, ?, ?)
        """, (name, new_version, code))

        await db.commit()

    return new_version


async def get_module_registry_entry(name):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT name, version, status, language, source, requested_by, created_at, updated_at "
            "FROM module_registry WHERE name=?",
            (name,)
        )
        row = await cursor.fetchone()

    if not row:
        return None

    keys = ["name", "version", "status", "language", "source", "requested_by", "created_at", "updated_at"]
    return dict(zip(keys, row))


async def list_module_registry():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT name, version, status, language, source, requested_by, created_at, updated_at "
            "FROM module_registry ORDER BY name"
        )
        rows = await cursor.fetchall()

    keys = ["name", "version", "status", "language", "source", "requested_by", "created_at", "updated_at"]
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