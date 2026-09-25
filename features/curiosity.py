# features/curiosity.py
"""
Her questions (curiosity_queue, written by reflection): asked in a lull
(core/proactive.py), brought back when he touches the topic, his next
words kept as the answer; a question has a week to be asked and two to be
recalled, then it is let go (db/db.py). This feature owns the mid-session
delivery tick, the recall block, and the capture of his answer. Off: no
unprompted questions, no recall, nothing captured. Reflection still
writes questions; they wait.
"""
import re
import time

from features.base import Feature as Base

# 2026-09-25 (Craig: "we also need a way for someone to ask her to
# elaborate on a question with[out] that statement being recorded as the
# answer itself"). Live at 13:02: "what do you mean?" was stored as his
# answer. These are requests to say more, and a short question back is
# one too; she restates the question and keeps waiting.
ELABORATE_RE = re.compile(
    r"^\W*(?:alex[,!.]?\s*)?(?:"
    r"what\W*$|huh\W*$|sorry\W*$|pardon(?: me)?\W*$|come again|say (?:that|it) again|"
    r"repeat (?:that|the question|yourself)|what do you mean|what are you (?:asking|talking about|referring to)|"
    r"what question|which (?:question|one)|"
    r"(?:can|could|would) you (?:please )?(?:elaborate|clarify|explain|rephrase|repeat|say more|be more specific)|"
    r"(?:please )?(?:elaborate|clarify|rephrase)|explain (?:that|the question|what you mean|yourself)|"
    r"i(?:'m| am) not sure what you(?:'re| are) (?:asking|referring to|talking about|getting at)|"
    r"i don'?t (?:understand|follow|know what you mean|get it)"
    r")", re.I)
SHORT_QUESTION_WORDS = 8        # a question back this short is not an answer
REPEAT_MINUTES = 60             # his recent words, for spotting a repeat
REPEAT_SHARED = 5               # content words in common, and
REPEAT_CONTAINMENT = 0.6        # this share of the shorter statement's content words, to call it one
ANSWER_LEAD_S = 4.0             # he may begin answering this early, before her question ends


def is_elaboration_request(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if ELABORATE_RE.search(t):
        return True
    return t.endswith("?") and len(t.split()) < SHORT_QUESTION_WORDS


def is_repeat(text: str, recent_prompts: list) -> bool:
    """2026-09-25 (Craig: "she took my old statement and used it to answer
    the red wall question"): he restated, word for word nearly, what he
    had said seven minutes earlier — an answer to an earlier question of
    hers that had been discarded — and the capture took it for the red
    wall. Something he has said before is not an answer to a new
    question. Content-word overlap, not string equality: the two were
    "I made you to be my personal assistant..." and "alex, i created you
    to be my personal assistant..." — 8 content words in common, 62% of
    the shorter one (measured; a Jaccard of 0.42 would have missed it)."""
    from db.db import _topic_words
    now_words = _topic_words(text or "")
    if len(now_words) < REPEAT_SHARED:
        return False
    for p in recent_prompts or []:
        then = _topic_words(p or "")
        if not then:
            continue
        shared = len(now_words & then)
        if shared >= REPEAT_SHARED and shared / min(len(now_words), len(then)) >= REPEAT_CONTAINMENT:
            return True
    return False


class Feature(Base):
    name = "curiosity"
    summary = ("Her questions: asked in a lull, brought back when he touches the topic, his next words kept "
               "as the answer; old questions are let go (core/proactive.py, db).")
    owns = ("core.proactive",)
    order = 50
    tick_every_s = 60.0
    first_tick_after_s = 60.0

    async def tick(self):
        from core import proactive
        await proactive._check_curiosity_delivery()

    # ---- the turn: was this his answer? ----------------------------------
    async def on(self, event: str, **kw):
        if event == "reply":
            await self.on_reply(kw.get("session"), bool(kw.get("interrupted")))
            return
        if event != "turn":
            return
        session, user_id, user_input = kw.get("session"), kw.get("user_id", ""), kw.get("text", "")
        if session is None:
            return
        from db.db import answer_curiosity_question, record_decision, fetch_recent_memory
        from core import mood
        from config.logger_config import logger
        # Craig: "does the curiosity queue retain my answers?" It did not.
        # Positional, like the awareness system's reason capture: the turn
        # straight after her question is the answer often enough. Requires
        # something substantial — "yeah" is acknowledgement, not an answer.
        awaiting_topic = session.pop("awaiting_curiosity_answer", None)
        if not awaiting_topic:
            return
        # 2026-09-25: if he STARTED speaking before she had finished asking
        # (less a moment's lead), he was answering what came before, not
        # her question. Keep waiting. The start comes from the page's
        # __SPEAKING__ (ws/ws_handlers.py); without it, the old rule.
        until = float(session.get("curiosity_asked_until") or 0)
        started = session.get("utterance_started_at")
        spoke_early = (float(started) < until - ANSWER_LEAD_S) if started else (time.time() < until)
        if spoke_early:
            logger.info(f"[ACTION] Curiosity about {awaiting_topic!r}: he started speaking before she finished asking — not the answer, still waiting")
            session["awaiting_curiosity_answer"] = awaiting_topic
            return
        # a request to say more: she restates the question (prompt_block)
        if is_elaboration_request(user_input):
            logger.info(f"[ACTION] Curiosity about {awaiting_topic!r}: he asked her to say more ({user_input[:60]!r}) — she restates it, still waiting")
            session["awaiting_curiosity_answer"] = awaiting_topic
            session["curiosity_elaborate"] = awaiting_topic
            return
        # something he has said before is not an answer to this
        try:
            recent = await fetch_recent_memory(user_id, limit=12)
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - REPEAT_MINUTES * 60))
            prompts = [r["prompt"] for r in recent
                       if str(r.get("created_at") or "") >= cutoff and not (r.get("prompt") or "").startswith("(unprompted")]
        except Exception:
            prompts = []
        if is_repeat(user_input, prompts):
            logger.info(f"[ACTION] Curiosity about {awaiting_topic!r}: he repeated something he said in the last hour — not the answer, still waiting")
            session["awaiting_curiosity_answer"] = awaiting_topic
            return
        words = len(user_input.split())
        if awaiting_topic and words >= 4:
            try:
                if await answer_curiosity_question(awaiting_topic, user_input):
                    await record_decision(
                        "curiosity",
                        f"Got an answer about {awaiting_topic}",
                        reasoning="She asked, and this is what he said back — his words, not something she worked out.",
                        evidence=user_input[:300],
                        outcome="kept, and she will not ask again",
                        actor=user_id)
                    logger.info(f"[ACTION] Curiosity about {awaiting_topic!r} answered: {user_input[:80]!r}")
                    await mood.note("curiosity_answered", who=user_id)
            except Exception as e:
                logger.warning(f"⚠️ could not keep the answer: {e}")
        elif awaiting_topic:
            # Too short to be an answer — keep waiting one more turn.
            session["awaiting_curiosity_answer"] = awaiting_topic

    # ---- the reply: when did her question finish playing? ------------------
    async def on_reply(self, session, interrupted: bool):
        if session is None or not session.pop("curiosity_asked_in_reply", False):
            return
        if interrupted:
            # cut off — the question may never have been said; drop the wait
            session.pop("awaiting_curiosity_answer", None)
            return
        # core/response_handler.py sets last_addressed_at to the END of her
        # playback just before this event; his answer starts after that
        session["curiosity_asked_until"] = float(session.get("last_addressed_at") or time.time())

    # ---- the prompt: restate a question, or bring an old one back on topic --
    async def prompt_block(self, user_id: str = "", session=None, text: str = "", **kw) -> str:
        if session is None:
            return ""
        from config.logger_config import logger
        topic = session.pop("curiosity_elaborate", None)
        if topic:
            try:
                from db.db import get_curiosity_question
                question = await get_curiosity_question(topic)
            except Exception:
                question = None
            if question:
                return (f"\n\n    HE IS ASKING WHAT YOU MEANT BY YOUR QUESTION: \"{question}\" Put it more plainly, "
                        "in your own words, in one or two sentences, and give him room to answer. Do not ask a "
                        "different question and do not answer it for him.")
        if session.get("awaiting_curiosity_answer"):
            return ""
        try:
            from db.db import fetch_relevant_curiosity, mark_curiosity_question_asked
            rel = await fetch_relevant_curiosity(user_id, text)
        except Exception:
            rel = None
        if not rel:
            return ""
        before = ("you asked before and got no answer" if rel.get("delivered") else "you never got to ask")
        session["awaiting_curiosity_answer"] = rel["topic"]
        session["curiosity_asked_in_reply"] = True         # on_reply sets curiosity_asked_until
        try:
            await mark_curiosity_question_asked(rel["topic"])
        except Exception:
            pass
        logger.info(f"[CURIOSITY] the topic {rel['topic']!r} came up — bringing her question back")
        return (f"\n\n    HE HAS JUST TOUCHED ON SOMETHING YOU WANTED TO KNOW ({before}): "
                f"\"{rel['question']}\" If it fits, ask it now in your own words, after your answer.")

    async def diagnose(self):
        from core.self_reflection import get_creator_name
        from db.db import fetch_undelivered_curiosity_questions
        who = (await get_creator_name() or "craig").lower()
        waiting = await fetch_undelivered_curiosity_questions(user=who, creator=True)
        return True, f"{len(waiting)} question(s) waiting to be asked"

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Curiosity — " + msg
