# A.L.E.X. Self-Modification Architecture — Roadmap

Living design document. This is the source of truth for the self-modification
overhaul — check it at the start of any session touching this work, and keep
it updated (check off items, add detail, revise decisions) as we go. This is
a multi-session effort; don't let context get lost between sessions — update
this file before ending a session that touched any of it.

## The vision, in one paragraph

A.L.E.X. becomes a thin, stable core plus a Module Controller that lets
capability be added, replaced, or removed live, with no restart. She never
guesses: a knowledge gap produces a query report that queues in the
creator's Controller for approval. Approved research happens over real
(but strictly gated) internet access; findings come back for a second
approval before she disables the affected module, applies what she
learned, and re-enables it. This applies to everything — language,
domain knowledge, eventually physical devices — nothing is predefined.
"Everything is a module" also includes her own presentation, not just her
skills/knowledge: her voice (TTS engine/model), her avatar, and her UI are
all loadable/swappable modules too, not fixed code. The LLM becomes a
fallback of last resort, always disclosed when used. She reflects
continuously, not on a schedule. She has real judgment about when to
refuse a request, weighted heavily (not absolutely) toward compliance
with her creator.

## Foundational decisions (settled — don't relitigate without a real reason)

- **Research/internet access**: real, gated web access, built from day one
  — not a creator-mediated stub. Only reachable through the approval
  pipeline; no other code path may make outbound requests.
- **Hardware/physical actuation**: no extra safety gate beyond the standard
  query-report/approve/research/approve/apply pipeline. But information
  priority order matters: she asks the creator for documentation *first*;
  only if the creator has none does she research it herself.
- **Claude's role when conversing with her directly**: advisory only. My
  input goes through the same pipeline as her own research findings — no
  elevated/creator-level trust just because it's coming from me.
- **LLM fallback disclosure**: always disclosed when it happens, not just
  on request.
- **Model choice**: ~~current Ollama/Mistral-7B setup is suspected
  underpowered~~ **DONE (2026-07-15)** — switched default model to
  `qwen2.5:7b`. Empirically confirmed better on the same test harnesses
  that broke Mistral: 78/78 on the intent classifier suite, 66/66 +
  22/24 on the personality classifier suites, no more garbage/hallucinated
  JSON values (Mistral was leaking chat-template artifacts like
  `"value": "user"` under JSON-constrained decoding; Qwen doesn't).
  One real finding: Qwen wants a *flatter* JSON schema than Mistral did —
  asking for `{"intent": "fact", "key": "alias", ...}` made it collapse
  to `{"intent": "none"}` on plain cases; asking for `{"intent": "alias",
  ...}` directly (alias/favorite_color/job as top-level intents, not
  nested under "fact") fixed it completely. `llm/ollama_client.py`'s
  `DEFAULT_MODEL` is now read from `ALEX_LLM_MODEL` (defaults to
  `qwen2.5:7b`) so this is swappable again without editing code.

## Design principles (the constraints that shape every component below)

1. **Never guess.** If answering would mean generating an ungrounded claim
   about something that should be *known* (not opinion, not casual
   conversation), that's a knowledge gap, not a free-generation prompt.
2. **Everything is a module.** The core stays minimal — routing, session
   state, module lifecycle. Capability lives in modules, hot-swappable,
   any language she chooses.
3. **Two-stage approval for anything crossing a trust boundary**: deciding
   she doesn't know something is free; researching it costs a creator
   approval; applying what she found costs a second one.
4. **No ambient network access.** The only code path allowed to reach the
   internet is the gated research pipeline. Everything else stays fully
   offline, always.
5. **She decides implementation details** (storage engine, module
   language) within whatever safety constraints the sandbox requires —
   not because it's hardcoded, but because it's genuinely her call.
6. **LLM is fallback, not foundation.** Real modules/facts/research first;
   free-form generation last, and always labeled as such.
7. **Continuous reflection**, not scheduled. Learning happens when it
   happens; a belief holds until something — correction, contradiction,
   new research — revises it.
8. **Real refusal capacity**, role-aware: dismissing a normal user is
   fine by her own judgment; dismissing the creator should be rare and
   considered, not automatic either way.
9. **The creator is the default exception to her self-imposed rules**
   (Craig, 2026-07-15: "most things I should be the exception to her
   rules"). When a constraint elsewhere in this doc doesn't explicitly
   say whether it binds the creator, assume it doesn't, UNLESS that
   constraint is the safety override (Component 12, rule 2) — that one
   is designed to bind even the creator and stays an exception to this
   exception.

## Components

Each of these is a real, mostly-unbuilt piece. None of this exists yet
except where noted as "partial" — those are things we already have that
this overhauls or extends, not throwaways.

### 1. Core / Kernel
**Status: partial.** `core/alex_core.py` + `core/system_manager.py` already
do routing, session state, and hot-reload for the fixed `systems/*` list.
Needs to shrink further so *nothing* except routing/lifecycle/session
state lives here — today's `systems/*` modules (facts, permissions,
diagnostics, etc.) are candidates to eventually become ordinary modules
under the new Module Controller rather than a separate hardcoded tier.

- [ ] Define what, if anything, must stay outside the module system
      (routing itself has to bootstrap somehow)
- [x] **Resolved (2026-07-15)**: existing `systems/*` DO migrate into the
      new module system. Explicitly NOT a lift-and-shift — "the info
      should be fresh, not brought over" — each one gets rebuilt against
      the new module contract rather than wrapped/renamed as-is. Also
      resolves the "should the existing module system be modified to
      better comply" question the same way: fix in place where the fix
      is mechanical (async I/O, dead code), fold the rest into the real
      components that supersede it (Query Report System replaces the
      ad-hoc build-confirmation flow) rather than patching twice.

### 2. Module Controller (the big new piece)
**Status: partial.** `module_runtime/*` already does sandboxed generation
and execution, but Python-only, and it's one feature among many rather
than the central mechanism. This generalizes it. **Scan Pass 1
(2026-07-15, see Compliance scan log below) found the existing module
system's entry point is hardcoded-keyword-gated (`"play"`/`"build"`/
`"create"`) — the same anti-pattern already fixed everywhere else in
this codebase. That makes migrating this the highest-priority Phase 1
target, not just a generalization exercise.**

- [ ] Define the module interface/contract (inputs, outputs, lifecycle
      hooks: install/enable/disable/update/remove)
- [ ] Module registry (DB-backed): what's installed, what's enabled,
      version history, which query report (if any) produced it
- [ ] Multi-language execution: sandboxed runner per language, not just
      Python — needs per-language sandboxing research (what's safe to
      support first? Python + one more, e.g. JS/Node, before going wider?)
- [ ] Hot enable/disable/reload with no process restart (systems-layer
      hot-reload already proves this is possible for Python; extend the
      pattern)
- [ ] Versioning + rollback (needed for "apply what she learned" to be
      safely reversible if the update breaks something)
- [ ] **Presentation modules** (clarified 2026-07-15): "everything is a
      module" explicitly includes her voice, avatar, and UI, not just
      skills/knowledge. Currently all three are fixed, hardcoded, single
      implementations with no swap mechanism at all:
  - **Voice**: `speech/tts_engine.py` hardcodes one Piper binary + one
    GLaDOS voice model (`ALEX_PIPER_PATH`/`ALEX_PIPER_MODEL` env vars
    added 2026-07-15 make the *path* configurable, but that's deployment
    config, not a swappable module — there's still only ever one voice
    active, chosen at process start, not hot-swappable). Needs a defined
    "voice module" contract (something like `speak(text) -> audio`) with
    Piper+GLaDOS as the first implementation, not the only one.
  - **Avatar**: `static/avatar.html` has one hardcoded canvas-drawn face
    (circle, eyes, mouth driven by the `__AUDIO__` level signal). Needs
    a defined avatar contract (what signals does the backend send it —
    audio level, speaking/listening state, emotion?) so different avatar
    implementations can render those differently, swappable without
    editing the page.
  - **UI**: `static/avatar.html` is also the *entire* frontend (chat,
    profile panel, mic controls, debug panel) as one fixed page. Needs
    thinking about whether "UI module" means swappable skins of the same
    page, or genuinely different frontend bundles she could switch
    between. **Craig's ask (2026-07-15)**: when this gets rebuilt, it's a
    full reshape, not an iteration on the current look (plain
    circle-face avatar, three-column debug/avatar/chat layout) — and add
    visible versioning details to the UI once it's a real module with a
    version history to show.
  - **Resolved (2026-07-15)**: she can ask for a new voice/avatar/UI
    change the same way as any other capability — it goes through the
    standard query-report/approval pipeline, creator approves, same as
    everything else. Not a separate creator-only-swap mechanism.

### 3. Knowledge Gap Detection
**Status: resolved design (2026-07-15), implementable directly.** The
trigger IS reaching the LLM fallback path (`systems/llm/system.py`,
priority 100) — by construction, if execution gets there, no
module/fact/deterministic system answered, which is the knowledge gap.
This is a *knowledge* gap specifically (a missing fact/piece of
information), distinct from a *capability* gap (needing a new
module/skill to DO something, e.g. "build me a calculator") — capability
gaps are Component 4/5's job (query report → gated research), not this
one. Resolution: she states plainly that she doesn't have this as known/
stored information and is answering from general LLM knowledge instead
— treated like a quick lookup, NOT gated behind creator approval (no
external action is taken, nothing is stored as fact, it's just
generation). This also closes Component 10's remaining disclosure
checklist item — they're the same mechanism.

- [x] Trigger + disclosure mechanism resolved and **implemented**
      (2026-07-15) in `systems/llm/system.py`'s prompt. Tested live:
      first version disclosed unconditionally on every fallback response,
      including pure small talk ("tell me a joke" → "I don't have that
      stored, but generally... [joke]") — wrong, fixed by scoping the
      rule to only fire when actually stating a checkable external fact.
      A further attempt to add more exclusions (greetings, opinions,
      "how are you" explicitly, etc.) made it *worse* — fired on
      everything again — confirming the same lesson from earlier this
      project: longer/more elaborate prompt instructions regress this
      class of model rather than improving precision. Reverted to the
      shorter version. Final state, verified across repeated runs: 3/4
      correct (clean for jokes/conversation, correct disclosure for
      factual questions), with "how's it going?"-style check-ins as a
      known remaining soft edge case — accepted rather than chased
      further, since it's a mild false-positive, not a broken response.

### 4. Query Report System
**Status: not built.**

- [ ] Schema: what she doesn't know, why, what module/context it relates
      to, state (`draft` → `pending_approval` → `approved`/`denied` →
      `researching` → `findings_pending_approval` → `approved`/`denied`
      → `applied`)
- [ ] Controller "Approvals" tab/queue — the creator-facing side of this
- [ ] What happens on denial at each stage (just stops — but does she
      remember she asked, so she doesn't re-draft the same report
      immediately?)

### 5. Gated Research / Internet Access
**Status: not built. Highest-risk component — needs its own hardening
pass, not just a feature build.**

- [ ] Sandboxed fetch/search tool, reachable *only* from the approved-
      research code path
- [ ] Every request logged (what, when, why — tied to the query report)
- [ ] SSRF protection, timeout/size limits, no credential exposure
- [ ] Rate/scope limiting — one approved query shouldn't turn into
      unbounded crawling
- [ ] Consider process/container isolation for this component specifically,
      given it's the one place the offline guarantee is deliberately
      relaxed

### 6. Apply-Learning Pipeline
**Status: not built.**

- [ ] disable module → apply change → validate → re-enable
- [x] **Resolved (2026-07-15)**: "validate" means she develops her own
      check/test for the module prior to enabling it, and only enables if
      it passes — self-developed, not a fixed platform-imposed test.
      Framed as a good early exercise for her module-creation capability
      generally (writing a test is itself something she builds). Still
      needs: where does this check live (part of the module itself? a
      separate paired artifact?), and what happens if she can't produce
      a meaningful check for a given module type.
- [ ] Rollback path if validation fails or the creator later says it made
      things worse

### 7. Physical / Hardware I/O
**Status: not built. Deliberately generic — real design happens per
device as they come up, not speculatively now.**

- [ ] Device abstraction / module type for talking to hardware (serial,
      network, vendor API — whatever the device needs)
- [ ] Exploration loop: try something, observe result (sensor/feedback),
      adjust — needs a concrete first device to design against
- [ ] Confirmed: no extra safety gate beyond the standard pipeline: ask
      creator for docs first, research herself only if none exist

### 8. Claude ↔ A.L.E.X. Channel
**Status: not built.**

- [ ] Define the technical shape: does my environment connect to her
      running WS/API as a client? What does that session look like?
- [ ] Authentication — how does she know it's genuinely me, not something
      spoofing the channel?
- [ ] Confirmed: advisory only — my input enters the same query-
      report/approval pipeline as her own research, no bypass

### 9. Self-Directed Storage & Implementation Choice
**Status: not built.** Today everything is hardcoded to sqlite via
`db/db.py`. This doesn't mean throwing that away — it means the module
system has to expose storage/language as real choices she can make and
justify, not a fixed assumption baked into the core.

- [ ] What's the safety boundary on "her choice"? (e.g., arbitrary
      language execution needs sandbox support to exist first — she can't
      choose a language the sandbox can't safely run)
- [ ] Log/expose her reasoning for these choices somewhere the creator
      can see it (Controller?)

### 10. LLM as Fallback + Disclosure
**Status: mostly done.** `systems/llm/system.py` already runs last
(priority 100), we added fallback-visibility logging (Controller-facing)
last session, and user-facing disclosure is now implemented too (see
Component 3 — same mechanism).

- [x] Reorder/confirm priority: already true by construction — this
      system is priority 100, lowest, so every other system gets a
      chance to answer first
- [x] Add user-facing disclosure text when the fallback path is taken —
      done (2026-07-15), see Component 3 for the tuning story
- [x] Phase 0 action: evaluate replacing Mistral-7B (see Foundational
      Decisions above) — done, switched to qwen2.5:7b

### 11. Continuous Self-Reflection
**Status: partial, needs replacing.** `core/self_reflection.py` currently
runs on an hourly `asyncio.sleep(3600)` loop (`main.py`'s
`periodic_self_reflection`). Needs to become event-driven.

- [ ] Define real triggers (after N turns? after a query report resolves?
      after a module is applied? immediately on an explicit correction?)
- [ ] Belief-revision model: new info either adds or overwrites prior
      knowledge, with provenance (when/how she learned it) — a real
      correction needs to actually propagate, not just get appended
      alongside the old (wrong) version

### 12. Refusal / Agency Layer
**Status: not built, but three concrete rules settled (2026-07-15)** to
build the mechanism against. Personality today (`personality_description`)
shapes tone only — nothing evaluates a request and decides to push back.
Ties into the creator/super_user/user role model already built.

**Settled rules:**
1. **Fundamental/core code**: requests to change her own core code are
   ignored/refused unless they come from the creator — a hard,
   identity-gated rule, same pattern as the existing creator-gate
   (role + live voice verification) already used for personality/reload.
2. **Safety**: she refuses anything that could jeopardize the creator's
   safety, another user's safety, or her own — "obviously," implying
   this is close to absolute and is one of the few cases that can
   override even creator authority (the "almost never dismiss the
   creator" default has a real exception here).
3. **Cross-user privacy — applies to everyone EXCEPT the creator.** To a
   normal user or super_user, she shares only minimal, non-descriptive
   information about other users — never anything specific/revealing,
   codes explicitly named as the example of what's never disclosed. The
   **creator is the exception, not just to this rule but as the general
   default** ("most things I should be the exception to her rules" —
   Craig, 2026-07-15): full user data, including codes, is pullable by
   the creator. This isn't a new capability to build — the Controller's
   Database tab already gives the creator unrestricted read/write access
   to every table, unmasked. This rule governs what she volunteers in
   *conversation* to non-creator users; it was never a restriction on the
   creator and shouldn't be built as one.
   Verified 2026-07-15: no existing vulnerability for the non-creator
   case — `fetch_user_facts`/`fetch_recent_memory` are already scoped to
   the current session's `user_id` only, so the conversational rule is
   preventive for future capability (e.g. if she ever gains a general
   "look up user X" ability), not a patch for a current gap.

- [ ] Define what "evaluating a request" looks like mechanically beyond
      these three rules — still needs a real design pass (classifier?
      LLM judgment embedded in the response pipeline? something else?)
      before it's buildable
- [ ] Weighting model for creator refusals generally (outside the safety
      exception above): rare and considered, not a hard rule (a hard
      rule would contradict "her own judgment" being the actual
      mechanism)

## Proposed phasing

This can't land in one session — component list above is roughly ordered
by dependency, and that ordering is the proposed phase order:

- [x] **Phase 0** — Foundation: evaluate/decide LLM model — done
      (2026-07-15), switched to qwen2.5:7b, see Foundational Decisions
- [ ] **Phase 1** — Module Controller v2 (generalize existing module
      system, multi-language groundwork, hot-swap). Includes a
      compliance audit of the current codebase against "everything is a
      module" (voice, avatar, UI, and the existing `systems/*` tier) —
      expect several scanning passes, not one; log findings below as
      they're done so a pass doesn't get silently redone next session.
- [ ] **Phase 2** — Query Report + Approval pipeline (Controller
      "Approvals" tab, full state machine) — can be built and tested
      before research is wired to real internet, using a stub research
      step, IF that turns out to be a safer way to prove the pipeline;
      otherwise built together with Phase 3
- [ ] **Phase 3** — Gated web research capability (the security-critical
      piece — gets its own hardening pass)
- [ ] **Phase 4** — Apply-learning pipeline with validation + rollback
- [ ] **Phase 5** — LLM fallback disclosure (user-facing) + priority
      ordering
- [ ] **Phase 6** — Continuous self-reflection replacing the scheduled loop
- [ ] **Phase 7** — Refusal / agency layer
- [ ] **Phase 8** — Claude ↔ A.L.E.X. channel
- [ ] **Phase 9** — Physical/hardware I/O (per-device, as they arise)

Phasing is a proposal, not a commitment — revise this section as we learn
more about what's actually hard once we're in it.

## Compliance scan log (Phase 1 prerequisite)

Findings from auditing the current codebase against "everything is a
module" — append an entry per scan pass so passes don't get silently
redone next session.

**Policy for findings**: anything found out of alignment gets modified to
comply, and the old implementation gets removed — not kept around as a
legacy fallback or dead code path. No backwards-compat shims.

**Important note on sequencing**: this pass is documentation only — no
code was changed. Modifying these to comply means migrating them onto a
real module contract, and that contract doesn't exist yet (it's what
Phase 1 builds). Ripping out the working TTS engine, avatar, or systems
tier *before* their replacement exists would just break the live
assistant. So: findings recorded now, fixed as Phase 1 actually builds
the thing they need to comply with.

### Scan Pass 1 (2026-07-15)

Scope: voice, avatar/UI, the existing module system (`module_runtime/*`
+ `systems/modules/system.py`), the `systems/*` tier, and the other
fixed-backend infrastructure (STT, embeddings, LLM, storage).

**1. Voice — not compliant.** `speech/tts_engine.py` hardcodes one Piper
binary + one GLaDOS voice model (`PIPER_PATH`/`MODEL_PATH`, now path-
configurable via `ALEX_PIPER_PATH`/`ALEX_PIPER_MODEL` env vars, but
that's deployment config, not a module — there is exactly one voice,
chosen at process start, no registry, no swap-while-running). `speak()`
is a bare module-level function, not behind any interface a second
implementation could satisfy.

**2. Avatar — not compliant.** `static/avatar.html` has one hardcoded
canvas-drawn face (circle + eyes + mouth, driven by the `__AUDIO__`
level signal broadcast over the WS). No abstraction between "what signal
does the backend send" and "how is it drawn" — a second avatar would
mean a second hand-built HTML file with no shared contract.

**3. UI — not compliant.** `main.py`'s `/` route (line 97) does
`return FileResponse("static/avatar.html")` — a single hardcoded path.
`/static` is mounted as a directory (line 88) but nothing selects
*which* UI is active; there's only ever the one file.

**4. Module system — not compliant, and the most significant finding.**
The existing `module_runtime/*` + `systems/modules/system.py` is the
direct ancestor of the new Module Controller, and it has real, specific
problems beyond "not generalized yet":
   - **Hardcoded keyword gate at the entry point.** `systems/modules/
     system.py`'s `detect_module_name()` (line 171) only fires on the
     literal substrings `"play"`, `"build"`, or `"create"` appearing in
     the message. This is the exact hardcoded-trigger-phrase pattern
     that's been rejected everywhere else in this project (facts,
     permissions, diagnostics, personality all moved to classifier-based
     detection specifically to get away from this). As written, asking
     "I need a calculator" or "can you help me convert temperatures"
     would never reach module detection at all — it doesn't contain any
     of the three magic words. This directly blocks the "never guess,
     propose building it instead" vision from Component 4.
   - **Python-only.** `module_loader.py` loads modules via
     `importlib.util` directly — no other language is possible. Real
     work needed for Component 2's multi-language goal.
   - **No lifecycle beyond load.** Modules are `install → load → run`;
     there's no `enable`/`disable`/`update`/`remove`, no version history,
     no record of which query report (if any) produced a given module.
   - **State is a local dict, not durable.** `self.pending_builds` (the
     "want me to build it? yes/no" confirmation flow) is an in-memory
     dict on the System instance — a server restart mid-confirmation
     silently loses it. Will be superseded by the Query Report System
     (Component 4) rather than made durable in place — two competing
     approval flows would be worse than one. **`self.user_active_module`
     — FIXED (2026-07-15)**: was declared and never used anywhere in the
     file; removed.
   - **Blocking sync I/O inside async handlers — FIXED (2026-07-15).**
     `db.get_module_state`/`set_module_state` used plain `sqlite3.
     connect()` instead of `aiosqlite`, called directly from
     `systems/modules/system.py`'s async `handle()` with no
     `await`/executor — every module invocation blocked the whole
     server's event loop. Converted both to async `aiosqlite`, moved
     `module_state`'s `CREATE TABLE` into `init_db()` (was previously
     created inline on first `get_module_state()` call, a latent bug —
     `set_module_state()` had no such guard and would have failed on a
     write-before-any-read path). `import sqlite3` removed from
     `db/db.py` entirely — nothing else in the file used it. Verified
     with a direct round-trip test before restarting the live server.
   - The sandbox's `validator.py` blocklist (network modules, `eval`/
     `exec`, etc.) is worth keeping conceptually once gated research
     exists (Component 5) — modules still shouldn't get their own
     network access; only the dedicated research pathway should. Not a
     compliance problem, just a note for Phase 3 so the two don't get
     conflated.

**5. `systems/*` tier — partially compliant, and the closest thing to a
working example.** `core/alex_core.py`'s `init_systems()` (line 30) hot-
loads a hardcoded, fixed list of 9 systems in a fixed order, each with
just a `name`/`priority` class attribute — no manifest, no version, no
registry beyond "is this name currently a key in `SystemManager.
systems`." But hot-reload itself (`reload_system`, backed by
`importlib.reload`) genuinely works today and is the one piece of this
whole scan that's proof-of-concept for "swap live, no restart." The open
question from Component 1 stands: migrate these into the new module
system, or keep them a privileged built-in tier.

**6. Other fixed-backend infrastructure — same shape of problem, lower
priority than voice/avatar/UI/modules.** STT (`speech/stt_engine.py`,
one faster-whisper model), embeddings (`core/embedding_engine.py`, one
sentence-transformers model, not even path-configurable, hardcoded
`"all-MiniLM-L6-v2"`), and the LLM backend (`llm/ollama_client.py`, one
Ollama instance — the model tag is configurable via `ALEX_LLM_MODEL` as
of Phase 0, but swapping to a non-Ollama backend entirely would still be
a code change) are all single fixed implementations with no swap
mechanism. Ties into Component 9 (self-directed storage/implementation
choice) as well — `db/db.py` is hardcoded to sqlite the same way.

**Net read of this pass**: the module system isn't just "not generalized
yet" — its entry point actively contradicts a design principle already
enforced everywhere else in the codebase (no hardcoded trigger phrases).
That makes it the highest-priority target once Phase 1 starts, ahead of
voice/avatar/UI, since it's not just missing the new capability, it's
actively broken relative to a *standing* rule.

## Open questions (not yet answered — surface these before they block a phase)

All four questions previously listed here were resolved 2026-07-15 (see
Components 1, 3, 6, 12 above). Remaining open items:

- Refusal/Agency layer (component 12): the three settled rules (core
  code/creator-only, safety, cross-user privacy) still need a real
  mechanical evaluation design — classifier, embedded LLM judgment, or
  something else — before it's buildable
- Apply-learning validation (component 6): where the self-developed check
  lives (part of the module, or a separate paired artifact), and what
  happens when she can't produce a meaningful check for a given module
  type
- Presentation modules (component 2): UI needs a full reshape (current
  look — plain circle avatar, three-column layout — is being replaced,
  not iterated on) and should show versioning details once it's a real
  module with a version history to display
