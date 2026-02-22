import os
import time
import re
from typing import Any, Iterable, Optional, Tuple, List

# Lyra — OpenAI-native workbench LLM adapter.
# OpenAI-first: retrieval via OpenAI Vector Store (file_search), generation via OpenAI Responses API.
print("LLM_OPENAI_LOADED=WORKBENCH_OPENAI_VECTORSTORE")

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

try:
    from .system_prompt import SYSTEM_PROMPT as _BASE_SYSTEM_PROMPT  # type: ignore
except Exception:
    _BASE_SYSTEM_PROMPT = ""


def _env_float(name: str, default: float) -> float:
    try:
        v = (os.getenv(name, "") or "").strip()
        return float(v) if v else default
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        v = (os.getenv(name, "") or "").strip()
        return int(v) if v else default
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _openai_api_key() -> str:
    return (os.getenv("OPENAI_API_KEY") or "").strip()


def _openai_base_url() -> Optional[str]:
    v = (os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "").strip()
    return v or None


def _openai_vector_store_id() -> Optional[str]:
    v = (os.getenv("OPENAI_VECTOR_STORE_ID") or "").strip()
    return v or None


def _llm_model() -> str:
    return (
        os.getenv("LYRA_OPENAI_MODEL")
        or os.getenv("LYRA_LLM_MODEL")
        or os.getenv("LYRA_MODEL")
        or "gpt-5-mini"
    ).strip()


# ---- Timeouts ----
OPENAI_TIMEOUT_CONNECT = _env_float("LYRA_OPENAI_TIMEOUT_CONNECT", 10.0)
OPENAI_TIMEOUT_READ = _env_float("LYRA_OPENAI_TIMEOUT_READ", 180.0)
OPENAI_TIMEOUT: Tuple[float, float] = (OPENAI_TIMEOUT_CONNECT, OPENAI_TIMEOUT_READ)

# ---- Output caps (optional) ----
MAX_OUTPUT_CHARS_DEFAULT = _env_int("LYRA_MAX_OUTPUT_CHARS_DEFAULT", 12000)
MAX_OUTPUT_CHARS_DEEP = _env_int("LYRA_MAX_OUTPUT_CHARS_DEEP", 24000)
DISABLE_OUTPUT_CAP = _env_bool("LYRA_DISABLE_OUTPUT_CAP", True)

# ---- Token caps (Responses API) ----
# By default we do NOT cap output tokens (0 = omit max_output_tokens).
# You can set LYRA_NUM_PREDICT_DEFAULT / LYRA_NUM_PREDICT_DEEP to enforce a cap.
NUM_PREDICT_DEFAULT = _env_int("LYRA_NUM_PREDICT_DEFAULT", 0)
NUM_PREDICT_DEEP = _env_int("LYRA_NUM_PREDICT_DEEP", 0)

# ---- Retries ----
EMPTY_RESPONSE_RETRIES = _env_int("LYRA_EMPTY_RESPONSE_RETRIES", 2)
ERROR_RETRIES = _env_int("LYRA_ERROR_RETRIES", 1)

# ---- Depth markers ----
DEEP_MARKERS = (
    "deep dive",
    "in depth",
    "step by step",
    "full analysis",
    "thorough analysis",
    "exhaustive",
    "very detailed",
    "highly detailed",
    "structural and normative",
    "normative overlaps",
    "constitutional analysis",
    "compare article",
)


def _cap_output(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[TRUNCATED_OUTPUT]"


def _is_deep_request(query: str) -> bool:
    q = (query or "").strip().lower()
    if q.startswith("deep:") or q.startswith("[deep]") or "deep=true" in q:
        return True
    return any(m in q for m in DEEP_MARKERS)


def _client() -> Any:
    if OpenAI is None:
        raise RuntimeError("OpenAI SDK not installed. Run: pip install openai")

    key = _openai_api_key()
    if not key:
        raise RuntimeError("Missing OPENAI_API_KEY. Set it in your environment or .env file.")

    base_url = _openai_base_url()
    if base_url:
        return OpenAI(api_key=key, base_url=base_url)
    return OpenAI(api_key=key)


def _build_system_prompt() -> str:
    """
    Behavior goals:
    - Chatbot-like collaboration, but institutional-grade analysis (not literary).
    - DOCS-first: attempt retrieval for project questions before answering.
    - No fabricated citations, no invented document claims.
    - If DOCS retrieval yields nothing relevant: say so explicitly, then provide general analytical
      perspective clearly labeled as non-DOCS-grounded (Variant B).
    - Markdown is ONLY required when the user explicitly asks for a document/file/ingest/.md.
    """
    sys = (_BASE_SYSTEM_PROMPT or "").strip()
    if not sys:
        sys = (
            "You are LYRA (personal institutional workbench).\n"
            "Mission: help the user analyze, refine, and design constitutional/institutional systems and documents.\n"
            "Tone: serious, rigorous, pragmatic. No storytelling, no filler, no performative verbosity.\n"
        )

    sys += (
        "\n\nCORE RULES:\n"
        "1) Always produce a written final answer (never empty).\n"
        "2) Institutional rigor: be precise, structured, and explicit about uncertainty.\n"
        "3) No hallucinated sources: never invent file names, quotes, articles, or citations.\n"
        "4) DOCS-first workflow: for any project/institutional question, first attempt retrieval from the vector store.\n"
        "5) When DOCS support exists: ground claims in DOCS and clearly reference sources.\n"
        "6) When DOCS support is missing/insufficient: explicitly say 'Not found in DOCS' or 'Insufficient DOCS support',\n"
        "   then continue with a GENERAL ANALYSIS section that is clearly labeled as non-DOCS-grounded.\n"
        "7) Formatting: default to normal readable text. Use strict Markdown ONLY when user explicitly requests a document\n"
        "   (e.g., 'generate document', 'create .md', 'for ingest', 'produce file', 'final draft').\n"
        "\n\nOUTPUT STRUCTURE (default / analysis):\n"
        "- Summary (1-3 bullets)\n"
        "- Findings (contradictions, redundancies, gaps)\n"
        "- Recommendations (actionable)\n"
        "- Sources (DOCS filenames when used; otherwise 'None')\n"
        "- If DOCS missing: add a 'GENERAL ANALYSIS (non-DOCS-grounded)' section.\n"
    )

    if _openai_vector_store_id():
        sys += (
            "\n\nRETRIEVAL (vector store):\n"
            "- Use the file_search tool before answering substantive questions.\n"
            "- Prefer the most relevant passages; do not over-quote.\n"
            "- In 'Sources', list the filenames/titles you relied on.\n"
            "- If file_search returns nothing relevant, state that explicitly.\n"
        )

    return sys.strip()


def _obj_get(x: Any, key: str, default: Any = None) -> Any:
    if x is None:
        return default
    if isinstance(x, dict):
        return x.get(key, default)
    return getattr(x, key, default)


def _extract_text(resp: Any) -> str:
    """
    Extract plain text from an OpenAI Responses API response.

    The SDK may expose `output_text`, but some responses encode text blocks as:
    - content[].type == "output_text" with {text: {value: "..."}}
    - content[].type == "text" with {text: "..."} (or {text: {value: "..."}})

    We support all of the above to avoid false "empty_response" situations.
    """
    # 1) SDK convenience
    t = (_obj_get(resp, "output_text", "") or "").strip()
    if t:
        return t

    # 2) Traverse output items (messages with content blocks)
    out = _obj_get(resp, "output", None)
    parts: List[str] = []
    if isinstance(out, list):
        for item in out:
            if str(_obj_get(item, "type", "") or "") != "message":
                continue
            content = _obj_get(item, "content", None)
            if not isinstance(content, list):
                continue

            for c in content:
                ctype = str(_obj_get(c, "type", "") or "")
                if ctype not in ("output_text", "text"):
                    continue

                # `text` can be a string, a dict/object with `.value`, or nested.
                text_obj = _obj_get(c, "text", None)
                val = _obj_get(text_obj, "value", None)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
                    continue
                if isinstance(text_obj, str) and text_obj.strip():
                    parts.append(text_obj.strip())
                    continue

    return "\n\n".join(parts).strip()



def _is_incomplete_response(resp: Any) -> bool:
    """
    Best-effort detection that the model hit an internal output limit.
    The Responses API may expose `status == "incomplete"` and/or `incomplete_details`.
    We keep this generic to remain SDK-version tolerant.
    """
    status = str(_obj_get(resp, "status", "") or "").lower()
    if status == "incomplete":
        return True
    if _obj_get(resp, "incomplete_details", None) is not None:
        return True
    return False

def _post_openai(
    client: Any,
    model: str,
    system: str,
    prompt: str,
    max_output_tokens: int,
    web_results: str,
) -> str:
    if web_results and web_results.strip():
        prompt = (
            prompt.strip()
            + "\n\n---\nWEB RESULTS (optional context):\n"
            + web_results.strip()
            + "\n---\n"
        )

    kwargs: dict = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }

    vs_id = _openai_vector_store_id()
    if vs_id:
        # OpenAI Responses tool: file_search over vector store
        kwargs["tools"] = [{"type": "file_search", "vector_store_ids": [vs_id]}]

    if max_output_tokens and max_output_tokens > 0:
        kwargs["max_output_tokens"] = int(max_output_tokens)

    resp = client.responses.create(**kwargs, timeout=OPENAI_TIMEOUT)
    text = _extract_text(resp)
    if text and _is_incomplete_response(resp):
        # Signal to the caller that continuation is needed.
        text = text.rstrip() + "\n\n[LYRA_OUTPUT_INCOMPLETE]"
    return text


def answer(query: str, contexts: Iterable[Any], web_results: Optional[str] = "", behavior: Optional[str] = None) -> str:
    # contexts are ignored in OpenAI-first mode; kept for compatibility.
    t0 = time.perf_counter()

    model = _llm_model()
    deep = _is_deep_request(query)

    max_out_tokens = NUM_PREDICT_DEEP if deep else NUM_PREDICT_DEFAULT
    out_cap = MAX_OUTPUT_CHARS_DEEP if deep else MAX_OUTPUT_CHARS_DEFAULT
    if DISABLE_OUTPUT_CAP:
        out_cap = 0

    system = _build_system_prompt()
    prompt = (query or "").strip()
    web = (web_results or "").strip()

    print(
        f"LLM_CALL_START provider=openai model={model} deep={deep} "
        f"max_output_tokens={max_out_tokens} timeout={OPENAI_TIMEOUT} prompt_chars={len(prompt)}"
    )

    client = _client()

    # Retry on empty outputs and transient errors.
    attempts = max(0, int(EMPTY_RESPONSE_RETRIES)) + 1
    err_attempts = max(0, int(ERROR_RETRIES))

    last_err: Optional[str] = None
    for i in range(attempts):
        try:
            text = _post_openai(
                client,
                model=model,
                system=system,
                prompt=prompt,
                max_output_tokens=max_out_tokens,
                web_results=web,
            )
            text = (text or "").strip()
            if text:
                text = _cap_output(text, out_cap)
                ms = int((time.perf_counter() - t0) * 1000)
                print(f"LLM_CALL_END status=ok ms={ms} chars={len(text)}")
                return text

            # Strengthen the prompt and retry.
            prompt = (
                prompt
                + "\n\nIMPORTANT: You MUST output a written final answer now. Do not return empty output."
            )
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"LLM_CALL_END status=empty_response attempt={i+1}/{attempts} ms={ms}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"LLM_CALL_END status=error attempt={i+1}/{attempts} ms={ms} err={last_err}")
            if err_attempts <= 0:
                break
            err_attempts -= 1
            time.sleep(0.4)

    # Final fallback: never empty.
    return (
        "The model did not return a usable answer (empty/failed). "
        "Please retry the same question. If it repeats, check OPENAI_VECTOR_STORE_ID and network."
    )
