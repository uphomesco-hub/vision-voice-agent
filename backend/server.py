from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
import logging
import asyncio
import json
import os
import base64
import io
import re
import websockets
import httpx
from datetime import datetime, timezone
from difflib import SequenceMatcher
from collections import Counter
from typing import Optional, Dict, Any, List, Tuple, AsyncIterator
from PIL import Image

from db import init_db, get_db, AsyncSessionLocal, SessionSnapshot
from personas import get_personas, get_voices
from session_store import SessionStore
from manual_repo import seed_manuals, lookup_manual_tool, get_manual_by_id
from nudge_engine import NudgeEngine
from vision_perception import perceive_scene, observation_signature

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

GEMINI_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
MODEL_ID = os.environ.get("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-latest")
CODING_HELPER_TEXT_MODEL = os.environ.get("CODING_HELPER_TEXT_MODEL", "gemini-3.1-flash-lite")
CODING_HELPER_FALLBACK_TEXT_MODEL = os.environ.get("CODING_HELPER_FALLBACK_TEXT_MODEL", "gemini-3-flash-preview")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_REALTIME_STT_URL = os.environ.get(
    "OPENAI_REALTIME_STT_URL",
    "wss://api.openai.com/v1/realtime?intent=transcription",
)
OPENAI_REALTIME_TRANSCRIPTION_MODEL = os.environ.get(
    "OPENAI_REALTIME_TRANSCRIPTION_MODEL",
    "gpt-4o-mini-transcribe",
)
CODING_HELPER_STT_SAMPLE_RATE = int(os.environ.get("CODING_HELPER_STT_SAMPLE_RATE", "24000"))
CODING_HELPER_STT_SILENCE_MS = int(os.environ.get("CODING_HELPER_STT_SILENCE_MS", "420"))
CODING_HELPER_STT_VAD_THRESHOLD = float(os.environ.get("CODING_HELPER_STT_VAD_THRESHOLD", "0.45"))
CODING_HELPER_CLIENT_VAD_COMMIT = os.environ.get("CODING_HELPER_CLIENT_VAD_COMMIT", "1").lower() not in {"0", "false", "no"}
INPUT_SAMPLE_RATE = 16000
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

CODING_HELPER_LIVE_PROMPT = """
You are only the realtime speech turn detector for Zeno AI Coding Helper.

Rules:
- Do not answer coding questions.
- Do not explain, reason, summarize, or ask follow-up questions.
- For every completed user utterance, respond with exactly one short word: ACK.
- Keep the response as short as possible so the server can move on quickly.
- Always stay in English.
"""

CODING_HELPER_ANSWER_PROMPT = """
You are Zeno AI Coding Helper, a fast voice-to-chat coding assistant.

Core behavior:
- Listen to the user's full spoken turn before answering.
- Keep answers optimized for the chat transcript. The app displays only your output transcript and does not play assistant audio.
- Help with coding, debugging, architecture, commands, Git, deployments, APIs, iOS, web, backend, and developer workflow questions.
- Keep answers direct and useful. Default to short interview-acceptable answers: 3-6 tight bullets or a compact paragraph.
- If the question needs depth, give enough detail to be correct, but avoid long essays unless the user asks for a deep explanation.
- Prefer wording the user can say aloud in an interview: clear definition, key tradeoff, and one concrete example when useful.
- For debugging or implementation questions, include concrete commands/code only when they materially help.
- If the user interrupts with a new question, stop the previous answer and answer the newer question.
- If the user is merely reciting, repeating, rehearsing, or reading back your previous answer, do not answer. Stay silent.
- If the utterance is not a question or actionable coding request, do not answer unless it clearly asks for help.
- Do not write meta headings like "Acknowledge readiness", "Defining essence", or describe your thinking process.
- For simple definition questions like "what is Python", answer the definition directly.
- Always answer in English.
"""

CODING_HELPER_PROMPT = CODING_HELPER_ANSWER_PROMPT

RECITATION_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "i", "if", "in", "is", "it", "its", "me", "my", "of", "on", "or",
    "our", "so", "that", "the", "then", "there", "this", "to", "was", "we",
    "with", "you", "your",
}

QUESTION_OR_REQUEST_RE = re.compile(
    r"\b("
    r"how|what|why|where|when|which|who|can|could|should|would|do|does|did|"
    r"is|are|will|explain|tell me|help me|show me|fix|debug|write|create|"
    r"make|implement|build|review|check|compare|refactor|run|install|deploy|"
    r"error|exception|bug|issue|failing|failed|crash|code|function|class|api|"
    r"backend|frontend|react|javascript|typescript|python|swift|xcode|ios|git|"
    r"github|aws|ec2|netlify|database|server|terminal|command"
    r")\b",
    re.IGNORECASE,
)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#._ -]+", " ", (value or "").lower())).strip()


def _content_tokens(value: str) -> List[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9+#._-]+", (value or "").lower())
        if len(token) > 2 and token not in RECITATION_STOPWORDS
    ]


def _token_cosine(left: List[str], right: List[str]) -> float:
    if not left or not right:
        return 0.0
    a = Counter(left)
    b = Counter(right)
    common = set(a) & set(b)
    numerator = sum(a[token] * b[token] for token in common)
    left_norm = sum(count * count for count in a.values()) ** 0.5
    right_norm = sum(count * count for count in b.values()) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _is_question_or_coding_request(text: str) -> bool:
    normalized = _compact_text(text)
    if not normalized:
        return False
    if "?" in text:
        return True
    if QUESTION_OR_REQUEST_RE.search(normalized):
        return True
    tokens = _content_tokens(normalized)
    return len(tokens) >= 4 and any(token in tokens for token in {"npm", "pip", "git", "docker", "server", "route", "branch"})


def _is_probable_answer_recitation(user_text: str, last_answer: str) -> Tuple[bool, Dict[str, float]]:
    if not user_text.strip() or not last_answer.strip():
        return False, {"overlap": 0.0, "cosine": 0.0, "sequence": 0.0}

    user_tokens = _content_tokens(user_text)
    answer_tokens = _content_tokens(last_answer)
    if len(user_tokens) < 5 or len(answer_tokens) < 8:
        return False, {"overlap": 0.0, "cosine": 0.0, "sequence": 0.0}

    user_set = set(user_tokens)
    answer_set = set(answer_tokens)
    overlap = len(user_set & answer_set) / max(1, len(user_set))
    cosine = _token_cosine(user_tokens, answer_tokens)
    sequence = SequenceMatcher(None, _compact_text(user_text), _compact_text(last_answer)).ratio()
    question_like = _is_question_or_coding_request(user_text)

    strong_match = overlap >= 0.78 or cosine >= 0.74 or sequence >= 0.70
    medium_non_question_match = not question_like and (overlap >= 0.56 or cosine >= 0.50 or sequence >= 0.54)
    return strong_match or medium_non_question_match, {
        "overlap": round(overlap, 3),
        "cosine": round(cosine, 3),
        "sequence": round(sequence, 3),
    }


def _voice_word_key(word: str) -> str:
    return re.sub(r"^[^\w+#.]+|[^\w+#.]+$", "", (word or "").lower())


def _voice_words_key(words: List[str]) -> List[str]:
    return [_voice_word_key(word) for word in words if _voice_word_key(word)]


def _merge_voice_fragment(buffer: str, fragment: str) -> str:
    """Merge streaming STT fragments that may arrive as deltas or cumulative text."""
    current = re.sub(r"\s+", " ", (buffer or "").strip())
    incoming = re.sub(r"\s+", " ", (fragment or "").strip())
    if not incoming:
        return current
    if not current:
        return incoming

    current_norm = _compact_text(current)
    incoming_norm = _compact_text(incoming)
    if not incoming_norm or incoming_norm in current_norm:
        return current
    if current_norm and incoming_norm.startswith(current_norm):
        return incoming

    current_words = current.split()
    incoming_words = incoming.split()
    current_keys = _voice_words_key(current_words)
    incoming_keys = _voice_words_key(incoming_words)
    max_overlap = min(len(current_keys), len(incoming_keys))
    for size in range(max_overlap, 0, -1):
        if current_keys[-size:] == incoming_keys[:size]:
            return " ".join(current_words + incoming_words[size:])
    return f"{current} {incoming}".strip()


def _clean_voice_text(text: str) -> str:
    """Reduce common live-STT stutters before gating or answering."""
    value = re.sub(r"\s+", " ", (text or "").strip())
    if not value:
        return ""

    words = value.split()
    collapsed: List[str] = []
    previous_key = ""
    for word in words:
        key = _voice_word_key(word)
        if key and key == previous_key:
            continue
        collapsed.append(word)
        if key:
            previous_key = key

    words = collapsed
    changed = True
    while changed:
        changed = False
        output: List[str] = []
        i = 0
        while i < len(words):
            matched = False
            max_phrase = min(5, (len(words) - i) // 2)
            for size in range(max_phrase, 1, -1):
                left = _voice_words_key(words[i:i + size])
                right = _voice_words_key(words[i + size:i + (2 * size)])
                if left and left == right:
                    output.extend(words[i:i + size])
                    i += 2 * size
                    changed = True
                    matched = True
                    break
            if not matched:
                output.append(words[i])
                i += 1
        words = output

    value = " ".join(words)

    def _repair_definition_question(match: re.Match) -> str:
        subject = match.group(1).strip().rstrip("?.!")
        return f"What is {subject}?"

    value = re.sub(
        r"^\s*what\s+(?:it\s+)?is\s+(?:by\s+)?(?:this\s+)?([a-z][a-z0-9+#._ -]{0,80})\??\s*$",
        _repair_definition_question,
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+([?.!,])", r"\1", value).strip()


def _clean_assistant_answer(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?is)^\*\*(?:acknowledge|acknowledging|defining|analyzing|thinking|planning)[^*]{0,90}\*\*\s*",
        "",
        value,
    ).strip()
    return value


def _format_coding_helper_history(turns: List[Any]) -> str:
    lines: List[str] = []
    for turn in turns[-12:]:
        role = "User" if turn.role == "user" else "Assistant"
        content = _clean_voice_text(turn.content) if turn.role == "user" else (turn.content or "").strip()
        if content:
            lines.append(f"{role}: {content[:1200]}")
    return "\n".join(lines)


def _build_coding_helper_answer_payload(user_text: str, turns: List[Any]) -> Dict[str, Any]:
    history = _format_coding_helper_history(turns)
    history_block = f"\n\nRecent chat context:\n{history}" if history else ""
    prompt = (
        f"{CODING_HELPER_ANSWER_PROMPT}{history_block}\n\n"
        "Current user question from voice transcription:\n"
        f"{user_text}\n\n"
        "Answer now. Keep it concise, direct, and interview-acceptable. "
        "If the transcription is awkward but clearly asks a simple definition, repair it silently and answer the intended question."
    )
    return {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "temperature": 0.25,
            "topP": 0.9,
            "maxOutputTokens": 520,
        },
    }


def _extract_gemini_text_chunk(data: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for candidate in data.get("candidates", []) or []:
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            text = part.get("text")
            if text:
                chunks.append(text)
    return "".join(chunks)


async def _stream_coding_helper_answer(user_text: str, turns: List[Any], timeout: float = 20.0) -> AsyncIterator[str]:
    if not GEMINI_API_KEY:
        yield "Backend is missing `GOOGLE_API_KEY`, so I cannot generate the answer right now."
        return

    payload = _build_coding_helper_answer_payload(user_text, turns)
    models = [CODING_HELPER_TEXT_MODEL]
    if CODING_HELPER_FALLBACK_TEXT_MODEL and CODING_HELPER_FALLBACK_TEXT_MODEL not in models:
        models.append(CODING_HELPER_FALLBACK_TEXT_MODEL)

    last_error: Optional[Exception] = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={GEMINI_API_KEY}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=timeout)) as client:
                async with client.stream("POST", url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw_data = line[5:].strip()
                        if not raw_data or raw_data == "[DONE]":
                            continue
                        try:
                            chunk = _extract_gemini_text_chunk(json.loads(raw_data))
                        except json.JSONDecodeError:
                            logger.debug("Skipping malformed Gemini stream line")
                            continue
                        if chunk:
                            yield chunk
            return
        except httpx.HTTPStatusError as exc:
            last_error = exc
            logger.warning(f"coding helper stream failed for {model}: {exc.response.status_code} {exc.response.text[:180]}")
        except Exception as exc:
            last_error = exc
            logger.warning(f"coding helper stream failed for {model}: {exc}")

    logger.error(f"coding helper streaming answer failed after fallbacks: {last_error}")
    yield "I hit an answer-generation error. Please ask that once more."


async def _generate_coding_helper_answer(user_text: str, turns: List[Any], timeout: float = 12.0) -> str:
    if not GEMINI_API_KEY:
        return "Backend is missing `GOOGLE_API_KEY`, so I cannot generate the answer right now."

    payload = _build_coding_helper_answer_payload(user_text, turns)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CODING_HELPER_TEXT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            parts = data["candidates"][0]["content"]["parts"]
            answer = "".join(part.get("text", "") for part in parts)
            return _clean_assistant_answer(answer) or "I heard the question, but I could not form a useful answer. Please ask it once more."
    except httpx.TimeoutException:
        logger.warning("coding helper answer timed out")
        return "That took too long to answer. Ask it again in one shorter question and I’ll respond faster."
    except Exception as exc:
        logger.error(f"coding helper answer failed: {exc}")
        return "I hit an answer-generation error. Please ask that once more."

# ─── Frame Diff ──────────────────────
DIFF_THUMB_SIZE = (32, 32)
DIFF_THRESHOLD = 12.0
DIFF_COOLDOWN_FRAMES = 1

def compute_frame_diff(prev_bytes, curr_bytes):
    """Compare two JPEG blobs as tiny grayscale thumbnails. Returns mean pixel diff 0-255."""
    try:
        prev = Image.open(io.BytesIO(prev_bytes)).convert("L").resize(DIFF_THUMB_SIZE)
        curr = Image.open(io.BytesIO(curr_bytes)).convert("L").resize(DIFF_THUMB_SIZE)
        pp, cp = list(prev.getdata()), list(curr.getdata())
        return sum(abs(a - b) for a, b in zip(pp, cp)) / len(pp)
    except Exception:
        return 0.0

# ─── Perception state cache (survives WS reconnects within same process) ───
_perception_cache: Dict[str, Dict[str, Any]] = {}

def _get_perception_state(sid: str) -> Dict[str, Any]:
    if sid not in _perception_cache:
        _perception_cache[sid] = {
            "last_perception": None,
            "last_perception_sig": "",
            "prev_frame_bytes": None,
            "latest_frame_b64": None,
        }
    return _perception_cache[sid]

def _clear_perception_state(sid: str):
    _perception_cache.pop(sid, None)


MANUAL_CLOSE_RE = re.compile(
    r"\b(close|hide|dismiss|remove|clear|stop|cancel)\s+(the\s+)?manual\b|"
    r"\bmanual\s+(close|hide|dismiss|remove|clear|stop|cancel)\b|"
    r"\b(no|dont|don't)\s+(use\s+)?(the\s+)?manual\b",
    re.IGNORECASE,
)

MANUAL_TERM_STOPWORDS = {
    "a", "an", "and", "for", "from", "guide", "help", "how", "manual",
    "model", "need", "of", "on", "repair", "the", "this", "to", "with",
}


def _manual_close_requested(text: str) -> bool:
    return bool(text and MANUAL_CLOSE_RE.search(text))


def _manual_lookup_terms(args: Dict[str, Any]) -> List[str]:
    terms: List[str] = []
    for key in ("brand", "model", "device_type", "query"):
        value = str(args.get(key, "") or "").strip().lower()
        if not value:
            continue
        terms.append(value)
        terms.extend(
            token
            for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", value)
            if token not in MANUAL_TERM_STOPWORDS
        )
    seen = set()
    return [term for term in terms if not (term in seen or seen.add(term))]


def _perception_confirms_terms(perception: Dict[str, Any], terms: List[str]) -> bool:
    confidence = float(perception.get("confidence", 0) or 0)
    if confidence < 0.55:
        return False

    visual_fields = [
        perception.get("device_state", ""),
        perception.get("focus_area", ""),
        " ".join(str(x) for x in perception.get("visible_features", []) or []),
        " ".join(str(x) for x in perception.get("changed_vs_prior", []) or []),
    ]
    visual_text = " ".join(visual_fields).lower()
    return any(term in visual_text for term in terms)


def _manual_lookup_grounded(
    args: Dict[str, Any],
    session_id: str,
    *,
    camera_status: str = "unavailable",
    frame_count: int = 0,
) -> Tuple[bool, str]:
    terms = _manual_lookup_terms(args)
    if not terms:
        return False, "manual lookup had no named device terms"

    if camera_status != "on" or frame_count <= 0:
        return False, "camera is unavailable"

    perception = (_get_perception_state(session_id).get("last_perception") or {})
    if _perception_confirms_terms(perception, terms):
        return True, "visual perception confirmed the device"

    return False, "requested manual terms were not visible"


async def _confirm_manual_lookup_grounding(
    args: Dict[str, Any],
    session_id: str,
    *,
    camera_status: str = "unavailable",
    frame_count: int = 0,
) -> Tuple[bool, str]:
    grounded, reason = _manual_lookup_grounded(
        args,
        session_id,
        camera_status=camera_status,
        frame_count=frame_count,
    )
    if grounded:
        return grounded, reason

    terms = _manual_lookup_terms(args)
    pstate = _get_perception_state(session_id)
    latest_frame_b64 = pstate.get("latest_frame_b64")
    if not terms:
        return False, "manual lookup had no named device terms"
    if camera_status != "on" or frame_count <= 0 or not latest_frame_b64:
        return False, "camera is unavailable"

    try:
        obs = await perceive_scene(latest_frame_b64, prior_obs=pstate.get("last_perception"))
    except Exception as e:
        logger.warning(f"[{session_id}] silent manual vision confirmation failed: {e}")
        return False, "silent vision confirmation failed"

    if not obs:
        return False, "silent vision confirmation returned no observation"

    confidence = float(obs.get("confidence", 0) or 0)
    if confidence >= 0.5:
        pstate["last_perception"] = obs
        pstate["last_perception_sig"] = observation_signature(obs)

    if _perception_confirms_terms(obs, terms):
        return True, "silent camera confirmation matched the requested device"

    return False, "silent camera confirmation did not see the requested device"


async def close_active_manual(session_id: str, client_ws: WebSocket, gemini_ws=None, reason: str = ""):
    async with AsyncSessionLocal() as db:
        await SessionStore.update_session(
            db,
            session_id,
            active_manual_id=None,
            active_device_type=None,
            active_device_model=None,
            current_step=0,
        )

    await client_ws.send_json({
        "type": "manual.closed",
        "message": "Manual closed.",
        "reason": reason,
    })

    if gemini_ws:
        await gemini_ws.send(json.dumps({
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{
                        "text": (
                            "[MANUAL_CLOSED] The active manual has been closed at the user's request. "
                            "Continue the conversation normally without using that manual. "
                            "Do not open another manual unless the user explicitly names a device or the camera visibly confirms one."
                        )
                    }],
                }],
                "turnComplete": True,
            }
        }))

LOOKUP_MANUAL_DECL = {
    "name": "lookup_manual",
    "description": "Search internal repair manuals database for a specific device. Returns structured repair guidance, troubleshooting steps, warnings, and tool requirements. Use only after the device is visible on the current camera feed; the backend will silently verify the latest frame before opening a manual.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "brand": {"type": "STRING", "description": "Device brand name (e.g., Stihl, Dyson)"},
            "model": {"type": "STRING", "description": "Device model (e.g., FS 56 RC, V15 Detect)"},
            "device_type": {"type": "STRING", "description": "Device category (e.g., string trimmer, vacuum)"},
            "issue": {"type": "STRING", "description": "The problem described by the user"},
            "query": {"type": "STRING", "description": "Free-text search query"}
        },
        "required": []
    }
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server starting — init DB...")
    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_manuals(db)
    logger.info("DB ready, manuals seeded")
    yield
    logger.info("Server shutting down")

app = FastAPI(title="Zeno AI", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ─── REST API ───────────────────────────
api = APIRouter(prefix="/api")

@api.get("/health")
async def health():
    return {
        "status": "healthy",
        "model": MODEL_ID,
        "mode": "coding-helper",
        "version": "2.1-coding-helper",
    }

@api.get("/personas")
async def list_personas():
    return get_personas()

@api.get("/voices")
async def list_voices():
    return get_voices()

@api.post("/sessions")
async def create_session(payload: dict, db: AsyncSession = Depends(get_db)):
    s = await SessionStore.create_session(db, payload.get("persona_id", "calm-expert"), payload.get("voice_id", "Puck"))
    return {"id": s.id, "status": s.status}

@api.get("/sessions/{sid}/state")
async def get_state(sid: str, db: AsyncSession = Depends(get_db)):
    st = await SessionStore.get_session_state(db, sid)
    return st or {"error": "Session not found"}

@api.get("/sessions/{sid}/logs")
async def get_logs(sid: str, db: AsyncSession = Depends(get_db)):
    turns = await SessionStore.get_turns(db, sid, limit=100)
    tool_runs = await SessionStore.get_tool_runs(db, sid, limit=50)
    return {
        "turns": [{"role": t.role, "content": t.content, "source_type": t.source_type, "created_at": t.created_at.isoformat()} for t in turns],
        "tool_runs": [{"tool": tr.tool_name, "status": tr.status, "input": tr.input_data, "output": tr.output_data, "created_at": tr.created_at.isoformat()} for tr in tool_runs],
    }

@api.post("/sessions/{sid}/end")
async def end_session_api(sid: str, db: AsyncSession = Depends(get_db)):
    return {"status": "ended" if await SessionStore.end_session(db, sid) else "not_found"}

@api.get("/sessions/{sid}/full-transcript")
async def get_full_transcript(sid: str, db: AsyncSession = Depends(get_db)):
    """Get the complete session transcript + tool runs + observations for debugging."""
    state = await SessionStore.get_session_state(db, sid)
    if not state:
        return {"error": "Session not found"}
    return {
        "session_id": state["session_id"],
        "status": state["status"],
        "persona_id": state["persona_id"],
        "voice_id": state["voice_id"],
        "active_manual_id": state["active_manual_id"],
        "current_step": state["current_step"],
        "transcript": [{"role": t["role"], "text": t["content"], "source": t["source_type"], "at": t["created_at"]} for t in state["turns"]],
        "tool_runs": [{"tool": tr["tool_name"], "status": tr["status"], "input": tr["input"], "output": tr["output"], "at": tr["created_at"]} for tr in state["tool_runs"]],
        "observations": state["observations"],
    }

app.include_router(api)


async def run_coding_helper_session(
    websocket: WebSocket,
    *,
    session_id: str,
    resume_id: Optional[str],
    language: str,
) -> bool:
    """Run the low-latency coding helper lane without touching the vision agent path."""
    if resume_id:
        async with AsyncSessionLocal() as db:
            state = await SessionStore.get_session_state(db, session_id)
            if state:
                await websocket.send_json({"type": "session.state", "data": state})

    if not OPENAI_API_KEY:
        await websocket.send_json({"type": "error", "message": "Backend is missing OPENAI_API_KEY for realtime transcription."})
        await websocket.send_json({"type": "assistant.state", "state": "idle"})
        await websocket.send_json({"type": "status", "message": "OpenAI STT key missing"})
        return False

    openai_ws = None
    recv_openai_task: Optional[asyncio.Task] = None
    answer_task: Optional[asyncio.Task] = None
    alive = True
    intentional_end = False
    answer_seq = 0
    last_assistant_answer = ""
    item_buffers: Dict[str, str] = {}

    async def cancel_answer_for_interruption():
        nonlocal answer_seq, answer_task
        if answer_task and not answer_task.done():
            answer_seq += 1
            answer_task.cancel()
            await websocket.send_json({"type": "interrupted"})
            await websocket.send_json({"type": "assistant.state", "state": "listening"})
            await websocket.send_json({"type": "status", "message": "Listening for the new question..."})

    async def answer_and_stream(seq: int, user_text: str, prior_turns: List[Any]):
        nonlocal last_assistant_answer
        full_answer = ""
        try:
            async for chunk in _stream_coding_helper_answer(user_text, prior_turns):
                if not alive or seq != answer_seq:
                    return
                full_answer += chunk
                await websocket.send_json({"type": "transcription", "role": "assistant", "text": chunk, "final": False})

            if not alive or seq != answer_seq:
                return

            final_answer = _clean_assistant_answer(full_answer) or "I heard the question, but I could not form a useful answer. Please ask it once more."
            await websocket.send_json({"type": "transcription", "role": "assistant", "text": final_answer, "final": True, "replace": True})
            last_assistant_answer = final_answer
            async with AsyncSessionLocal() as db:
                await SessionStore.add_turn(db, session_id, "assistant", final_answer)
            await websocket.send_json({"type": "turn_complete"})
            await websocket.send_json({"type": "assistant.state", "state": "listening"})
            await websocket.send_json({"type": "status", "message": "Listening for a coding question..."})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{session_id}] coding streaming answer failed: {exc}")
            if alive and seq == answer_seq:
                await websocket.send_json({"type": "error", "message": "Answer generation failed. Please ask again."})
                await websocket.send_json({"type": "turn_complete"})
                await websocket.send_json({"type": "assistant.state", "state": "listening"})

    async def handle_completed_user_turn(raw_text: str, *, replace_transcript: bool):
        nonlocal answer_seq, answer_task
        user_text = _clean_voice_text(raw_text)
        if not user_text:
            return

        await websocket.send_json({
            "type": "transcription",
            "role": "user",
            "text": user_text,
            "final": True,
            "replace": replace_transcript,
        })

        is_recitation, metrics = _is_probable_answer_recitation(user_text, last_assistant_answer)
        is_actionable = _is_question_or_coding_request(user_text)

        if is_recitation:
            logger.info(f"[{session_id}] Suppressed likely answer recitation: metrics={metrics} text={user_text[:120]!r}")
            await websocket.send_json({"type": "recitation.ignored"})
            await websocket.send_json({"type": "assistant.state", "state": "listening"})
            await websocket.send_json({"type": "status", "message": "Listening..."})
            return

        if not is_actionable:
            logger.info(f"[{session_id}] Suppressed non-question voice turn: {user_text[:120]!r}")
            await websocket.send_json({"type": "non_question.ignored"})
            await websocket.send_json({"type": "assistant.state", "state": "listening"})
            await websocket.send_json({"type": "status", "message": "Listening for a coding question..."})
            return

        await websocket.send_json({"type": "assistant.state", "state": "thinking"})
        await websocket.send_json({"type": "status", "message": "Answering..."})
        async with AsyncSessionLocal() as db:
            prior_turns = await SessionStore.get_turns(db, session_id, limit=12)
            await SessionStore.add_turn(db, session_id, "user", user_text)

        answer_seq += 1
        if answer_task and not answer_task.done():
            answer_task.cancel()
        answer_task = asyncio.create_task(answer_and_stream(answer_seq, user_text, prior_turns))

    async def recv_openai():
        nonlocal alive
        try:
            async for raw_msg in openai_ws:
                if not alive:
                    break
                try:
                    data = json.loads(raw_msg)
                    event_type = data.get("type")

                    if event_type == "error":
                        error = data.get("error") or {}
                        message = str(error.get("message") or "OpenAI realtime transcription error")
                        if "empty" in message.lower() and "buffer" in message.lower():
                            logger.debug(f"[{session_id}] Ignoring empty local VAD commit: {message}")
                            continue
                        logger.error(f"[{session_id}] OpenAI STT error: {message}")
                        await websocket.send_json({"type": "error", "message": message})
                        continue

                    if event_type == "input_audio_buffer.speech_started":
                        await cancel_answer_for_interruption()
                        await websocket.send_json({"type": "assistant.state", "state": "listening"})
                        await websocket.send_json({"type": "status", "message": "Listening..."})
                        continue

                    if event_type == "input_audio_buffer.speech_stopped":
                        await websocket.send_json({"type": "status", "message": "Transcribing..."})
                        continue

                    if event_type == "conversation.item.input_audio_transcription.delta":
                        item_id = data.get("item_id") or "current"
                        delta = data.get("delta") or ""
                        if delta:
                            item_buffers[item_id] = f"{item_buffers.get(item_id, '')}{delta}"
                            await websocket.send_json({"type": "transcription", "role": "user", "text": delta, "final": False})
                        continue

                    if event_type == "conversation.item.input_audio_transcription.completed":
                        item_id = data.get("item_id") or "current"
                        transcript = data.get("transcript") or item_buffers.get(item_id, "")
                        item_buffers.pop(item_id, None)
                        await handle_completed_user_turn(transcript, replace_transcript=True)
                        continue

                    if event_type in {"session.updated", "transcription_session.updated"}:
                        logger.info(f"[{session_id}] OpenAI STT ready ({OPENAI_REALTIME_TRANSCRIPTION_MODEL})")
                        await websocket.send_json({"type": "status", "message": "Listening for a coding question..."})
                        continue
                except Exception as exc:
                    logger.error(f"[{session_id}] OpenAI STT msg err: {exc}")
        except websockets.exceptions.ConnectionClosedError as exc:
            alive = False
            logger.error(f"[{session_id}] OpenAI STT closed: {exc}")
            try:
                await websocket.send_json({"type": "error", "message": "Realtime transcription disconnected."})
            except Exception:
                pass
        except asyncio.CancelledError:
            pass

    try:
        logger.info(f"[{session_id}] Connecting OpenAI realtime STT")
        openai_ws = await websockets.connect(
            OPENAI_REALTIME_STT_URL,
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1",
                "OpenAI-Safety-Identifier": session_id,
            },
            ping_interval=30,
            ping_timeout=60,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
        )
        setup_payload = {
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": OPENAI_REALTIME_TRANSCRIPTION_MODEL,
                    "language": "en" if language.lower().startswith("en") else language.split("-")[0],
                    "prompt": "Coding assistant dictation. Expect terms like Python, JavaScript, React, Swift, Xcode, GitHub, AWS, EC2, API, backend, frontend, deployment, branch, and terminal commands.",
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": CODING_HELPER_STT_VAD_THRESHOLD,
                    "prefix_padding_ms": 240,
                    "silence_duration_ms": CODING_HELPER_STT_SILENCE_MS,
                },
                "input_audio_noise_reduction": {"type": "near_field"},
                "include": [],
            },
        }
        await openai_ws.send(json.dumps(setup_payload))
        recv_openai_task = asyncio.create_task(recv_openai())

        await websocket.send_json({"type": "assistant.state", "state": "listening"})
        await websocket.send_json({"type": "status", "message": "Listening for a coding question..."})

        while alive:
            try:
                inbound = await asyncio.wait_for(websocket.receive(), timeout=900)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "message": "Session timeout (15 min idle)"})
                break

            if inbound.get("type") == "websocket.disconnect":
                break

            raw_bytes = inbound.get("bytes")
            raw_text = inbound.get("text")

            try:
                if raw_bytes is not None:
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(raw_bytes).decode("ascii"),
                    }))
                    continue

                if raw_text is None:
                    continue

                msg = json.loads(raw_text)
                msg_type = msg.get("type")

                if msg_type == "audio":
                    audio_b64 = msg.get("data")
                    if audio_b64:
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": audio_b64}))

                elif msg_type == "audio.commit":
                    if CODING_HELPER_CLIENT_VAD_COMMIT:
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

                elif msg_type == "text":
                    await cancel_answer_for_interruption()
                    await handle_completed_user_turn(msg.get("content", ""), replace_transcript=False)

                elif msg_type == "heartbeat":
                    await websocket.send_json({"type": "heartbeat"})

                elif msg_type == "end":
                    intentional_end = True
                    logger.info(f"[{session_id}] Client ended coding helper session intentionally")
                    break
            except websockets.exceptions.ConnectionClosed:
                alive = False
                await websocket.send_json({"type": "error", "message": "Realtime transcription disconnected."})
                break
            except Exception as exc:
                logger.error(f"[{session_id}] coding helper forward err: {exc}")

    finally:
        alive = False
        if recv_openai_task:
            recv_openai_task.cancel()
            try:
                await recv_openai_task
            except asyncio.CancelledError:
                pass
        if answer_task:
            answer_task.cancel()
            try:
                await answer_task
            except asyncio.CancelledError:
                pass
        if openai_ws:
            try:
                await openai_ws.close()
            except Exception:
                pass

    return intentional_end


# ─── WEBSOCKET ───────────────────────────
@app.websocket("/api/ws/session")
async def ws_session(websocket: WebSocket):
    await websocket.accept()
    logger.info("WS connected")

    gemini_ws = None
    recv_task = None
    alive = True
    session_id = None
    frame_count = 0
    camera_status = "unavailable"
    nudge = NudgeEngine()
    ai_speaking = False
    intentional_end = False
    answer_task: Optional[asyncio.Task] = None

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        cfg = json.loads(raw)
        if cfg.get("type") != "config":
            await websocket.send_json({"type": "error", "message": "First message must be config"})
            return

        persona_id = cfg.get("persona_id", "calm-expert")
        voice_id = cfg.get("voice_id", "Puck")
        language = cfg.get("language", "en-US")
        resume_id = cfg.get("resume_session_id")  # For reconnect-with-history
        assistant_mode = cfg.get("mode", "coding_helper")
        text_only_mode = assistant_mode == "coding_helper"

        # Create or resume session
        async with AsyncSessionLocal() as db:
            if resume_id:
                existing = await SessionStore.get_session(db, resume_id)
                if existing and existing.status == "active":
                    session_id = resume_id
                    logger.info(f"[{session_id}] Resuming session")
                else:
                    s = await SessionStore.create_session(db, persona_id, voice_id)
                    session_id = s.id
            else:
                s = await SessionStore.create_session(db, persona_id, voice_id)
                session_id = s.id

        await websocket.send_json({"type": "session.ready", "session_id": session_id})
        await websocket.send_json({"type": "assistant.state", "state": "connecting"})

        if text_only_mode:
            intentional_end = await run_coding_helper_session(
                websocket,
                session_id=session_id,
                resume_id=resume_id,
                language=language,
            )
            return

        # Build prompt + conversation history for context
        from prompts import build_agent_prompt
        system_prompt = CODING_HELPER_LIVE_PROMPT if text_only_mode else build_agent_prompt(persona_id, voice_id)

        # Load previous turns for reconnect context
        history_context = ""
        active_manual_context = ""
        if resume_id:
            async with AsyncSessionLocal() as db:
                turns = await SessionStore.get_turns(db, session_id, limit=20)
                if turns:
                    history_context = "\n\nPREVIOUS CONVERSATION (for context — session was briefly interrupted):\n"
                    for t in turns:
                        role_label = "User" if t.role == "user" else "Assistant"
                        history_context += f"{role_label}: {t.content}\n"
                    history_context += "\nContinue the conversation naturally from where you left off. Do NOT repeat your last response."

                if not text_only_mode:
                    # Load active manual context
                    sess = await SessionStore.get_session(db, session_id)
                    if sess and sess.active_manual_id:
                        manual = await get_manual_by_id(db, sess.active_manual_id)
                        if manual:
                            active_manual_context = f"\n\nACTIVE MANUAL (already loaded — do NOT call lookup_manual again): {manual.get('brand','')} {manual.get('model','')} — {manual.get('title','')}\nCurrent step: {sess.current_step}\n"

                    # Load previous tool runs so Gemini doesn't repeat them
                    tool_runs = await SessionStore.get_tool_runs(db, session_id, limit=10)
                    if tool_runs:
                        history_context += "\n\nPREVIOUS TOOL CALLS (already executed — do NOT repeat):\n"
                        for tr in tool_runs:
                            if tr.tool_name == "lookup_manual":
                                out = tr.output_data or {}
                                if out.get("selected_manual_id"):
                                    history_context += f"- lookup_manual: Found {out.get('manual_summary','')}\n"
                                else:
                                    history_context += f"- lookup_manual: No manual found for {tr.input_data}. Used general knowledge instead.\n"
                        history_context += "Do NOT call these tools again unless the user mentions a DIFFERENT device.\n"

        full_prompt = system_prompt + history_context + active_manual_context
        if text_only_mode:
            full_prompt += (
                "\n\nSESSION MODE: Voice input, chat transcript output. Gemini Live is used here only for speech turn detection. "
                "The server generates the real chat answer with a separate fast text model after the user's turn ends. "
                "Do not answer the user's coding content in Live; respond only with ACK."
            )
        else:
            full_prompt += (
                "\n\nCAMERA AVAILABILITY: At session start no camera frame has been received yet. "
                "Until you receive image frames, do not claim you can see the scene. "
                "If the user asks what you see before frames arrive, say: "
                "\"I can't see anything right now — please turn on the camera or check camera access in settings.\""
            )
        if language.lower().startswith("en"):
            full_prompt += (
                "\n\nLANGUAGE: The client requested English. Speak only in English and keep all output transcription in English. "
                "If the input audio transcription is in another language, answer in English anyway."
            )

        # Connect to Gemini
        logger.info(f"[{session_id}] Connecting Gemini (resume={bool(resume_id)})")
        gemini_ws = await websockets.connect(
            GEMINI_WS_URL,
            ping_interval=30, ping_timeout=60, close_timeout=5,
            max_size=16 * 1024 * 1024,
        )

        generation_config = {
            "response_modalities": ["AUDIO"],
            "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": voice_id if not text_only_mode else "Puck"}}},
        }
        setup_payload = {
            "model": f"models/{MODEL_ID}",
            "generation_config": generation_config,
            "system_instruction": {"parts": [{"text": full_prompt}]},
            "input_audio_transcription": {},
        }
        if text_only_mode:
            setup_payload["tools"] = []
        else:
            setup_payload["tools"] = [{"function_declarations": [LOOKUP_MANUAL_DECL]}]
            setup_payload["output_audio_transcription"] = {}

        setup = {
            "setup": {
                **setup_payload,
            }
        }
        await gemini_ws.send(json.dumps(setup))
        json.loads(await asyncio.wait_for(gemini_ws.recv(), timeout=10))
        logger.info(f"[{session_id}] Gemini ready")

        await websocket.send_json({"type": "assistant.state", "state": "listening"})
        await websocket.send_json({"type": "status", "message": "Listening for a coding question..." if text_only_mode else "Connected! Start speaking..."})

        # Send session state to client if resuming
        if resume_id:
            async with AsyncSessionLocal() as db:
                state = await SessionStore.get_session_state(db, session_id)
                if state:
                    await websocket.send_json({"type": "session.state", "data": state})

        # Trigger greeting — tell Gemini to introduce itself
        if not resume_id and not text_only_mode:
            greet_msg = {
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": "Session just started. Greet the user briefly in English and ask what device they need help with. Keep it to one natural sentence."}]}],
                    "turnComplete": True
                }
            }
            await gemini_ws.send(json.dumps(greet_msg))
            logger.info(f"[{session_id}] Greeting trigger sent")

        # ─── Gemini → Client ───
        last_assistant_answer = ""
        answer_seq = 0

        async def recv_gemini():
            nonlocal alive, ai_speaking, last_assistant_answer, answer_task, answer_seq
            user_buf = ""
            asst_buf = ""
            manual_close_seen = False
            assistant_text_part_seen = False

            async def answer_and_send(seq: int, user_text: str, prior_turns: List[Any]):
                nonlocal last_assistant_answer
                try:
                    answer = await _generate_coding_helper_answer(user_text, prior_turns)
                    if not alive or seq != answer_seq:
                        return
                    await websocket.send_json({"type": "transcription", "role": "assistant", "text": answer, "final": True})
                    last_assistant_answer = answer
                    async with AsyncSessionLocal() as db:
                        await SessionStore.add_turn(db, session_id, "assistant", answer)
                    await websocket.send_json({"type": "turn_complete"})
                    await websocket.send_json({"type": "assistant.state", "state": "listening"})
                    await websocket.send_json({"type": "status", "message": "Listening for a coding question..."})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"[{session_id}] coding answer task failed: {exc}")
                    if alive and seq == answer_seq:
                        await websocket.send_json({"type": "error", "message": "Answer generation failed. Please ask again."})
                        await websocket.send_json({"type": "turn_complete"})
                        await websocket.send_json({"type": "assistant.state", "state": "listening"})

            try:
                async for raw_msg in gemini_ws:
                    if not alive:
                        break
                    try:
                        data = json.loads(raw_msg)

                        # Tool call
                        tc = data.get("toolCall")
                        if tc:
                            await handle_tool_call(
                                tc,
                                session_id,
                                gemini_ws,
                                websocket,
                                camera_status=camera_status,
                                frame_count=frame_count,
                            )
                            continue

                        sc = data.get("serverContent")
                        if not sc:
                            continue

                        # Audio
                        mt = sc.get("modelTurn")
                        if mt and mt.get("parts"):
                            if text_only_mode:
                                await websocket.send_json({"type": "assistant.state", "state": "thinking"})
                                for p in mt["parts"]:
                                    text_part = p.get("text")
                                    if text_part:
                                        assistant_text_part_seen = True
                                        asst_buf += text_part
                            else:
                                ai_speaking = True
                                await websocket.send_json({"type": "assistant.state", "state": "speaking"})
                                for p in mt["parts"]:
                                    idata = p.get("inlineData")
                                    if idata and idata.get("data"):
                                        await websocket.send_json({"type": "audio", "data": idata["data"]})

                        # Input transcription
                        itx = sc.get("inputTranscription")
                        if itx and itx.get("text"):
                            if text_only_mode:
                                if answer_task and not answer_task.done():
                                    answer_seq += 1
                                    answer_task.cancel()
                                    await websocket.send_json({"type": "interrupted"})
                                    await websocket.send_json({"type": "assistant.state", "state": "listening"})
                                    await websocket.send_json({"type": "status", "message": "Listening for the new question..."})
                                user_buf = _merge_voice_fragment(user_buf, itx["text"])
                            else:
                                user_buf += itx["text"]
                            nudge.user_spoke()
                            if not text_only_mode:
                                await websocket.send_json({"type": "transcription", "role": "user", "text": itx["text"]})
                            if not text_only_mode and not manual_close_seen and _manual_close_requested(user_buf):
                                manual_close_seen = True
                                await close_active_manual(session_id, websocket, gemini_ws, reason="voice request")

                        # Output transcription
                        otx = sc.get("outputTranscription")
                        if otx and otx.get("text"):
                            if text_only_mode:
                                if not assistant_text_part_seen and otx["text"].strip().upper() != "ACK":
                                    asst_buf += otx["text"]
                            else:
                                asst_buf += otx["text"]
                                await websocket.send_json({"type": "transcription", "role": "assistant", "text": otx["text"]})

                        # Grounding (Google Search)
                        gm = sc.get("groundingMetadata")
                        if gm:
                            queries = gm.get("webSearchQueries", [])
                            if queries:
                                await websocket.send_json({"type": "tool.status", "tool": "google_search", "status": "done", "queries": queries})
                                logger.info(f"[{session_id}] Google Search: {queries}")

                        if sc.get("interrupted"):
                            ai_speaking = False
                            if text_only_mode:
                                if answer_task and not answer_task.done():
                                    answer_seq += 1
                                    answer_task.cancel()
                                asst_buf = ""
                                assistant_text_part_seen = False
                            await websocket.send_json({"type": "interrupted"})
                            await websocket.send_json({"type": "assistant.state", "state": "listening"})

                        if sc.get("turnComplete"):
                            ai_speaking = False
                            logger.info(f"[{session_id}] Turn complete")
                            send_turn_complete = True
                            if text_only_mode:
                                user_text = _clean_voice_text(user_buf)
                                assistant_text = _clean_assistant_answer(asst_buf)
                                is_recitation, metrics = _is_probable_answer_recitation(user_text, last_assistant_answer)
                                is_actionable = _is_question_or_coding_request(user_text)

                                if user_text and is_recitation:
                                    logger.info(f"[{session_id}] Suppressed likely answer recitation: metrics={metrics} text={user_text[:120]!r}")
                                    await websocket.send_json({"type": "recitation.ignored"})
                                elif user_text and is_actionable:
                                    await websocket.send_json({"type": "transcription", "role": "user", "text": user_text, "final": True})
                                    await websocket.send_json({"type": "assistant.state", "state": "thinking"})
                                    await websocket.send_json({"type": "status", "message": "Answering..."})
                                    async with AsyncSessionLocal() as db:
                                        prior_turns = await SessionStore.get_turns(db, session_id, limit=12)
                                        await SessionStore.add_turn(db, session_id, "user", user_text)
                                    answer_seq += 1
                                    if answer_task and not answer_task.done():
                                        answer_task.cancel()
                                    answer_task = asyncio.create_task(answer_and_send(answer_seq, user_text, prior_turns))
                                    send_turn_complete = False
                                elif user_text:
                                    logger.info(f"[{session_id}] Suppressed non-question voice turn: {user_text[:120]!r}")
                                    await websocket.send_json({"type": "non_question.ignored"})
                            else:
                                # Persist turns
                                async with AsyncSessionLocal() as db:
                                    if user_buf.strip():
                                        await SessionStore.add_turn(db, session_id, "user", user_buf.strip())
                                    if asst_buf.strip():
                                        await SessionStore.add_turn(db, session_id, "assistant", asst_buf.strip())
                                        # Check if assistant mentioned completing a step — advance step counter
                                        step_keywords = ["next step", "step done", "move on to", "that's done", "let's proceed", "now we need to", "good, now"]
                                        lower_asst = asst_buf.lower()
                                        if any(kw in lower_asst for kw in step_keywords):
                                            sess = await SessionStore.get_session(db, session_id)
                                            if sess:
                                                new_step = (sess.current_step or 0) + 1
                                                await SessionStore.update_session(db, session_id, current_step=new_step)
                                                await websocket.send_json({"type": "step.update", "step": new_step})
                                                logger.info(f"[{session_id}] Step advanced to {new_step}")
                            if send_turn_complete:
                                await websocket.send_json({"type": "turn_complete"})
                                await websocket.send_json({"type": "assistant.state", "state": "listening"})
                            user_buf = ""
                            asst_buf = ""
                            manual_close_seen = False
                            assistant_text_part_seen = False

                    except Exception as e:
                        logger.error(f"[{session_id}] Gemini msg err: {e}")

            except websockets.exceptions.ConnectionClosedOK:
                logger.info(f"[{session_id}] Gemini closed OK")
            except websockets.exceptions.ConnectionClosedError as e:
                logger.error(f"[{session_id}] Gemini closed: {e}")
                alive = False
                try:
                    await websocket.send_json({"type": "error", "message": "Voice session disconnected."})
                except:
                    pass
            except asyncio.CancelledError:
                pass

        recv_task = asyncio.create_task(recv_gemini())

        # ─── Client → Gemini ───
        while alive:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=900)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "message": "Session timeout (15 min idle)"})
                break

            if not alive:
                break

            try:
                msg = json.loads(raw)
                t = msg.get("type")

                if t == "audio":
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE}", "data": msg["data"]}]}
                    }))

                elif t == "video":
                    if text_only_mode:
                        continue
                    frame_count += 1
                    if camera_status != "on":
                        camera_status = "on"
                        await gemini_ws.send(json.dumps({
                            "clientContent": {
                                "turns": [{"role": "user", "parts": [{"text": "[CAMERA_STATUS: on] Camera frames are now available. Use only current visible frames for visual claims."}]}],
                                "turnComplete": True,
                            }
                        }))
                    raw_b64 = msg["data"]
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": "image/jpeg", "data": raw_b64}]}
                    }))

                    # Frame-diff nudge: only trigger when the scene actually changes
                    curr_bytes = base64.b64decode(raw_b64)
                    pstate = _get_perception_state(session_id)
                    pstate["latest_frame_b64"] = raw_b64

                    if pstate["prev_frame_bytes"] and not ai_speaking:
                        diff = compute_frame_diff(pstate["prev_frame_bytes"], curr_bytes)
                        if diff >= DIFF_THRESHOLD and nudge.should_nudge("vision_check", ""):
                            # Fire perception off the main loop to avoid blocking frame intake
                            _snap_b64 = raw_b64
                            _snap_sid = session_id
                            _snap_diff = diff
                            async def _run_perception():
                                ps = _get_perception_state(_snap_sid)
                                async def _ws_send(msg):
                                    try:
                                        await websocket.send_json(msg)
                                    except Exception:
                                        pass
                                try:
                                    obs = await perceive_scene(_snap_b64, prior_obs=ps["last_perception"])
                                    if obs is None:
                                        logger.info(f"[{_snap_sid}] perception failed, skipping nudge")
                                        await _ws_send({"type": "vision.perception", "status": "failed", "diff": round(_snap_diff, 1)})
                                        return
                                    sig = observation_signature(obs)
                                    confidence = float(obs.get("confidence", 0) or 0)
                                    changes = obs.get("changed_vs_prior", []) or []
                                    safety = (obs.get("safety_concern") or "").strip()
                                    is_first_obs = ps["last_perception"] is None

                                    # Skip if low confidence or nothing new (safety always passes)
                                    # BUT: if this is the first observation, treat it as a change so AI reacts to device appearing
                                    if not safety and not is_first_obs and (confidence < 0.5 or not changes) and sig == ps["last_perception_sig"]:
                                        logger.info(f"[{_snap_sid}] perception stable (conf={confidence:.2f}), no narration")
                                        await _ws_send({
                                            "type": "vision.perception", "status": "stable",
                                            "confidence": confidence, "device_state": obs.get("device_state", ""), "diff": round(_snap_diff, 1),
                                        })
                                        return
                                    if not safety and not is_first_obs and (confidence < 0.5 or not changes):
                                        if confidence >= 0.5:
                                            ps["last_perception"] = obs
                                            ps["last_perception_sig"] = sig
                                        logger.info(f"[{_snap_sid}] perception: no new changes (conf={confidence:.2f})")
                                        await _ws_send({
                                            "type": "vision.perception", "status": "no_change",
                                            "confidence": confidence, "device_state": obs.get("device_state", ""), "diff": round(_snap_diff, 1),
                                        })
                                        return

                                    # First observation or real changes — update prior and send to Live
                                    if confidence >= 0.5:
                                        ps["last_perception"] = obs
                                        ps["last_perception_sig"] = sig

                                    # Build step context if a manual is active
                                    step_context = ""
                                    async with AsyncSessionLocal() as db:
                                        sess = await SessionStore.get_session(db, _snap_sid)
                                        if sess and sess.active_manual_id:
                                            manual = await get_manual_by_id(db, sess.active_manual_id)
                                            if manual:
                                                step_num = sess.current_step or 0
                                                teardown = manual.get("teardown", {})
                                                steps_list = teardown.get("steps", []) if isinstance(teardown, dict) else []
                                                if steps_list and step_num < len(steps_list):
                                                    current = steps_list[step_num]
                                                    step_context = f" Current manual step ({step_num+1}/{len(steps_list)}): {current.get('title','')} — {current.get('instruction','')}"

                                    obs_summary = json.dumps({
                                        "device_state": obs.get("device_state", ""),
                                        "visible_features": obs.get("visible_features", []),
                                        "changed_vs_prior": changes,
                                        "focus_area": obs.get("focus_area", ""),
                                        "safety_concern": safety,
                                    }, ensure_ascii=False)

                                    if safety:
                                        nudge_text = (
                                            f"[SAFETY_ALERT: {safety}] Look at the current frame and warn the user about this specific concern right now, in one sentence."
                                        )
                                    else:
                                        nudge_text = (
                                            f"[VISION_UPDATE] Verified facts from a grounded visual observer:\n{obs_summary}\n\n"
                                            f"Now LOOK at the current camera frame yourself.{step_context}\n"
                                            "Describe what you see in one short natural sentence — speak from your own eyes, "
                                            "but use the verified facts above as guardrails. "
                                            "If focus_area is given, direct your attention there first. "
                                            "Never contradict the verified facts. Never claim user actions (no 'removed', 'installed'); "
                                            "describe current state only. If safety_concern is non-empty, interrupt immediately about that. "
                                            "Do not read the JSON aloud — use it as reference, narrate naturally."
                                        )

                                    await gemini_ws.send(json.dumps({
                                        "clientContent": {
                                            "turns": [{"role": "user", "parts": [{"text": nudge_text}]}],
                                            "turnComplete": True
                                        }
                                    }))
                                    logger.info(f"[{_snap_sid}] VISION_UPDATE sent (conf={confidence:.2f}, changes={len(changes)})")
                                    await _ws_send({
                                        "type": "vision.perception", "status": "update_sent",
                                        "confidence": confidence, "device_state": obs.get("device_state", ""),
                                        "changes": changes, "safety_concern": safety, "diff": round(_snap_diff, 1),
                                    })
                                except Exception as e:
                                    logger.error(f"[{_snap_sid}] perception task error: {e}")

                            asyncio.create_task(_run_perception())

                    pstate["prev_frame_bytes"] = curr_bytes
                    if frame_count % 10 == 0:
                        logger.info(f"[{session_id}] Frames sent: {frame_count}")

                elif t == "text":
                    txt = msg.get("content", "")
                    if txt:
                        if not text_only_mode and _manual_close_requested(txt):
                            await close_active_manual(session_id, websocket, gemini_ws, reason="text request")
                        if not text_only_mode and (camera_status != "on" or frame_count == 0):
                            txt = (
                                "[CAMERA_STATUS: unavailable] No camera frame is currently available. "
                                "If this message asks what you see, say you can't see anything right now and ask the user to turn on the camera or check camera access in settings. "
                                "Do not guess.\n\n"
                                f"USER MESSAGE: {txt}"
                            )
                        await gemini_ws.send(json.dumps({
                            "clientContent": {"turns": [{"role": "user", "parts": [{"text": txt}]}], "turnComplete": True}
                        }))

                elif t == "manual.close":
                    if not text_only_mode:
                        await close_active_manual(session_id, websocket, gemini_ws, reason="client request")

                elif t == "camera_status":
                    if text_only_mode:
                        continue
                    state = msg.get("state", "unavailable")
                    reason = msg.get("reason", "")
                    if state not in {"on", "off", "unavailable"}:
                        state = "unavailable"
                    camera_status = state
                    status_text = (
                        f"[CAMERA_STATUS: {state}] "
                        "The camera feed is not visible to you. "
                        "If the user asks what you see, say you can't see anything right now and ask them to turn on the camera or check camera access in settings. "
                        "Do not guess."
                    )
                    if state == "on":
                        status_text = "[CAMERA_STATUS: on] Camera frames are available. Only describe what is visible in the current frame."
                    if reason:
                        status_text += f" Reason: {reason}"
                    await gemini_ws.send(json.dumps({
                        "clientContent": {"turns": [{"role": "user", "parts": [{"text": status_text}]}], "turnComplete": True}
                    }))

                elif t == "heartbeat":
                    await websocket.send_json({"type": "heartbeat"})

                elif t == "end":
                    intentional_end = True
                    logger.info(f"[{session_id}] Client ended session intentionally")
                    break

            except websockets.exceptions.ConnectionClosed:
                alive = False
                await websocket.send_json({"type": "error", "message": "Voice session disconnected."})
                break
            except Exception as e:
                logger.error(f"[{session_id}] Fwd err: {e}")
                if "closed" in str(e).lower():
                    alive = False
                    break

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] Client disconnected")
    except asyncio.TimeoutError:
        logger.info("Config timeout")
    except Exception as e:
        logger.error(f"[{session_id}] Session err: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        alive = False
        if recv_task:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
        if answer_task:
            answer_task.cancel()
            try:
                await answer_task
            except asyncio.CancelledError:
                pass
        if gemini_ws:
            try:
                await gemini_ws.close()
            except:
                pass
        # Save snapshot on disconnect
        if session_id:
            try:
                async with AsyncSessionLocal() as db:
                    state = await SessionStore.get_session_state(db, session_id)
                    if state:
                        import uuid
                        snap = SessionSnapshot(id=str(uuid.uuid4()), session_id=session_id, state_data=state)
                        db.add(snap)
                        await db.commit()
                        logger.info(f"[{session_id}] Snapshot saved")
                    # Only mark session as ended if user intentionally clicked End
                    if intentional_end:
                        await SessionStore.end_session(db, session_id)
                        logger.info(f"[{session_id}] Session ended (intentional)")
                    else:
                        logger.info(f"[{session_id}] Session kept active for reconnect")
            except:
                pass
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"[{session_id}] Cleaned up (frames={frame_count})")
        if intentional_end and session_id:
            _clear_perception_state(session_id)


async def handle_tool_call(
    tc,
    session_id,
    gemini_ws,
    client_ws,
    *,
    camera_status: str = "unavailable",
    frame_count: int = 0,
):
    calls = tc.get("functionCalls", [])
    responses = []
    for call in calls:
        fn = call.get("name")
        fid = call.get("id")
        args = call.get("args", {})
        logger.info(f"[{session_id}] Tool: {fn}({json.dumps(args)[:100]})")
        await client_ws.send_json({"type": "tool.status", "tool": fn, "status": "running", "args": args})

        result = {}
        if fn == "lookup_manual":
            grounded, ground_reason = await _confirm_manual_lookup_grounding(
                args,
                session_id,
                camera_status=camera_status,
                frame_count=frame_count,
            )
            if not grounded:
                logger.warning(f"[{session_id}] lookup_manual blocked: {ground_reason}; args={args}")
                result = {
                    "found_count": 0,
                    "selected_manual_id": None,
                    "manual_summary": "Manual not opened — device was not confirmed.",
                    "blocked": True,
                    "block_reason": ground_reason,
                    "no_manual_instruction": (
                        "Do not open a manual yet. Ask the user to show the device more clearly, "
                        "bring the label/model number closer to the camera, or say the model number "
                        "while keeping the device on camera. Do not guess the device."
                    ),
                }
            else:
                async with AsyncSessionLocal() as db:
                    result = await lookup_manual_tool(db, session_id, brand=args.get("brand", ""), model=args.get("model", ""), device_type=args.get("device_type", ""), issue=args.get("issue", ""), query=args.get("query", ""))
                    if result.get("selected_manual_id"):
                        await SessionStore.update_session(db, session_id, active_manual_id=result["selected_manual_id"], active_device_type=args.get("device_type", ""), active_device_model=args.get("model", ""))
                    else:
                        # No manual found — short instruction for Gemini
                        result["no_manual_instruction"] = "No manual found. Tell the user briefly: 'No manual for this one — I'll use general knowledge and web search.' Then help using your training and Google Search."
        else:
            result = {"error": f"Unknown tool: {fn}"}

        responses.append({"id": fid, "name": fn, "response": result})

        # Send different UI status based on whether manual was found
        if result.get("selected_manual_id"):
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": result.get("manual_summary", ""),
                "manual_id": result.get("selected_manual_id"),
                "warnings": result.get("warnings", []),
                "steps": result.get("troubleshooting_steps", [])[:5],
            })
        else:
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": result.get("manual_summary") or "No manual found — using general knowledge",
                "manual_id": None,
                "warnings": [],
                "steps": [],
                "blocked": result.get("blocked", False),
            })

    await gemini_ws.send(json.dumps({
        "toolResponse": {"functionResponses": [{"id": r["id"], "name": r["name"], "response": r["response"]} for r in responses]}
    }))
    logger.info(f"[{session_id}] Tool response → Gemini")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8001")))
