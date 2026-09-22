# A.L.E.X. — What you can say to her

The one list of every fixed phrase she listens for, who may say it, and
what gate applies. Rewritten 2026-09-21 from the code as it stands; the
July 2026 version had drifted (it still said answers were auto-stored).

**How this stays true.** `python -X utf8 tools/commands_drift.py` reads
the trigger lists and `startswith("...")` literals out of the code and
fails if any of them is missing from this file. Run it after adding or
changing a trigger. She can read this file herself: ask her what you can
tell her to do and she reads it (her `read_my_source` tool). You can read
it in the Controller's **Commands** tab, or from the **What can I say?**
button on her page, which opens it at `/commands`.

**Where this is going.** Craig, 2026-09-21: "She should be able to derive
my goal through speech." Everything in section 2 already works that way,
and the keyword paths in sections 6 and 7 are to be retired as the tool
path proves reliable (roadmap item 1). Sections 3, 4, 8 and 9 are
security-relevant and stay deterministic on purpose: a misheard "delete
database row" is worse than a rephrase.

---

## 1. Getting her attention, by voice

| To | Say | Notes |
|---|---|---|
| Address her | her name, **"Alex"**, anywhere in the sentence | As its own word, in any position. |
| Keep talking without her name | nothing extra, within **45 seconds** of her finishing a reply | The window restarts every time she finishes speaking. Past it, the page shows the dropped sentence as "not addressed". |
| Bring back what she just ignored | her name, within **60 seconds** of the dropped sentence | **"Alex."** alone (or with a word or two) makes her answer the dropped sentence. **"Alex, what do you think?"** answers that, with the dropped sentence as what you are probably referring to. |
| Answer "Did you say ...?" | **yes / yeah / yep / yup / y / correct / right / affirmative / okay / ok / mhm** to confirm, or just repeat or correct it | Either counts as addressed, wake word or not. Your correction is never itself re-asked. |
| Pass her voice check at connect | anything, in your own words | She compares the speaker, not the words. She will never give you a phrase to repeat; if she does, that is a bug. |
| End the conversation | **"stop listening"** / **"quit listening"** (anywhere in the sentence) | Closes the window at once, no reply. |
| End it politely | **"that's all / that's it / that's enough / that'll be all / we're done / I'm done / never mind / goodbye / good night / talk (to you) later / catch you later"**, near the end of the sentence | She replies once, then the window closes. |
| Interrupt her | start talking | Barge-in: she stops. A word said over her long reply may be lost to the recorder; say it again after she stops. |

## 2. What she now decides for herself — no phrase needed

Ask naturally. She chooses to look before answering; these are her tools
(`core/tools.py`), all read-only, all logged to the Reasoning tab:

| She can | Tool | Example |
|---|---|---|
| search what was said between you | `search_memory` | "Did I ever mention NASCAR?" |
| list your recent exchanges | `recent_turns` | "What were we talking about ten minutes ago?" |
| read her own state: modules, what is off, response times, your standing instructions — **creator only** | `my_state` | "What's switched off on you?" / "How fast have you been answering?" |
| list her modules and what each does | `list_modules` | "What modules do you have?" |
| run one of her modules | `run_module` | "Use recall to check for anything about YouTube." |
| run her diagnostics and report them in her own words | `run_diagnostics` | "Run a diagnostic." |
| read her own log — **creator only**; it carries every user's turns | `read_log` | "Anything in your log I should know about?" |
| read her own source, a page at a time — **creator only** | `read_my_source` | "Read me the top of core/self_model.py." |
| the clock | `current_time`, and always in her context | never something she guesses |
| read her own test scores: latest and previous per suite, weakest categories | `my_scores` | "What are you bad at?" / "Did the last change help?" |
| read the projects you have planned for her and where each stands (kept at the Controller, Her → Projects) | `my_projects` | "What's next for you?" / "How is the camera work coming along?" |
| ask you to consider a change to one of five whitelisted settings of hers — **creator only**; it only files a request; the Controller's Versions tab builds, tests and decides | `propose_change` | "If you could change one thing about how you look things up, what would it be?" |
| her module names | always in her context | never something she guesses |

Not a tool, by design: web search (section 5) — it goes online, so it
needs your approval every time.

## 3. Correcting her ("stop saying that")

Yours bind. Anyone else's are hers to weigh, and her decision is recorded.

| Say | She does |
|---|---|
| **"stop saying X"**, **"don't say X"**, **"quit saying X"**, **"never say X"**, **"do not say X"**, **"stop with X"**, **"stop repeating X"**, **"you keep saying X"**, **"no more X"**, **"enough of X"**, **"stop referencing / mentioning / bringing up X"**, **"stop calling me X"**, **"stop referring to me as X"**, **"stop saying my name"** | records a correction against X. "X and Y" records two. A phrase in quotes wins outright. |
| **"stop saying that"** / **"stop that"** / **"drop that"** / **"stop it"** | she works out which phrase she has been repeating; if nothing repeats, she asks which you meant. |
| "say X again and there will be **repercussions / consequences / trouble**" / "if you say X again" | same as a correction. |

Escalation: first time, a nudge she can still override; second, a standing
rule about you; third and after, the phrase is removed from her output
automatically. A correction is not a personality change and needs no
code. Saying it **with** the override code makes it a standing
personality rule instead (section 9).

Not a correction: quoting her back ("that's where you said that") — she
knows the difference now.

## 4. Yes-or-no questions she asks you

One vocabulary everywhere (`core/text_utils.py`), first word of your
reply:

- **Yes**: yes, y, yeah, yep, yup, confirm, confirmed, sure, ok, okay, keep, save, store, please, do, go, affirmative, correct, right, fact, definitely, absolutely
- **No**: no, n, nope, nah, don't, dont, skip, forget, delete, drop, never, trash, bin, discard, negative
- Anything else means "moved on": the question is dropped, not left hanging, and a dropped retain shows up in the Controller's Activity tab.

| She asks | When |
|---|---|
| whether to search the web | after "look up X" (section 5) |
| whether to keep what she found | after a search |
| whether to keep an answer she thinks was worth it | occasionally, after a factual answer of hers; at most once per two minutes |
| to confirm a fact change | "yes / y / confirm" or "no / n", within 30 seconds |
| to confirm an elevated-access grant | after "approve request N" (section 9) |

## 5. Looking things up online (everyone; the only path to the internet)

**"look up X"**, **"search for X"**, **"search the web for X"**, **"google X"**
→ she asks your approval → yes → she searches, reports the findings
verbatim, and asks whether to keep them (section 4). Kept findings never
expire. Pending approvals time out after 60 seconds.

## 6. Her memory, by keyword (stays for now: measured 2026-09-21, the 9b reads "what do you remember about X" as a topic question, so this phrasing needs the keyword)

**"remember"**, **"recall"**, **"your memories"**, **"memories"** route to the
recall module directly:

- with **about / on / regarding / concerning / of** a topic → what she has stored about it ("what do you remember about NASCAR?")
- otherwise → your last ten exchanges
- a question that names the module ("what does the recall module do?") gets its description instead.

## 7. Her health, by classifier (to be retired the same way)

Any phrasing that asks her to check herself — "are you okay", "run a
diagnostic", "check your systems", "is everything working" — is classified
as a status check. She measures, then reports in her own words, adding
nothing. Known over-trigger: "things are working but not behaving as I
expect" gets a status report; that is on the list.

**"can you hear me"**, **"are you listening"**, **"are you there"** → a short
presence reply, not a report.

## 8. Facts about you (everyone)

| Say | Effect |
|---|---|
| **"my name is X"**, **"call me X"**, **"you can call me X"**, **"I go by X"** | stores your name/alias |
| **"my favorite color is X"** | stores it |
| **"my job is X"**, **"I work as X"** | stores it |
| **"forget / remove / clear / delete my name / nickname / alias / favorite color / job"** | deletes that fact |
| **"set my edit code 1234"**, **"change my edit code 1234"**, **"update my edit code 1234"** (or "set the edit code", "change the edit code", "update the edit code") | sets your edit code; digits |
| **"unlock"** / **"enable edit"** / **"enable editing"** + your code | unlocks your profile for edits |
| **"lock profile"**, **"lock my profile"**, **"lock the profile"**, **"re-lock profile"**, **"relock profile"**, **"secure my profile"** | locks it again |
| **"set override code X"**, **"change override code X"**, **"update override code X"** (or "set the override code", "change the override code", "update the override code") | creator or admin only |

Hypotheticals ("what if my job was X", "suppose", "imagine") are never
stored. The keys `edit_code`, `override_code` and `role` cannot be changed
through the ordinary "my X is Y" path.

## 9. Creator commands

All of these need a voice-verified creator session, **or** the override
code said in the same sentence, which works from any session. Some need
the code regardless, marked **code**.

**Personality**

| Say | Gate |
|---|---|
| **"set your personality to ..."** / **"override your personality to ..."** | **code** |
| any open-ended change ("be snarkier", "stop using emojis", "be more direct") — classified, not a fixed phrase | **code** |
| **"reset your personality"** / **"go back to your default personality"** / **"go back to default"** / **"default personality"** | **code**; also clears your standing rules |
| **"what is your personality"** / **"what's your personality"** | verified |
| **"reset your phrases"** / **"reset how you talk"** | **code** |

Standing rules (the verbatim instructions she keeps) and her beliefs about
you are viewed, removed, confirmed or retracted only at the Controller
(both under the Her tab: Personality, and Beliefs). There is
no voice command for either, on purpose.

**Roles**

- **"grant super user to NAME with override code CODE"**
- **"revoke super user from NAME with override code CODE"**
- She refuses to change anyone whose role is creator.

**Systems** (creator or super user unless noted)

- **"disable system NAME"** / **"enable system NAME"** / **"list systems"**
- **"reload system NAME"** — creator only. Her systems also reload themselves when their files change.

**Modules** (creator or super user)

- **"disable module NAME"** / **"enable module NAME"** / **"list modules"**
- **"list access requests"** / **"list pending access"**
- **"approve request N"** (or "request N approved") → she reads back exactly what access is being granted → **"yes"**

**Database** (creator)

- **"list database tables"**
- **"show database table NAME"**
- **"edit database row ID in TABLE set FIELD to VALUE"** — exact shape
- **"delete database row ID in TABLE"** — exact shape

## 10. Things she may say to you first

| She says | What to do |
|---|---|
| a question of her own, about something you mentioned or about herself | answer in a full sentence and she keeps it, and will not ask again; two words is not an answer |
| "Still there? Just checking in." | after 15 minutes of silence in an open session; anything you say counts |
| a question about something of hers that has been switched off | your reply is taken as the reason; she stops asking about that one |
| "want me to keep that?" | section 4 |

## 11. Not commands, and why

- **"It's me"** is nothing; she verifies by voice, not by being told.
- **"stop"** alone is not "stop listening".
- **"you said that"** is quoting, not correcting (fixed 2026-09-21).
- **"keep it"** said while she is still talking may never reach her; wait for her to finish.
- **"Override [1]"** and any bracketed command syntax she may have offered you does not exist and never did.
