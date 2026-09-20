# core/knowledge_filter.py
"""
Is this exchange worth keeping as knowledge?

2026-09-20 (Craig, after reading the audit): "stop auto-storing entirely —
do so. Can we make her ask if I want something stored instead? But only if
it's real knowledge, I dont want to be asked about everything."

## What went wrong

`learned_knowledge` is the cache of pre-formed answers — the thing that
produced the chlorophyll line. Until today anything that reached the LLM
fallback and looked "factual" was auto-stored, and `_is_factual_question()`
in systems/llm/system.py counts **any message containing "?"** as factual.

Measured, not assumed. 16 entries were auto-stored on 2026-09-20. **14 of
them are conversational**, and two — the capital of France (#526) and what
a paperclip maximizer is (#537) — are genuine knowledge that happened to
fall through the same hole. The 14:

  #525  "and what green screens?"
        -> "You were going on and on about how green everything is"
  #530-532  him asking why she keeps referencing old data, and her
        inventing justifications for it

#525 stored **her own hallucination as a fact about the past**, and #532
was retrieved and replayed at similarity 0.91 twenty minutes later when he
asked the same exasperated question again. That is the whole confabulation
loop in two rows: she invents something, it is filed as knowledge, and she
hands it back later with the confidence of a stored fact.

By contrast every entry that arrived through the approved research path
(systems/inquiry/system.py) is real. The problem is not the table, it is
that anything at all was being written to it without a decision. Two good
rows out of sixteen is not a filter, it is an accident.

## Measured against those same 16

The 14 conversational entries are all rejected, 10 of them deterministically
before any LLM call. Both genuine entries are offered. The one real cost is
#526: "ok and what is the capital of France?" strips to "and what is..." and
is dropped as a fragment, because nothing deterministic separates it from
#525's "and what green screens?" — the same three words open a real question
and a confabulated one. That is a known, accepted miss, not an oversight.

## The filter

Two stages, cheapest first, and both stages only ever say NO — nothing here
can cause something to be stored, only to be offered.

1. `looks_durable()` — deterministic, no LLM call, rejects the whole class
   of conversational turns. The single strongest signal is **person
   reference**: real world knowledge ("what is a bomb calorimeter") needs
   no pronouns, while every junk entry above is about him, her, or the
   conversation itself. Sentence fragments ("and what green screens?") are
   continuations of something else and never stand alone as knowledge.

2. `is_worth_keeping()` — one short binary LLM call on whatever survives.
   Deliberately a **separate, single-purpose prompt** rather than another
   category on the shared classifier: this file's neighbours record what
   happened the last two times a category was added to
   `classify_intent()` (total collapse) and the last time a second job was
   folded into `classify_personality_set()` (false positives on unrelated
   messages). Binary, short, positively phrased — see WORTH_KEEPING_PROMPT
   for why the phrasing matters.

It runs in `after_response()`, i.e. after `__END__` has already been sent,
so it costs the user nothing in perceived latency.

**Honest about what this is not:** the filter decides what he gets *asked*
about. It is tuned to be roughly right, not exactly right, because the
consequence of a false positive is one question and the consequence of a
false negative is one unremembered answer. Neither is the old failure mode,
which was silent storage of things nobody would ever have approved.
"""
import re

from llm.ollama_client import ollama_manager
from config.logger_config import logger

# Whole-word person references. Any of these in the QUESTION means it is
# about one of us rather than about the world — "why is white your favorite
# colour", "what did i say two interactions ago", "why are you referencing
# old data". All three were auto-stored today.
_PERSON_WORDS = {
    "i", "im", "ive", "id", "me", "my", "mine", "myself",
    "we", "us", "our", "ours", "ourselves",
    "you", "youre", "youve", "your", "yours", "yourself",
    "alex",
}

# A question opening with a conjunction is a continuation of the previous
# turn; its meaning lives in what came before, so it cannot be a standalone
# knowledge entry no matter what the answer says. "and what green screens?"
# — entry #525 — is exactly this shape.
_FRAGMENT_OPENERS = ("and ", "but ", "so ", "or ", "then ", "also ", "plus ")

# Phrasings that are grammatically questions but conversationally a nudge.
# Swearing is NOT one of these. "what the fuck is a paperclip maximizer?"
# is a real question asked in his voice, and an earlier version of this
# list rejected it on "what the fuck" while the actually-conversational
# "what the hell are you talking about?" was already caught by the pronoun
# check. Filtering on register rather than content threw away the one
# genuine knowledge question in the whole junk set.
_CONVERSATIONAL_MARKERS = (
    "what about", "how about", "what do you think", "what's that",
    "who cares", "says who",
    "what did", "what were", "what was that", "why did you", "why are you",
)

# Her own non-answers. Storing "I'm not sure" as knowledge is worse than
# storing nothing, because a retrieval will later replay the non-answer in
# place of actually trying.
# "I don't have that stored, but generally..." is deliberately NOT here.
# That disclaimer is REQUIRED of her by the system prompt on any factual
# claim she is generating rather than retrieving, so it prefixes almost
# every genuine knowledge answer she gives — "I don't have that stored,
# but generally, the capital of France is Paris." Treating it as a
# non-answer rejected nearly everything this filter exists to catch.
_NON_ANSWER_MARKERS = (
    "i don't know", "i dont know", "i'm not sure", "im not sure",
    "i can't", "i cannot", "no idea", "not certain",
    "could you provide more context", "what do you mean",
    "i don't have access", "i only have",
)

# Below this an "answer" is an acknowledgement, not an explanation.
# Starting point, not tuned — the shortest genuinely useful answer in the
# four research-derived entries currently in the table is 96 characters.
MIN_ANSWER_CHARS = 60


def _words(text: str) -> set:
    return set(re.findall(r"[a-z]+", text.lower().replace("'", "")))


def looks_durable(question: str, answer: str) -> bool:
    """Deterministic rejects only. True means 'worth spending an LLM call
    on', never 'worth storing'."""
    if not question or not answer:
        return False

    q = question.strip().lower()
    a = answer.strip().lower()

    # Leading discourse markers are noise, not content — "ok and what is
    # the capital of France?" is the same question without them, and
    # leaving them on made the fragment test below fire on the "and".
    q = re.sub(r"^(ok|okay|so|well|alright|right|now|hey|alex)\b[\s,]*", "", q).strip()

    if len(answer.strip()) < MIN_ANSWER_CHARS:
        return False

    if q.startswith(_FRAGMENT_OPENERS):
        return False

    if _words(q) & _PERSON_WORDS:
        return False

    if any(m in q for m in _CONVERSATIONAL_MARKERS):
        return False

    if any(m in a for m in _NON_ANSWER_MARKERS):
        return False

    return True


# Positively phrased on purpose. The curiosity prompt in
# core/self_reflection.py ended with "Only say yes if it's a real, nameable
# topic — not vague curiosity" and produced **zero** rows in two months;
# this project's own tuning notes say qwen2.5 takes exclusion clauses
# literally and gets steadily more conservative as you add them. So the bar
# here is stated as a single positive test rather than a list of things not
# to say yes to.
WORTH_KEEPING_PROMPT = """Question asked: "{question}"
Answer given: "{answer}"

Would that answer still be true and useful to someone a year from now, as a general piece of knowledge, independently of the conversation it came from?

Respond with ONLY a JSON object:
{{"durable": true}} or {{"durable": false}}"""


async def is_worth_keeping(question: str, answer: str) -> bool:
    """Full check: deterministic rejects, then one binary LLM call.

    Fails CLOSED — any error, timeout or unparseable reply means "don't
    ask". A missed offer is invisible; a spurious one is a question he
    explicitly said he did not want."""
    if not looks_durable(question, answer):
        return False

    try:
        result = await ollama_manager.generate_json(
            WORTH_KEEPING_PROMPT.format(question=question[:500], answer=answer[:1500]),
            timeout=20.0,
            temperature=0,
        )
    except Exception as e:
        logger.warning(f"⚠️ knowledge durability check failed: {e}")
        return False

    return bool(result and result.get("durable") is True)
