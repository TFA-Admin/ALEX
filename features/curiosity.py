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
import time

from features.base import Feature as Base


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
        if event != "turn":
            return
        session, user_id, user_input = kw.get("session"), kw.get("user_id", ""), kw.get("text", "")
        if session is None:
            return
        from db.db import answer_curiosity_question, record_decision
        from core import mood
        from config.logger_config import logger
        # Craig: "does the curiosity queue retain my answers?" It did not.
        # Positional, like the awareness system's reason capture: the turn
        # straight after her question is the answer often enough. Requires
        # something substantial — "yeah" is acknowledgement, not an answer.
        awaiting_topic = session.pop("awaiting_curiosity_answer", None)
        # 2026-09-25: if he was already talking before she finished asking,
        # he was answering what came before, not her question. Keep waiting.
        if awaiting_topic and time.time() < session.get("curiosity_asked_until", 0):
            logger.info(f"[ACTION] Curiosity about {awaiting_topic!r}: he spoke before she finished asking — not the answer, still waiting")
            session["awaiting_curiosity_answer"] = awaiting_topic
            awaiting_topic = None
        if awaiting_topic and len(user_input.split()) >= 4:
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

    # ---- the prompt: bring an old question back on topic -----------------
    async def prompt_block(self, user_id: str = "", session=None, text: str = "", **kw) -> str:
        if session is None or session.get("awaiting_curiosity_answer"):
            return ""
        from config.logger_config import logger
        try:
            from db.db import fetch_relevant_curiosity, mark_curiosity_question_asked
            rel = await fetch_relevant_curiosity(user_id, text)
        except Exception:
            rel = None
        if not rel:
            return ""
        before = ("you asked before and got no answer" if rel.get("delivered") else "you never got to ask")
        session["awaiting_curiosity_answer"] = rel["topic"]
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
