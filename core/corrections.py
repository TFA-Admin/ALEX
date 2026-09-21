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
    r"\bstop saying\b", r"\bdon'?t say\b", r"\bquit saying\b",
    r"\bstop with\b", r"\bstop repeating\b", r"\byou keep saying\b",
    r"\byou said that\b", r"\bstop that\b", r"\bdrop that\b",
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
