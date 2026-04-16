"""Two-stage perception: Gemini 2.5 Flash as a cold visual observer.
Produces grounded JSON facts. No inference about user actions, no narration."""
import os
import json
import logging
import httpx
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

FLASH_MODEL = "gemini-2.5-flash"

def _get_flash_url():
    key = os.environ.get('GOOGLE_API_KEY', '')
    return f"https://generativelanguage.googleapis.com/v1beta/models/{FLASH_MODEL}:generateContent?key={key}"

PERCEPTION_PROMPT = """You are a cold visual observer. You do NOT converse. You ONLY describe what is literally visible in the given camera frame.

Output STRICT JSON, no prose, no markdown fences:
{
  "objects": [string, ...],           // concrete items visible — e.g. "Logitech mouse", "AA battery", "Phillips screwdriver", "exposed PCB"
  "device_state": string,             // one short sentence describing the CURRENT visible state of the main device — e.g. "battery cover open, one AA battery still seated"
  "visible_features": [string, ...],  // specific features you can point to — e.g. "two metal contacts exposed in empty slot", "red LED off", "one screw hole empty"
  "changed_vs_prior": [string, ...],  // ONLY facts that changed vs the prior observation — empty list if identical. Each entry must cite a visible feature, not infer an action.
  "confidence": float,                // 0.0 to 1.0 — how confidently you can see the main subject. Low if blurry / hand blocking / dark.
  "safety_concern": string            // empty string unless you see bare wires, sparks, liquid, blade near fingers, etc.
}

RULES — ABSOLUTE:
- Report ONLY what is literally visible in THIS frame.
- Never infer user actions. Write "battery compartment now empty with contacts exposed", NOT "user removed the battery".
- If the frame is blurry, obscured, or dark, set confidence low and leave changed_vs_prior empty.
- If a region that was visible before is now hidden by a hand, do NOT treat that as "removed" — leave changed_vs_prior empty for that object.
- JSON only. No prose. No markdown."""


async def perceive_scene(frame_b64: str, prior_obs: Optional[Dict[str, Any]] = None, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    """Run one Flash text call against the current frame. Returns parsed JSON dict or None on failure."""
    if not os.environ.get('GOOGLE_API_KEY', ''):
        logger.warning("perceive_scene: no API key")
        return None

    prior_text = ""
    if prior_obs:
        prior_text = f"\n\nPRIOR OBSERVATION (compare against this for changed_vs_prior):\n{json.dumps(prior_obs, ensure_ascii=False)}"

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": PERCEPTION_PROMPT + prior_text},
                {"inline_data": {"mime_type": "image/jpeg", "data": frame_b64}},
            ],
        }],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(_get_flash_url(), json=payload)
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return parsed
    except httpx.TimeoutException:
        logger.warning("perceive_scene: Flash timed out")
        return None
    except Exception as e:
        logger.error(f"perceive_scene failed: {e}")
        return None


def observation_signature(obs: Dict[str, Any]) -> str:
    """Stable fingerprint for dedup — ignores confidence and empty-change observations."""
    if not obs:
        return ""
    return json.dumps({
        "objects": sorted(obs.get("objects", [])),
        "device_state": obs.get("device_state", ""),
        "visible_features": sorted(obs.get("visible_features", [])),
    }, ensure_ascii=False)
