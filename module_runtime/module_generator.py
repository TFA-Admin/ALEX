import re
import ast
import asyncio
from llm.ollama_client import ollama_manager


MAX_RETRIES = 2
MAX_CYCLES = 3


# =========================
# 🚀 MAIN LOOP
# =========================
async def generate_module_code(name, description, model_override=None):

    print("🧠 [SYSTEM] Starting module generation loop")

    best_code = None
    best_score = -1

    for cycle in range(1, MAX_CYCLES + 1):

        print(f"\n🔁 GENERATION CYCLE {cycle}/{MAX_CYCLES}")

        code = await generate_once(name)

        if not code:
            print("⚠️ No code generated — skipping")
            continue

        code = clean_pipeline(code)

        score = score_module_quality(code, name)
        print(f"🧠 Score: {score}")

        if score > best_score:
            best_score = score
            best_code = code

        # ✅ Only validate when meaningful
        if score >= 6:

            if not is_syntax_valid(code):
                print("⚠️ Syntax failed — repairing")
                code = await repair_code(code)

            code = clean_pipeline(code)

            if code and is_syntax_valid(code):
                valid, _ = validate_module_code(code)
                if valid:
                    print("✅ Final module accepted")
                    return code

        print("⚠️ Refining...")
        refined = await refine_code(code, name)

        if not refined:
            print("⚠️ Refinement failed")
            continue

        refined = clean_pipeline(refined)

        score = score_module_quality(refined, name)
        print(f"🧠 Post-refine score: {score}")

        if score > best_score:
            best_score = score
            best_code = refined

    print("⚠️ Returning best attempt")

    if best_code:
        best_code = clean_pipeline(best_code)

        if not is_syntax_valid(best_code):
            print("⚠️ Repairing best attempt...")
            best_code = await repair_code(best_code)

        best_code = clean_pipeline(best_code)

        if best_code and is_syntax_valid(best_code):
            return best_code

    return None


# =========================
# 🧱 GENERATION
# =========================
async def generate_once(name):

    base_prompt = f"""def init():
    return "{name} module ready"

def handle(command, state):
    if state is None:
        state = {{}}

    if command == "start":
        state["status"] = "started"
        return "Started", state

"""

    print("🧠 [Stage 1] Mistral")

    response = ""

    async for chunk in ollama_manager.generate_stream(
        base_prompt,
        model_override="mistral",
        raw_mode=True
    ):
        response += chunk

    code = extract_code(normalize_output(response))

    if not code:
        return None

    code = fix_signature(code)

    # =========================
    # 🧠 DEEPSEEK CONTINUATION
    # =========================
    print("🧠 [Stage 2] DeepSeek continuation")

    refine_prompt = f"""{code}

"""

    refined = ""

    start_time = asyncio.get_event_loop().time()
    last_token_time = start_time

    STALL_TIMEOUT = 10
    MAX_TOTAL = 60

    try:
        async for chunk in ollama_manager.generate_stream(
            refine_prompt,
            model_override="deepseek-coder:6.7b",
            raw_mode=True
        ):
            refined += chunk

            now = asyncio.get_event_loop().time()

            # update only when token arrives
            last_token_time = now

            if now - start_time > MAX_TOTAL:
                print("⚠️ Max generation time reached")
                break

            # true stall detection
            if now - last_token_time > STALL_TIMEOUT:
                print("⚠️ DeepSeek stalled")
                break

    except Exception as e:
        print("⚠️ DeepSeek error:", e)

    refined = strip_explanations(remove_invalid_tokens(refined))

    if not refined or len(refined.strip()) < 5:
        print("⚠️ DeepSeek empty — using base")
        return code

    return code + "\n" + refined


# =========================
# 🔁 REFINEMENT
# =========================
async def refine_code(code, name):

    prompt = f"""{code}

"""

    refined = ""

    async for chunk in ollama_manager.generate_stream(
        prompt,
        model_override="deepseek-coder:6.7b",
        raw_mode=True
    ):
        refined += chunk

    refined = strip_explanations(remove_invalid_tokens(refined))

    return extract_code(normalize_output(refined))


# =========================
# 🧠 DOMAIN DETECTION
# =========================
def detect_domain_features(code, target):

    c = code.lower()
    target = target.lower()

    domain_signals = {
        "game": ["board", "move", "turn", "win", "player"],
        "tictactoe": ["board", "x", "o", "win", "grid"],
        "chess": ["board", "move", "piece", "king"],
        "checkers": ["jump", "capture", "king", "board"],
        "calculator": ["add", "subtract", "multiply", "divide"],
        "song": ["lyrics", "verse", "chorus"]
    }

    keywords = domain_signals.get(target, domain_signals["game"])

    return sum(1 for k in keywords if k in c)


# =========================
# 🧠 SCORING
# =========================
def score_module_quality(code, target):

    if not code:
        return 0

    c = code.lower()
    score = 0

    # structure
    if "def init" in c: score += 1
    if "def handle" in c: score += 1
    if "return" in c: score += 1
    if "state" in c: score += 1

    # logic
    logic = c.count("if ") + c.count("elif ")
    score += min(3, logic)

    # command parsing
    if "split" in c or "startswith" in c:
        score += 2

    # 🔥 domain enforcement
    domain_hits = detect_domain_features(code, target)

    if domain_hits == 0:
        print("❌ No domain logic detected")
        return 0

    score += domain_hits

    # penalties
    if "parts" in c and "split" not in c:
        score -= 3

    if "<" in c or ">" in c:
        score -= 3

    return score


# =========================
# 🔥 SYNTAX
# =========================
def is_syntax_valid(code):
    try:
        ast.parse(code)
        return True
    except Exception as e:
        print("❌ Syntax:", e)
        return False


async def repair_code(code):

    prompt = f"""Fix this Python code. Only return valid Python.

{code}
"""

    fixed = ""

    async for chunk in ollama_manager.generate_stream(
        prompt,
        model_override="deepseek-coder:6.7b",
        raw_mode=True
    ):
        fixed += chunk

    return extract_code(normalize_output(strip_explanations(fixed)))


# =========================
# VALIDATION
# =========================
def validate_module_code(code):
    try:
        tree = ast.parse(code)

        has_init = any(isinstance(n, ast.FunctionDef) and n.name == "init" for n in tree.body)
        has_handle = any(isinstance(n, ast.FunctionDef) and n.name == "handle" for n in tree.body)

        return has_init and has_handle, None

    except Exception as e:
        return False, str(e)


# =========================
# CLEANING PIPELINE
# =========================
def clean_pipeline(code):
    code = enforce_single_module_structure(code)
    code = fix_signature(code)
    code = fix_common_runtime_issues(code)
    code = remove_invalid_tokens(code)
    return code


# =========================
# CLEANERS
# =========================
def normalize_output(text):
    text = re.sub(r"```.*?\n", "", text)
    text = text.replace("```", "")
    return text.strip()


def extract_code(text):
    if not text:
        return None

    start = text.find("def init")
    if start != -1:
        return text[start:]

    if "def " in text:
        return text

    return None


def strip_explanations(text):
    if not text:
        return ""

    return "\n".join(
        l for l in text.splitlines()
        if l.strip().startswith((
            "def", "class", "if", "elif", "else",
            "for", "while", "return", "import",
            "from", "state", "response", "pass"
        ))
    )


def remove_invalid_tokens(text):
    text = re.sub(r"<.*?>", "", text)
    text = re.sub(r"[^\x00-\x7F]+", "", text)
    return text


def fix_signature(code):
    return re.sub(
        r"def\s+handle\s*\(\s*command\s*,\s*state\s*=\s*None\s*\)",
        "def handle(command, state)",
        code
    )


# =========================
# STRUCTURE
# =========================
def enforce_single_module_structure(code):

    if not code:
        return code

    parts = re.split(r"(def init|def handle)", code)

    init_block = ""
    handle_block = ""

    for i in range(len(parts)):
        if parts[i] == "def init":
            init_block = "def init" + parts[i + 1]
        elif parts[i] == "def handle":
            handle_block = "def handle" + parts[i + 1]

    return init_block.strip() + "\n\n" + handle_block.strip()


# =========================
# FIXES
# =========================
def fix_common_runtime_issues(code):

    if not code:
        return code

    if "startswith(\"move\")" in code and "split" not in code:
        code = code.replace(
            'elif command.startswith("move"):',
            '''elif command.startswith("move"):
        parts = command.split()
        if len(parts) < 3:
            return "Invalid move", state
        from_value = parts[1]
        to_value = parts[2]'''
        )

    return code