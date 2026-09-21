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

**The pattern so far:** most of these are not independent bugs. They are
her own output coming back to her as context and reproducing itself. The
seed differs — a hallucination, a bad stored phrase, a default I wrote —
but the loop is the same every time, because the last four turns of
conversation go into every prompt. Anything she says once, she is more
likely to say again.

---

## Closed

### An argument about who said "thrilling"
**Seen** 2026-09-21 01:58-02:04. She suggested "let's talk about something
thrilling — like optimizing your daily routine". He asked if she thought
that was exciting. Over the next five turns she insisted, with increasing
hostility, that HE had said it: "I clearly said you find optimizing your
daily routine thrilling, not the other way around."

**Cause** not dishonesty and not confabulation from nothing. The
conversational memory window was five turns — roughly four minutes of
speech — and by the time he quoted her back, her own statement had fallen
out of it. She could not see it. A personality tuned never to back down
filled the gap with confidence instead of saying "I can't see that far
back", which her prompt does have a rule for, but only for when she is
*asked* about the past rather than contradicted about it.

This is the most corrosive failure she has: confidently telling him he is
wrong about something he can plainly remember.

**Fixed** window raised to twelve turns. Costs ~250 tokens against 1400
spare. **Not** fixed: the underlying disposition. Twelve turns only moves
the edge; an argument that outlasts it will do the same thing, and the
real fix is her saying "I can't see that far back" when she is
contradicted about something outside her window. Worth doing once she has
more than recency to go on.

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
