# core/corrections.py
"""
"Stop saying that" — worked out, remembered, and escalating.

2026-09-20 (Craig, rejecting a banned-phrase list): "I don't want banned
phrases per say I just want her to listen... In my mind she would be
building a profile of me and know me. so if I say something like stop
saying that, she would review what she said for duplication and be able to
identify it."

And on how it should behave over time: "it should be considered a
temporary correction, however if she starts saying it again and I shut it
down again she should be interpretting that as a flatout correction and
become less likely she uses it. It's a simple disciplinary correction, but
keep doing it and the impact is worse."

And on whose corrections count: "the permanent changes should be unique to
me. if talking to someone else she can choose to listen to them or not."

## The three pieces

1. **She works out what he means.** He says "stop saying that" without
   naming anything. `find_repeated()` looks at what she actually just
   said and finds the phrase she has been repeating. Deterministic —
   repetition is a countable property of her own output, not a judgement
   call, and this is the half a language model is worst at and a word
   counter is best at.

2. **It escalates.** First correction is a nudge she can still override in
   context; each repeat of the same thing raises the strength. At the top
   it stops being advice and becomes a filter on the way out. The point is
   that ignoring him costs her something.

3. **His corrections bind; other people's are hers to weigh.** Design
   Principle 9 applied to behaviour rather than permissions. A correction
   from anyone else is recorded with its outcome and her reason, so he can
   see what she decided about whom — not silently dropped and not
   silently obeyed.
"""
import re

_CORRECTION_PATTERNS = (
    # "referencing"/"mentioning"/"bringing up" belong here as well as in
    # _NAMED_TARGET_PATTERNS. 2026-09-20: they were added to the extractor
    # and not to this gate, so "stop referencing green" — Craig's actual
    # words during the emerald loop — was not even recognised AS a
    # correction, and the extractor that could have handled it never ran.
    r"\bstop saying\b", r"\bdon'?t say\b", r"\bquit saying\b",
    r"\bstop (?:referencing|mentioning|bringing up)\b",
    r"\b(?:don'?t|do not|never) (?:reference|mention|bring up)\b",
    r"\bstop with\b", r"\bstop repeating\b", r"\byou keep saying\b",
    # NOT "you said that" — dropped 2026-09-20 after it fired on
    # "Alex, stop. See, that's where you said that you found it thrilling.
    # I did not. You did." He was QUOTING her back to settle a dispute
    # about who said what, and it was recorded as an instruction to stop
    # saying "thrilling". Quoting her is the opposite of correcting her,
    # and it happens constantly in an argument.
    r"\bstop that\b", r"\bdrop that\b",
    r"\benough of\b", r"\bno more\b", r"\bstop it\b",
    # "say deal with it again and there will be repercussions" — his
    # actual words, and the plainest correction in the whole transcript.
    # A threat conditional on repeating it is a correction; the first
    # version of this list read it as ordinary conversation.
    r"\bagain and\b.{0,40}\b(repercussion|consequence|problem|trouble)",
    r"\bif you say\b.{0,40}\bagain\b",
    r"\bdo not say\b", r"\bnever say\b",
)

_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)


def is_correction(text: str) -> bool:
    """Is he telling her to stop doing something she just did?

    Deliberately broad on the trigger and narrow on the target: catching a
    correction he did not mean costs one logged no-op, because
    find_repeated() then finds nothing to act on and nothing is recorded."""
    return bool(text) and bool(_CORRECTION_RE.search(text))


# What he actually said to stop saying, when he said it.
#
# 2026-09-20, and this is the important one. The first live correction
# recorded "craig" — but Craig had NAMED the thing he wanted stopped, in
# the same sentence. His words: "I did say what I wanted her to stop
# saying. And that's the problem. I pointed her at it and she still got it
# wrong."
#
# He was right, and the mistake was structural rather than a bad
# threshold: find_repeated() reads HER output and never once looked at
# HIS. The "work it out from duplication" half was built because he asked
# for it, and then used even when there was nothing to work out. Guessing
# is the fallback, not the method.
#
# Ordered longest-first so "stop saying my name" is tried before the
# generic "stop saying <rest of sentence>".
_NAMED_TARGET_PATTERNS = (
    r"stop calling me (?P<t>.+)",
    r"stop referring to me as (?P<t>.+)",
    r"stop saying my (?P<t>name)",
    r"stop using my (?P<t>name)",
    r"stop (?:saying|using|repeating|referencing|mentioning|bringing up) (?P<t>.+)",
    r"(?:don'?t|do not|never) (?:say|use|repeat) (?P<t>.+)",
    r"quit (?:saying|using|repeating|referencing|mentioning|bringing up) (?P<t>.+)",
    r"(?:don'?t|do not|never) (?:reference|mention|bring up) (?P<t>.+)",
    r"stop with (?:the |that )?(?P<t>.+)",
    r"enough (?:of|with) (?:the |that )?(?P<t>.+)",
    r"no more (?P<t>.+)",
    r"you keep saying (?P<t>.+)",
)

# Trailing scaffolding people add after naming the thing.
_TARGET_TRAILERS = (
    r"\s+(?:so much|so often|all the time|every|each|constantly|again|"
    r"anymore|any more|please|ok|okay|alright|will you|would you|sentence)\b.*$",
)


# What splits "X and Y" into two things he named. Deliberately not "also"
# or "then": those introduce a second instruction, not a second phrase.
_TARGET_SPLIT_RE = re.compile(r"\s*(?:,|\band\b|\bor\b)\s*")

# Placeholders that name nothing — "stop saying that" — and, as the FIRST
# part of a list, mean the rest is an instruction rather than a phrase
# ("stop saying that and move on").
_NAMES_NOTHING = {"that", "it", "this", "those", "these", "stuff", "things",
                  "thing", "", "so much", "shit"}


# A quoted phrase. Single quotes only count with a real boundary on each
# side: 2026-09-21 the previous pattern read the apostrophes in
# "i'm not trying to override you. i'm just trying to correct you. stop
# saying hell so much." as quote marks and recorded the phrase
# "m not trying to override you. i" — his actual words, mangled.
_QUOTED_TARGET_RE = re.compile(
    r'"([^"]{2,60})"'
    r'|\u201c([^\u201d]{2,60})\u201d'
    r"|(?:^|[\s:(])'([^']{2,60})'(?=$|[\s.!?,;:)])"
    r"|(?:^|[\s:(])\u2018([^\u2019]{2,60})\u2019(?=$|[\s.!?,;:)])"
)


def named_targets(text: str, speaker_name: str = None) -> list:
    """Everything he explicitly told her to stop saying, in the order he
    said it, or [] if he named nothing.

    2026-09-21: "stop saying hell and my name so much" names two things,
    and the single-phrase version of this returned one, "hell and my
    name" — a phrase she has never said. Splits on and/or/commas. A quoted
    phrase is never split: quotes are how he says the phrase itself
    contains an "and".

    Quoted text wins outright — "stop saying 'deal with it'" is as
    unambiguous as it gets. Otherwise the phrase after the instruction,
    trimmed of the qualifiers people tack on ("so much", "all the time").

    `speaker_name` resolves "my name", which is a real instruction and
    must work even though names are excluded from automatic detection.
    Him naming it is the whole point — the exclusion exists to stop her
    GUESSING at his name, not to overrule him.
    """
    if not text:
        return []

    lowered = " ".join(text.lower().split())

    quoted = _QUOTED_TARGET_RE.search(text)
    if quoted:
        phrase = next(g for g in quoted.groups() if g)
        return [" ".join(phrase.lower().split())]

    for pattern in _NAMED_TARGET_PATTERNS:
        m = re.search(pattern, lowered)
        if not m:
            continue

        target = m.group("t").strip(" .!?,;:")
        for trailer in _TARGET_TRAILERS:
            target = re.sub(trailer, "", target).strip(" .!?,;:")

        parts = [p.strip(" .!?,;:") for p in _TARGET_SPLIT_RE.split(target)]

        # "stop saying that and ..." — the first thing names nothing, so
        # the whole sentence is one unnamed correction plus an instruction.
        if not parts or parts[0] in _NAMES_NOTHING:
            return []

        found = []
        for part in parts:
            for trailer in _TARGET_TRAILERS:
                part = re.sub(trailer, "", part).strip(" .!?,;:")
            if part in ("name", "my name", "your name"):
                if speaker_name:
                    part = speaker_name.lower()
                else:
                    continue
            if part in _NAMES_NOTHING or not (2 <= len(part) <= 60):
                continue
            if part not in found:
                found.append(part)
        return found

    return []


def named_target(text: str, speaker_name: str = None) -> str:
    """The first thing he named, or "". Kept for callers that want one;
    see named_targets() for the list."""
    found = named_targets(text, speaker_name=speaker_name)
    return found[0] if found else ""


# Words that repeat in ordinary English regardless of any verbal tic, so a
# phrase made only of these is not evidence of anything.
_FUNCTION_ONLY = {
    "the", "a", "an", "and", "or", "but", "if", "so", "to", "of", "in",
    "on", "at", "for", "with", "from", "as", "by", "is", "are", "was",
    "were", "be", "been", "am", "do", "does", "did", "have", "has", "had",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their", "this", "that",
    "these", "those", "what", "when", "where", "who", "why", "how", "not",
    "no", "yes", "can", "will", "would", "should", "could", "just", "now",
}

# 2026-09-20: 1, not 2. Craig said "stop referencing green." during the
# emerald/chlorophyll loop and nothing was listening; with this live it
# would have been caught — except that a bare "green" is one word, and a
# two-word floor would only have found "emerald green" and "green screen"
# while missing the word he actually objected to.
#
# Safe because of what each strength does. At advisory and standing a
# correction is a line in her prompt, where a single word is exactly as
# meaningful as a phrase. Only ENFORCED removes text, and it removes a
# phrase only where it stands as its own sentence — a lone "green." rarely
# is one, so enforcement of a single word is nearly a no-op rather than
# something that shreds her sentences.
MIN_PHRASE_WORDS = 1
MAX_PHRASE_WORDS = 6
MIN_OCCURRENCES = 2


# Things she says constantly that are not tics. 2026-09-20: the first live
# correction recorded **"craig"** — she addresses him by name in nearly
# every reply, so his own name is the most repeated word in her output, and
# "stop saying that" was read as "stop saying my name".
#
# Names are passed in rather than listed here, because whose they are
# depends on who is talking — see the `never` argument.
_NEVER_A_TIC = {
    "alex", "yeah", "okay", "sure", "right", "well", "look", "listen",
    "anyway", "sorry", "thanks", "hey", "hello",
}


def find_repeated(responses, min_occurrences: int = MIN_OCCURRENCES,
                  never=None):
    """The phrase she has been repeating across her recent replies.

    This is the part Craig asked for by name — she reviews what she said
    and finds the duplication herself, rather than being handed a string to
    ban. Returns the longest phrase repeated at least `min_occurrences`
    times, or "" if there is nothing.

    Most repeated wins, with length as the tie-break — see the sort below
    for the measurement that settled that order.

    Counts a phrase once per response, not once per occurrence: saying
    something twice in one breath is a stumble, saying it in three
    consecutive replies is the habit he is objecting to.
    """
    if not responses:
        return ""

    # Her habitual address is not the thing he is objecting to. Anything
    # here is skipped outright, and a multi-word phrase built only from
    # these is skipped too, so "okay craig" cannot sneak through.
    excluded = set(_NEVER_A_TIC) | {
        " ".join(str(n or "").lower().split()) for n in (never or [])
    }
    excluded.discard("")

    seen = {}
    for text in responses:
        words = re.findall(r"[a-z']+", (text or "").lower())
        here = set()
        for size in range(MIN_PHRASE_WORDS, MAX_PHRASE_WORDS + 1):
            for i in range(len(words) - size + 1):
                gram = words[i:i + size]
                if all(w in _FUNCTION_ONLY or w in excluded for w in gram):
                    continue
                here.add(" ".join(gram))
        for phrase in here:
            seen[phrase] = seen.get(phrase, 0) + 1

    candidates = [(p, n) for p, n in seen.items() if n >= min_occurrences]
    if not candidates:
        return ""

    # **Most frequent first, then longest.** Ordering these the other way
    # round was measured wrong against the real transcript: "next time keep
    # it" appeared in 2 of her 4 replies and "deal with it" in all 4, and
    # preferring length picked the incidental phrase over the actual tic.
    #
    # Length still matters as the tie-break, and it has to: a repeated
    # phrase contains its own fragments, so "deal with it" and "with it"
    # both score 4, and without this the answer is a sliver of the real one.
    candidates.sort(key=lambda pn: (pn[1], len(pn[0].split())), reverse=True)
    return candidates[0][0]


# How strength maps to consequence. Craig: "It's a simple disciplinary
# correction, but keep doing it and the impact is worse."
#
#   1  she is told, in context, that he asked her to stop. She can still
#      say it — the point of a first correction is that it is a correction,
#      not a cage.
#   2  the instruction hardens: it is now a standing rule about him.
#   3+ deterministic. It does not reach him regardless of what she
#      generates, the same guarantee strip_emojis() gives.
ADVISORY = 1
STANDING = 2
ENFORCED = 3


def consequence(strength: int) -> str:
    if strength >= ENFORCED:
        return "enforced"
    if strength == STANDING:
        return "standing"
    return "advisory"


def context_line(phrase: str, strength: int) -> str:
    """What she is told about an active correction, in her own prompt.

    Phrased as something he did, not as a rule from nowhere — she should
    know it came from him and how many times, because that is exactly the
    information that makes it a correction rather than a constraint."""
    # 2026-09-20 (Craig: "would she go to far and now just never reference
    # anything green or does she understand the context?"). A fair worry,
    # and the first wording invited exactly that — "do not use it again"
    # reads as a ban on the subject when what he objected to was her
    # inserting it unprompted. Every line now says which of the two it is,
    # because the difference between "stop bringing this up on your own"
    # and "never speak of this" is the whole point of a correction.
    scope = (f'He is not banning the subject — if he raises "{phrase}" '
             f'himself, or it is genuinely what the answer is about, use it '
             f'normally. What he objected to is you working it into replies '
             f'on your own.')

    if strength == ADVISORY:
        return (f'He asked you once to stop saying "{phrase}" — you had been '
                f'repeating it. {scope}')
    if strength == STANDING:
        return (f'He has now asked you TWICE to stop saying "{phrase}". He '
                f'notices, and he is losing patience. {scope}')
    return (f'He has asked you {strength} times to stop saying "{phrase}". '
            f'It is now removed from your replies automatically wherever it '
            f'stands on its own, so leaning on it just means the sentence '
            f'arrives broken. {scope}')
