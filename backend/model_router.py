import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx


DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_OLLAMA_BASE = "http://localhost:11434"


@dataclass
class ModelProviderConfig:
    provider: str
    api_base: str
    api_key: str
    model_id: str
    vision_model_id: str


def get_provider_profiles() -> List[Dict[str, Any]]:
    return [
        {
            "id": "gemini_live",
            "name": "Gemini Live",
            "default_model": os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-latest"),
            "default_api_base": "",
            "requires_api_key": True,
            "capabilities": provider_capabilities("gemini_live"),
        },
        {
            "id": "openai_compatible",
            "name": "OpenAI-compatible",
            "default_model": os.getenv("MODEL_ID", "gpt-4o-mini"),
            "default_api_base": os.getenv("MODEL_API_BASE", DEFAULT_OPENAI_BASE),
            "requires_api_key": False,
            "capabilities": provider_capabilities("openai_compatible"),
        },
        {
            "id": "ollama",
            "name": "Ollama",
            "default_model": os.getenv("OLLAMA_MODEL", "llava"),
            "default_api_base": os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE),
            "requires_api_key": False,
            "capabilities": provider_capabilities("ollama"),
        },
    ]


def provider_capabilities(provider: str) -> Dict[str, Any]:
    if provider == "gemini_live":
        return {
            "audio_input": "native_stream",
            "audio_output": "native_stream",
            "vision": "native_frames",
            "tool_calling": "native",
            "streaming_text": True,
            "local": False,
        }
    if provider == "ollama":
        return {
            "audio_input": "none",
            "audio_output": "none",
            "vision": "image_prompt",
            "tool_calling": "json_instruction",
            "streaming_text": False,
            "local": True,
        }
    return {
        "audio_input": "none",
        "audio_output": "none",
        "vision": "image_prompt",
        "tool_calling": "json_instruction",
        "streaming_text": False,
        "local": False,
    }


def resolve_model_provider_config(cfg: Dict[str, Any]) -> ModelProviderConfig:
    provider = (cfg.get("model_provider") or os.getenv("ASSISTANT_PROVIDER") or "gemini_live").strip()
    if provider not in {"gemini_live", "openai_compatible", "ollama"}:
        provider = "gemini_live"

    if provider == "ollama":
        default_base = os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE)
        default_model = os.getenv("OLLAMA_MODEL", "llava")
        default_vision = os.getenv("OLLAMA_VISION_MODEL", default_model)
    elif provider == "openai_compatible":
        default_base = os.getenv("MODEL_API_BASE", DEFAULT_OPENAI_BASE)
        default_model = os.getenv("MODEL_ID", "gpt-4o-mini")
        default_vision = os.getenv("VISION_MODEL_ID", default_model)
    else:
        default_base = ""
        default_model = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-latest")
        default_vision = default_model

    return ModelProviderConfig(
        provider=provider,
        api_base=(cfg.get("model_api_base") or default_base).strip().rstrip("/"),
        api_key=(cfg.get("model_api_key") or os.getenv("GOOGLE_API_KEY" if provider == "gemini_live" else "MODEL_API_KEY") or "").strip(),
        model_id=(cfg.get("model_id") or default_model).strip(),
        vision_model_id=(cfg.get("vision_model_id") or default_vision).strip(),
    )


def public_provider_state(config: ModelProviderConfig) -> Dict[str, Any]:
    return {
        "provider": config.provider,
        "api_base": config.api_base,
        "model_id": config.model_id,
        "vision_model_id": config.vision_model_id,
        "has_api_key": bool(config.api_key),
        "capabilities": provider_capabilities(config.provider),
    }


def _join_url(base: str, path: str) -> str:
    normalized = base.rstrip("/") + "/"
    return urljoin(normalized, path.lstrip("/"))


def _openai_chat_url(base: str) -> str:
    if base.rstrip("/").endswith("/chat/completions"):
        return base.rstrip("/")
    if base.rstrip("/").endswith("/v1"):
        return _join_url(base, "chat/completions")
    return _join_url(base, "v1/chat/completions")


async def call_model_once(
    config: ModelProviderConfig,
    system_prompt: str,
    user_text: str,
    image_b64: Optional[str] = None,
    timeout: float = 45.0,
) -> str:
    if config.provider == "ollama":
        return await _call_ollama(config, system_prompt, user_text, image_b64=image_b64, timeout=timeout)
    if config.provider == "openai_compatible":
        return await _call_openai_compatible(config, system_prompt, user_text, image_b64=image_b64, timeout=timeout)
    raise ValueError(f"Unsupported HTTP model provider: {config.provider}")


async def _call_ollama(
    config: ModelProviderConfig,
    system_prompt: str,
    user_text: str,
    image_b64: Optional[str] = None,
    timeout: float = 45.0,
) -> str:
    model = config.vision_model_id if image_b64 and config.vision_model_id else config.model_id
    user_message: Dict[str, Any] = {"role": "user", "content": user_text}
    if image_b64:
        user_message["images"] = [image_b64]
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            user_message,
        ],
        "options": {"temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(_join_url(config.api_base or DEFAULT_OLLAMA_BASE, "api/chat"), json=payload)
        response.raise_for_status()
    data = response.json()
    return ((data.get("message") or {}).get("content") or data.get("response") or "").strip()


async def _call_openai_compatible(
    config: ModelProviderConfig,
    system_prompt: str,
    user_text: str,
    image_b64: Optional[str] = None,
    timeout: float = 45.0,
) -> str:
    model = config.vision_model_id if image_b64 and config.vision_model_id else config.model_id
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    content: Any = user_text
    if image_b64:
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(_openai_chat_url(config.api_base or DEFAULT_OPENAI_BASE), headers=headers, json=payload)
        response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
    return str(content).strip()


def extract_tool_call(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    object_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        tool = parsed.get("tool_call") if isinstance(parsed, dict) else None
        if isinstance(tool, dict) and tool.get("name"):
            return tool
    return None


def strip_tool_json(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"```(?:json)?\s*\{.*?\"tool_call\".*?\}\s*```", "", text, flags=re.DOTALL)
    text = re.sub(r"\{[^{}]*\"tool_call\".*\}", "", text, flags=re.DOTALL)
    return text.strip()
