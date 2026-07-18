# A.L.E.X. — Command Reference

Every way a message gets routed to something other than plain conversation
(the LLM fallback, `systems/llm/system.py`, priority 100). Built 2026-07-17
because these are scattered across ~10 files with no single list — this is
that list. Update it whenever a trigger phrase changes or a new
creator-gated command is added; it will drift otherwise, same as
`SELF_MODIFICATION_ARCHITECTURE.md`'s "Current State" section did.

Routing order matters — `core/alex_core.py`'s `init_systems()` call
sequence is the real authority on priority, not the `priority` class
attribute (decorative only, kept in sync by convention).

## Creator/role-gated commands (`systems/controller/*`)

All of these require `require_creator()`/`require_privileged()` — creator
role + live voice verification this session, **or**, as of 2026-07-17,
stating the creator's actual override code anywhere in the same message
(works regardless of the current session's voice-verification state —
see `core/override_code.py`'s `is_creator_override_code()`).

| Command | File | Notes |
|---|---|---|
| `"set your personality to <description>"` / `"override your personality to <description>"` | `_personality.py` | Exact phrase, requires override code stated in the same message |
| *(open-ended, e.g. "be snarkier")* | `_personality.py` | Falls back to a dedicated classifier (`classify_personality_set()`) only for creator messages that don't match the exact phrase above |
| `"reset your personality"` / `"go back to your default personality"` / `"go back to default"` / `"default personality"` | `_personality.py` | Deterministic phrase list (`PERSONALITY_RESET_TRIGGERS`), not a classifier — false positives here are dangerous, not just annoying |
| `"what is your personality"` / `"what's your personality"` | `_personality.py` | Read-only, still creator-gated |
| `"reset your phrases"` / `"reset how you talk"` | `_personality.py` | Resets all scripted phrase re-voicings to default |
| `"grant super user to <name> with override code <code>"` / `"revoke super user from <name> with override code <code>"` | `_personality.py` | Refuses to ever touch anyone whose current role is `creator` |
| `"disable system <name>"` / `"enable system <name>"` | `_system_toggle.py` | `require_privileged` (creator or super_user) |
| `"list systems"` | `_system_toggle.py` | `require_privileged` |
| `"reload system <name>"` | `_system_toggle.py` | `require_creator` — manual hot-reload trigger (systems-layer also auto-reloads on file change regardless) |
| `"list database tables"` | `_database.py` | `require_creator` |
| `"show database table <name>"` | `_database.py` | `require_creator` |
| `"edit database row <id> in <table> set <field> to <value>"` | `_database.py` | Regex-shaped on purpose, not a classifier — a DB write misread is worse than a rephrase |
| `"delete database row <id> in <table>"` | `_database.py` | Same reasoning |
| `"disable module <name>"` / `"enable module <name>"` | `_module_admin.py` | `require_privileged` |
| `"list modules"` | `_module_admin.py` | `require_privileged` |
| `"list access requests"` / `"list pending access"` | `_module_admin.py` | `require_privileged` |
| `"approve request <N>"` / `"...request <N> approved"` | `_module_admin.py` | Propose-then-confirm — reads back the specific elevated access being granted, only commits on an explicit "yes"; matched via `ACCESS_APPROVAL_TRIGGER_RE`, not an exact phrase |

## Deterministic, non-classifier triggers (everyone, not creator-only)

| Command | File | Notes |
|---|---|---|
| `"set/change/update (my/the) edit code <digits>"` | `systems/command/system.py` | `SET_EDIT_CODE_TRIGGERS` — deterministic phrase list, kept deterministic on purpose (2026-07-17): same reasoning as `PERSONALITY_RESET_TRIGGERS` below, a classifier false-positive here is a real security cost |
| `"set/change/update (the) override code <code>"` | `systems/command/system.py` | admin/creator role only; `SET_OVERRIDE_CODE_TRIGGERS`, same reasoning |
| `"unlock"` / `"enable edit(ing)"` (+ code) | `systems/command/system.py` | Broad substring match, unchanged |
| `"lock/re-lock/relock (my/the) profile"` / `"secure my profile"` | `systems/command/system.py` | `LOCK_PROFILE_TRIGGERS` |
| yes/no after a pending fact change | `systems/command/system.py` | Generic confirm/decline, `CONFIRM_TIMEOUT=30s` |
| `"look up <query>"` / `"search for <query>"` / `"search the web for <query>"` / `"google <query>"` | `systems/inquiry/system.py` | Two-stage: search approval, then a separate retain approval before it's kept as `learned_knowledge` |
| yes/no on a pending search/retain | `systems/inquiry/system.py` | `PENDING_TIMEOUT=60s`; a stale one now falls through to be re-evaluated fresh rather than eating the next message (fixed 2026-07-17) |
| `"remember"` / `"recall"` / `"your memories"` / `"memories"` | `systems/modules/system.py` | `KNOWN_MODULE_TRIGGERS` — resolves and runs the `recall` module directly. As of 2026-07-17 this system no longer detects implicit build requests at all (`classify_module_gap()` removed) or proposes builds; `diagnostic_tool`/`inquiry` already have their own dedicated trigger systems ahead of this one, so this dict only still matters for `recall` |
| `"are you okay"` / `"check your systems"` / `"is everything working"` / `"run/perform/do a diagnostic"` / etc. | `systems/diagnostics/system.py` | Not a fixed phrase list — routed via `classify_intent()`'s `status_check` category (deliberately broad, catches casual phrasing) |
| bare acknowledgment ("thanks", "okay", "cool"...) right after her own closing-type statement | `systems/llm/system.py` | Suppresses a redundant reply — `ACKNOWLEDGMENT_PHRASES` + `CLOSING_MARKERS`, both deterministic |

## Classifier-routed (`core/intent_classifier.py`'s `classify_intent()`)

One shared call, `session["intent"]`, consumed by whichever system needs
it. Four categories — **do not add a 5th** without re-reading this
project's own history first: adding categories to this shared classifier
has caused real accuracy regressions more than once.

| Category | Consumed by | What it catches |
|---|---|---|
| `fact` | `systems/facts/system.py` | Statements like "my name is X" / "call me X" — value is always re-derived from a real trigger phrase in the user's own text, never trusted from the classifier's own extraction |
| `permission_command` | `systems/permissions/system.py` | Attempts to change a field, checked against `LOCKED_KEYS = ["edit_code", "override_code", "role"]` |
| `status_check` | `systems/diagnostics/system.py` | See table above |
| `none` | — | Falls through to the LLM system (priority 100) |

Hypothetical-language detection ("what if my job was X") is deliberately
**not** classifier-trusted — a fixed deterministic check applied to the
classifier's output, since a wrong call here means storing a false fact
as if confirmed.

## Everything else

Any message not claimed by anything above reaches `systems/llm/system.py`
(priority 100, the real fallback) — either answered from `learned_knowledge`
if a confident match exists (personality-reworded, not verbatim, as of
2026-07-17), or generated fresh and auto-stored/conflict-flagged
afterward.
