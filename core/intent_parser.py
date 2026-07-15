import re

ENTITY_PATTERNS = [
    r"profile for ([a-zA-Z ]+)",
    r"who is ([a-zA-Z ]+)",
    r"do you know ([a-zA-Z ]+)",
    r"have you heard of ([a-zA-Z ]+)",
    r"is ([a-zA-Z ]+) in your system",
    r"does ([a-zA-Z ]+) exist",
    r"tell me about ([a-zA-Z ]+)",
    r"what about ([a-zA-Z ]+)",
    r"about ([a-zA-Z ]+)"
]

INVALID_ENTITIES = {"what", "who", "you", "me", "it", "this", "that"}
ENTITY_STOP_WORDS = {
    "why", "what", "who", "when", "where", "how",
    "did", "didnt", "didn't", "does", "dont", "don't",
    "and", "or", "but", "so", "then"
}
COMMON_WORDS = {
    "again", "ask", "asked", "said", "ill", "will", "going", "now",
    "everything", "something", "anything", "nothing",
    "this", "that", "these", "those",
    "have", "from", "your", "about", "know", "what",
    "are", "was", "were", "been", "being",
    "hello", "alex", "tell"
}


def clean_entity(raw: str):
    words = re.findall(r"[a-zA-Z']+", raw.lower())
    cleaned = []

    for word in words:
        if word in ENTITY_STOP_WORDS:
            break
        if word in COMMON_WORDS:
            continue
        cleaned.append(word)

    if not cleaned:
        return None

    return " ".join(cleaned).strip()

def detect_intent(prompt: str):
    p = prompt.lower()

    if any(x in p for x in [
        "who am i",
        "about me",
        "my profile",
        "my age",
        "how old am i"
    ]):
        return "self_profile"

    # 🔍 profile / identity queries
    if any(x in p for x in [
        "who is",
        "profile",
        "do you know",
        "heard of",
        "exist",
        "in your system",
        "tell me about",
        "what about"   # 👈 ADD
    ]):
        return "profile_lookup"

    # 👤 self queries
    if any(x in p for x in [
        "who am i",
        "about me",
        "my profile"
    ]):
        return "self_profile"

    return "general"

def extract_entities(prompt: str):
    found = set()

    lower_text = prompt.lower()

    # ---------------- PATTERN MATCHES ----------------
    for pattern in ENTITY_PATTERNS:
        matches = re.findall(pattern, lower_text)

        for m in matches:
            parts = re.split(r'\band\b|,', m)

            for part in parts:
                part = clean_entity(part)

                if not part or len(part) < 3:
                    continue

                if part in INVALID_ENTITIES:
                    continue

                found.add(part)

    # ---------------- 🔥 FALLBACK (CASE-INSENSITIVE) ----------------
    words = re.findall(r'\b[a-zA-Z]{3,}\b', prompt)

    for w in words:
        clean = w.lower()

        if clean in INVALID_ENTITIES:
            continue

        if clean in COMMON_WORDS:
            continue

        if clean.endswith("s"):
            continue

        # 🔥 ONLY allow if it appears in a "name-like position"
        if not re.search(rf"(who is|know|about)\s+{clean}", prompt.lower()):
            continue

        found.add(clean)

    return sorted(found)
