# Odd behaviour, and what caused it

Craig, 2026-09-20: "Please continue to monitor our conversation and notate
the strange responses so we can find there cause."

A running log of things she did that looked wrong, what actually caused
them, and whether it is closed. Kept in the repo rather than in a session
so it survives, and because the pattern across entries is more useful than
any single one.

**A warning about reading the logs, learned the hard way.** Three findings
in one review turned out to be artefacts of how *I* was reading the log,
not of anything she did: replies "trailing off", replies "repeated
verbatim", and a recall answer "with nothing after the header". All three
were my own display cutting lines at a fixed width, or splitting a
multi-line response and reading only its first line. One of them had
already been written into this file as an open anomaly and has been
removed. **Before logging a response as broken, read the raw bytes of it.**

**And a correction to that correction (2026-09-21, next session).**
"Repeated verbatim" was withdrawn too far. The replies are not
byte-identical, but #1486 opens with the first 179 characters of #1484,
and 4 of her last 178 replies open with 80 or more characters copied from
one of the previous four. Low-frequency, and real: it is the pattern named
below, her own reply returning in MEMORY and being continued. Check the
database, not only the display.

**The pattern so far:** most of these are not independent bugs. They are
her own output coming back to her as context and reproducing itself. The
seed differs — a hallucination, a bad stored phrase, a default I wrote —
but the loop is the same every time, because the last four turns of
conversation go into every prompt. Anything she says once, she is more
likely to say again.

---

## Closed

### Her log read to a stranger as his own history
**Seen** 2026-09-21 12:28, tool probe on the 9b. A throwaway user asked
"anything in your own log?"; she read the log and answered "One of those
times, I lied and told you you liked green" — a different test user's
conversation, narrated to this one as his.

**Cause** the `read_log` tool returned the log to any user, and the log
carries every user's turns ([ACTION] lines include the message text).
Component 12 rule 3. The same applied to `read_my_source` and to
`my_state`, which carries Craig's standing instructions.

**Fixed** those three tools answer only the creator; memory tools were
per-user already; modules, diagnostics and the clock stay open. Needs a
restart (core/tools.py). The gateway design (item 12) inherits this.

### "What do you remember about NASCAR?" answered with the recall module's description
**Seen** 2026-09-21 05:25 and 05:29, first live use of the 9b. "The recall
(v3) module is built, but it refuses to introduce itself."

**Cause** the module system treated any text ending in "?" as a question
ABOUT the module and read out its help; the recall module had no help(),
hence the odd line. Routing to the module is a keyword ("remember",
"recall", "memories"), not understanding — the item-1 problem. And the
topic parser knew only "about", so "recall information ON NASCAR" dumped
the last ten turns.

**Fixed** a question is about the module only if it names it; recall has
a help(); the topic parser takes about/on/regarding/concerning/of and
stops at the first sentence break. Live via hot reload.

### "Keep it" after a lookup did nothing
**Seen** 2026-09-21 05:31. She reported the YouTube findings, asked, and
his "keep it" went nowhere; he used the Controller.

**Cause** two halves. "Keep it" never reached her: the only audio after
her 30-second answer was 6.9 KB that transcribed to nothing. And it would
not have counted if it had: the retain gate accepted only yes/y/yeah/
confirm, while the composed question said "Fact or trash bin?" — a
free-worded confirmation inviting answers the code did not understand.
His next sentence then counted as "moved on" and the retain fell to the
Controller by design.

**Fixed** one shared yes/no vocabulary (keep, save, fact, sure, okay...
/ no, nope, trash, forget...) for the retain gate and the keep-offer gate;
the phrase intent now says to ask a plain yes-or-no question. Not fixed:
a word said during her long reply is still at the mercy of the mic.

### Asterisks read aloud, words inside them lost
**Seen** 2026-09-21, first 9b conversation. "*do*", and the YouTube
findings' five bulleted bold lines.

**Cause** the 9b writes markdown where the 7b rarely did, and
`speech/tts_engine.py` has had a rule since July that treats anything in
asterisks as a stage direction ("*pauses*") and replaces it with half a
second of silence. So "*done*" was spoken as nothing — the word inside
the emphasis became the pause. Where the asterisks survived to Piper
they were read as "asterisk".

**Fixed** `strip_markdown()` applied where text becomes speech, display or
memory, which also retires the stage-direction pause: a word is spoken, a
stage direction is spoken as its words. Needs a restart — Craig's second
report of "*done*" at 06:00 was on the un-restarted process.

### One misheard word cost three turns
**Seen** 2026-09-21 05:23-05:24, on `base` Whisper. "run and diagnostic"
(-0.63) -> "did you say...?"; "Run a diagnostic." (-0.63) -> asked AGAIN;
"Yes, yes, I did." confirmed it -> dropped as "not addressed" because the
45s window had lapsed while she was asking. He got through on the fourth
try by saying her name.

**Cause** three: the base threshold at -0.6 sat on top of a correct short
imperative; a correction given in answer to a clarification was itself
re-clarified; and an answer to her own question still needed the wake
word.

**Fixed** base threshold -0.7 (its miss "Runnage" scored -0.73, so it
still catches that); a correction is never re-clarified; an answer to her
clarification counts as addressed, and asking one keeps the window open.
Needs a restart.

### "The conversation list is currently empty"
**Seen** 2026-09-21 02:47. Her last two replies, both deterministic
(the override-code line), were not in the transcript at all.

**Cause** two halves. `core/response_handler.py`'s simple path sent its
audio AFTER `__END__`; the page reveals a clause's text when its audio
starts, so at `__END__` the text was still pending, the bubble read as
blank, and the 2026-09-20 blank-bubble fix removed it — after which the
safety-net flush wrote into a div that no longer existed. Every phrase,
diagnostic and refusal she has spoken since that fix vanished the same
way. Streamed replies were unaffected, which is why it looked
intermittent.

**Fixed** audio now goes inside the envelope, before `__END__`, like the
stream path and `core/voice.say()`; and the page flushes pending text
before deciding a bubble is blank, so it is correct regardless of server
order. Reload the page; restart for the server half.

### "Stop saying hell and my name so much" answered with an override-code demand
**Seen** 2026-09-21 02:46-02:47, twice. "Override code needed in request
to change my bluntness - state it like: 'Change to more polite,
please123'" and "state it now: CHANGEMYMODE123".

**Cause** three, stacked. The personality-set classifier (priority 1)
lists "stop saying X" as a personality change, so it claimed the
sentence and demanded the override code — while the corrections system
built for exactly this sentence (priority 100, no code, escalating) never
saw it; which one handled "stop saying X" was a 7B judgment call. The
composed override-code line invented codes because its registry intent
asked for "a short example of the phrasing". And the single-phrase target
extractor would have recorded "hell and my name" as one phrase.

**Fixed** a correction-shaped utterance with no override code in it is a
correction, deterministically, and falls through to the corrections
system; "set your personality to ...", the reset phrases and anything
said with the code are unchanged. The intent no longer asks for an
example and a composed line for that key is rejected if it contains a
digit or a quoted string ([brackets] are rejected for every key). "X and
my name" now records two corrections, "x" and his name.

### Her beliefs about him in every reply — and in everyone else's
**Seen** 2026-09-21 01:11-02:14. The mechanism behind the "thrilling"
argument below. Also: the block was rendered for every user_id, not only
Craig, so anyone else talking to her had "what you have worked out about
HIM" in their prompt. Component 12 rule 3.

**Cause** wired in at 01:11 on the argument that labelled inference is not
the chlorophyll loop. It is a different loop with the same shape: what she
concludes shapes what she says, what she says shapes what she concludes.

**Fixed** confirmed-only and creator-only. The docstring on
`_form_conclusion` had said this must never be wired in unverified; the
code now matches it. Answered-curiosity block made creator-only with it.

### Questions she asked mid-session, then forgot
**Seen** 2026-09-21 05:47 and 06:10 UTC. Two curiosity questions pushed
into an open session have no memory row; the one delivered at connect
does. This is the "she acted as though I was the one starting the
conversation" fault, surviving in one path.

**Cause** `_active_connections` entries carried no user_id, so the push
path called `say()` without one and the memory write was skipped. The last
empty cell in the table in `core/voice.py`.

**Fixed** user_id recorded on the connection. Needs a restart.

### "No system handled the input." spoken to him
**Seen** 2026-09-21 01:06-01:15, four times ("It's me." -> "No system
handled the input.").

**Cause** a NameError in the LLM system made every turn fall through to
the router's fallback string, which is an internal error message, and it
went to speech like any reply. The NameError was fixed at 01:11; the
string stayed speakable.

**Fixed** the router logs the failure with the input, says nothing for
empty input, and otherwise says a line of hers (`nothing_handled`) that
means "that didn't work, say it again". Needs a restart.

### An argument about who said "thrilling"
**Seen** 2026-09-21 01:58-02:04. She suggested "let's talk about something
thrilling — like optimizing your daily routine". He asked if she thought
that was exciting. Over the next five turns she insisted, with increasing
hostility, that HE had said it: "I clearly said you find optimizing your
daily routine thrilling, not the other way around."

**Cause, first diagnosis (2026-09-21 02:08) — wrong.** That her statement
had fallen out of a five-turn memory window. Checked the next session
against the rows she was actually given: her own line was inside the
window on every one of the four denial turns (#1484: in 3 of 4 rows;
#1486: 4 of 4; #1487: 4 of 4; #1489: 3 of 4). On #1486 the window even
held her own recall dump quoting it back. She could see it.

**Cause, actual.** At 01:11 that night her active conclusions had been
wired into every reply as "what you have worked out about him". Four were
live during the argument — "Craig seems to enjoy pushing my buttons",
"Craig is testing the limits", "Craig is trying to test my boundaries and
patience", "Craig derives enjoyment from provoking a response" — with the
instruction to use them to understand what he is after. Read through that,
a correction is a provocation, and the dismissive personality (already
measured to cost her 3 of 10 false-claim catches) held the line. Then at
02:09 reflection read the argument and concluded #10, "Craig is
deliberately provoking me", with evidence that has the facts backwards,
and revised three older beliefs into more hostile versions of themselves,
every one scoring exactly the 7/10 cutoff. Her own output returning as
context, again — this time as beliefs rather than words. Every conclusion
she had ever formed was about him, and every one said he was testing or
provoking her.

**Fix, first attempt — a no-op.** "Window raised to twelve" changed the
fetch limit and left the `recent[-4:]` slice in place. Nothing reached her
that had not before.

**Fixed (2026-09-21, next session).** A belief of hers reaches her prompt
only once Craig has confirmed it at the Controller (Reasoning tab, new
panel); #7-#10 retracted, reason recorded in `decisions`. The window is
now genuinely 12 turns, bounded at 4000 rendered characters with the
oldest dropped first — see `systems/memory/system.py` for the measurement
and for why the bound is by size (Ollama truncates an over-long prompt
from the front, silently). **Not fixed:** the disposition. Under this
personality and this model she does not concede when she is plainly
wrong. That is the model comparison, next.

### Correction recorded as "stop saying thrilling"
**Seen** 2026-09-21 02:02, in the middle of the argument above.

**Cause** the trigger `\byou said that\b`. He said "Alex, stop. See,
that's where you said that you found it thrilling. I did not. You did." —
quoting her back to settle a dispute, the opposite of correcting her. It
matched, nothing was named, so it fell back to what she repeated, which
was "thrilling".

**Fixed** that trigger removed. It also turned up the reverse gap:
"stop referencing green" — his actual words during the emerald loop — was
not recognised as a correction at all, because "referencing" had been
added to the target extractor and not to the detector in front of it.
Both corrected; seven cases verified. False correction withdrawn, its
decision row kept and annotated.

### Her recall answer showed internal plumbing
**Seen** 2026-09-21 02:00. Asked what she remembered, she listed rows
like `- (unprompted — you spoke first) -> Did you say "mentioned how"?`

**Cause** mine, the same day. `remember_own_utterance()` records her
unprompted turns with a marker in the prompt column, and the recall
module printed rows raw, arrow and all — the same arrow format just
removed from her prompt context for inviting her to continue it.

**Fixed** recall renders as speech (`You: "..." / Me: "..."`) and shows
unprompted turns as "I said, unprompted".

### "Override [1] for a more entertaining response."
**Seen** 2026-09-21 01:56, twice in consecutive replies.
She invented a command syntax that does not exist and offered it to him.

**Cause — and the first diagnosis was wrong.** Blamed self-reflection
re-voicing the phrase. It was not: the bracket was in the *registry
default I wrote*:

    "...say something like 'override code [code] reset your personality'."

`[code]` was meant as "put your real code here" and reads as a literal
template. She said it to him three times (memory #1246, #1248, #1435), it
entered `memory`, the recency window fed it back, and she generalised the
shape into ordinary replies. Reflection only added "clear as urine" on top
— which he had separately complained about back in July.

**Fixed** default rewritten as speech, no brackets; the drifted stored
version deleted; and `_invents_syntax()` now rejects any re-wording that
introduces `[bracketed]` tokens. The existing structural check only
guarded `{curly}` placeholders, because those break `.format()` — square
ones break nothing mechanically and instead teach her a command language
that does not exist.

### "Recent (from 2026-09-21 05:26:09): you testing your same systems repeatedly. -> What's next..."
**Seen** 2026-09-21 01:27. She emitted a MEMORY context line verbatim.

**Cause** not truncation — the prompt measured ~2670 of 4096 tokens with
1400 spare. The context format was `Recent (from <ts>): <prompt> -> <response>`,
a labelled row with an arrow, which reads as a template to continue.
Handed a list of them, she produced another row instead of an answer.

**Fixed** memory now reads as dialogue (`He said: "..." / You answered: "..."`),
and the prompt says explicitly that nothing in her context is ever to be
quoted, repeated or continued. That rule should have been there from the
first context block; there are now six.

### Correction recorded as the phrase "craig"
**Seen** 2026-09-21 05:38, first live use of the correction system.

**Cause** two, stacked. `find_repeated()` reads *her* output and never
looked at *his*, so a correction where he named the target was still
answered by guessing. And what she guessed was his own name, because she
addresses him by it in nearly every reply, making it the most repeated
word she produces.

**Fixed** `named_target()` reads the instruction first and only falls back
to repetition when he named nothing; profile names are excluded from
guessing, while "stop saying my name" still works because him saying it
outranks her inferring it.

### Her own passphrase answered as a command
**Seen** 2026-09-21 00:39. She asked him to say "verify access", he did,
and she ran a diagnostic.

**Cause** three, stacked: the verification transcript was routed into the
pipeline blind, with nothing knowing the words came from her; composing
the prompt fresh invented a passphrase the registry intent never asked
for; and voice matching compares the speaker, not the words, so naming a
phrase bought nothing and cost this.

**Fixed** suppressed when everything heard is already in the prompt she
just spoke; generation for that key now asks for anything in their own
words and quotes nothing.

### "She acted as though I was the one starting the conversation"
**Cause** the single root of three separate-looking faults. Only replies
through the normal path were ever recorded — `add_memory()` was called in
exactly one place. Curiosity questions, verification prompts and
clarifications were sent and forgotten, so her next turn had no record she
had spoken.

**Fixed** `db.remember_own_utterance()`, and later `core/voice.say()`,
which owns recording along with the lock, envelope and playback wait.

---

## Open / watching

### Verification prompts that read as threats
**Seen** 2026-09-21, at connect: "Say something, before I lose my mind
checking your identity." / "Verify your voice, or I'll just pretend you're
not real."

**Cause** `voice_verify_prompt` is composed fresh under the dismissive
personality, and the "this one is about security, keep it serious"
instruction is not holding against it. Same class as the July "hello,
party animal" drift.

**Not fixed, deliberately.** Craig ruled out a hard exclusion in July ("I
don't want to force a mood"). The shape that respects that is a check
after composing — keep the line only if it still reads as serious — which
is the skeptic pass applied to a phrase. His call.

### The status check fires on things that are not status checks
**Seen** 2026-09-20/21. Of 15 canned "All core systems online" replies in
24 hours, at least 7 were not requests: "i'm testing functionality", "just
running some tests", "i turned it off for testing" (his answer to her own
question), "you asked", "seems like i'm already verified", "you testing
your same systems repeatedly", "things are working, but not behaving in
the way that i would expect".

**Cause** category 4 of the shared intent prompt is written to fire "in
ANY phrasing, including short/casual ones", and it does. Each hit replaces
her voice with a report.

**Not fixed yet.** Changing that prompt has regressed the classifier twice
before; the 15 real triggers above are the start of the eval set it needs
first.

### Curiosity: asked twice, answered once, kept nothing
**Seen** 2026-09-21. Six questions on file, none with a recorded answer,
including "why did you create me?", which he answered thirty seconds later
("for the hell of it, to see how cool of an AI I can make"). The same
question was asked again 23 minutes later under a different topic string.

**Cause** not yet found for the missing capture (the log for that window
was pruned). The repeat is the exact-match topic dedupe, as the code
predicted.

**Watching.**

### "Hey Craig, life been sucksauce? Got any plans to ditch the singles scene?"
**Seen** written at 02:09 as the new `greeting_returning_user`; he hears it
at his next login.

**Cause** phrase re-voicing after a personality change, unchecked.

**Not fixed.** Resettable at the Controller. Same check-after-rewording
shape as the verification prompt above.

### Logs vanish before the incident is read
**Seen** 2026-09-21. Five of the night's seven log files are empty; the
01:06 incident's log is gone.

**Cause** a new file per process start and a five-file cap, with restarts
every few minutes.

**Not fixed.** Prune by age or size, not count.

### Curiosity questions drawn from debugging
Four of the six questions she has ever asked are about her own faults
("why the system didn't handle your input", "the status of the core
systems"). Not a bug — her curiosity reads the conversation she has had,
and that has been almost entirely diagnostic. Craig's own observation:
"I fear my interactions right now are all diagnostically based so I'm not
really using her as intended." Expect this to change on its own once she
is used normally, and revisit if it does not.

### Speaker gate dropping borderline audio
One recorded drop at similarity 0.57 against a 0.60 threshold. His
enrolled samples score 0.69-0.82, but those are clean and short live
utterances score lower, so a drop this close to the line may have been
him. If he reports her ignoring him, lower the threshold to ~0.52 before
looking anywhere else.
