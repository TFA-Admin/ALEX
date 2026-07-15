import re
import ast
import asyncio
from llm.ollama_client import ollama_manager
from module_runtime.validator import check_safety
from config.logger_config import logger


MAX_RETRIES = 2
MAX_CYCLES = 3


# =========================
# 🚀 MAIN LOOP
# =========================
async def generate_module_code(name, description, model_override=None):

    # logger.info (not print) so this shows up in the Controller's A.L.E.X.
    # tab — a build genuinely takes a couple of minutes (it swaps between
    # two different Ollama models across up to 3 cycles), and there was
    # previously zero visibility into whether it was progressing or stuck.
    logger.info(f"[ACTION] Module generation starting for '{name}'")

    best_code = None
    best_score = -1

    for cycle in range(1, MAX_CYCLES + 1):

        logger.info(f"[ACTION] Module '{name}': generation cycle {cycle}/{MAX_CYCLES}")

        code = await generate_once(name)

        if not code:
            logger.info(f"[ACTION] Module '{name}': cycle {cycle} produced no code, skipping")
            continue

        code = clean_pipeline(code)

        score = score_module_quality(code, name)
        logger.info(f"[ACTION] Module '{name}': cycle {cycle} score {score}")

        if score > best_score:
            best_score = score
            best_code = code

        # ✅ Only validate when meaningful
        exec_error = None

        if score >= 6:

            if not is_syntax_valid(code):
                logger.info(f"[ACTION] Module '{name}': syntax failed, repairing")
                code = await repair_code(code)

            code = clean_pipeline(code)

            if code and is_syntax_valid(code):
                valid, _ = validate_module_code(code)

                if valid:
                    # Structural checks (parses, has init/handle) passing
                    # isn't enough on its own — confirmed live that an
                    # empty-but-structurally-plausible result can get this
                    # far. Actually run it before accepting.
                    ok, exec_error = execution_test(code)

                    if ok:
                        logger.info(f"[ACTION] Module '{name}': accepted on cycle {cycle} (execution-tested)")
                        return code

                    logger.info(f"[ACTION] Module '{name}': cycle {cycle} failed execution test: {exec_error}")

        logger.info(f"[ACTION] Module '{name}': refining cycle {cycle}")
        refined = await refine_code(code, name, error=exec_error)

        if not refined:
            logger.info(f"[ACTION] Module '{name}': refinement failed on cycle {cycle}")
            continue

        refined = clean_pipeline(refined)

        score = score_module_quality(refined, name)
        logger.info(f"[ACTION] Module '{name}': post-refine score {score}")

        if score > best_score:
            best_score = score
            best_code = refined

    logger.info(f"[ACTION] Module '{name}': no cycle produced a fully valid result, using best attempt")

    if best_code:
        best_code = clean_pipeline(best_code)

        if not is_syntax_valid(best_code):
            logger.info(f"[ACTION] Module '{name}': repairing best attempt")
            best_code = await repair_code(best_code)

        best_code = clean_pipeline(best_code)

        # is_syntax_valid() alone isn't enough here — an empty string (or
        # any fragment with no functions) parses as valid Python syntax,
        # so this used to silently "succeed" with a near-empty module.
        # Confirmed live: a real build reported success and installed a
        # 4-byte file (just blank lines) after all 3 cycles failed to
        # produce a real candidate. Requiring the same execution test the
        # normal path uses means a genuinely empty/broken result correctly
        # falls through to the caller's fallback-template instead.
        if best_code and is_syntax_valid(best_code):
            valid, _ = validate_module_code(best_code)

            if valid:
                ok, exec_error = execution_test(best_code)

                if ok:
                    return best_code

                logger.info(f"[ACTION] Module '{name}': best attempt failed execution test: {exec_error}")
            else:
                logger.info(f"[ACTION] Module '{name}': best attempt has no real init/handle functions")

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

    print("🧠 [Stage 1] Generating")

    response = ""

    async for chunk in ollama_manager.generate_stream(
        base_prompt,
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
async def refine_code(code, name, error=None):

    # raw_mode has no chat template — for real failure feedback (from
    # execution_test, not just the heuristic score) to actually reach the
    # model, it has to be phrased as a code comment ahead of the
    # completion seed, since there's no separate instruction channel here.
    if error:
        prompt = f"""# The code below was tested and failed: {error}
# Fix that specific problem, then continue the module below.
{code}

"""
    else:
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


def execution_test(code):
    """
    Actually RUNS the generated code, rather than just checking it parses
    and has the right function names — that weaker check is exactly what
    let a completely empty module report success (confirmed live: a
    4-byte file with no real code passed every check that existed before
    this). Runs check_safety() first since this executes model output
    that hasn't gone through install_module()'s own safety gate yet.

    Returns (ok, reason) — reason is None on success, otherwise a short,
    concrete description of what actually broke, meant to be fed back
    into a refinement prompt as real signal instead of a guess.
    """
    safe, reason = check_safety(code)
    if not safe:
        return False, f"blocked by sandbox: {reason}"

    namespace = {}
    try:
        exec(code, namespace)
    except Exception as e:
        return False, f"code raised an exception on load: {e}"

    handle_fn = namespace.get("handle")
    if not callable(handle_fn):
        return False, "no callable handle() after running the code"

    # Every generated module is seeded from the same scaffold (see
    # generate_once()'s base_prompt below) that always defines a "start"
    # command — a real module should handle it without crashing.
    try:
        result = handle_fn("start", {})
    except Exception as e:
        return False, f"handle('start', {{}}) raised: {e}"

    if not isinstance(result, tuple) or len(result) != 2:
        return False, f"handle() returned {result!r}, expected a (response, state) tuple"

    response, _ = result
    if not response:
        return False, "handle() ran but returned an empty response"

    return True, None


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