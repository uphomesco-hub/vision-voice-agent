from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from model_router import extract_tool_call, provider_capabilities, resolve_model_provider_config


def test_resolve_ollama_provider_config_uses_client_values(monkeypatch):
    monkeypatch.delenv("ASSISTANT_PROVIDER", raising=False)
    config = resolve_model_provider_config({
        "model_provider": "ollama",
        "model_api_base": "http://localhost:11434/",
        "model_id": "llava",
        "vision_model_id": "llava:13b",
    })

    assert config.provider == "ollama"
    assert config.api_base == "http://localhost:11434"
    assert config.model_id == "llava"
    assert config.vision_model_id == "llava:13b"
    assert provider_capabilities(config.provider)["vision"] == "image_prompt"


def test_extract_tool_call_from_json_response():
    tool = extract_tool_call(
        '{"tool_call":{"name":"highlight","args":{"operation":"create","target_hint":"left screw"}}}'
    )

    assert tool["name"] == "highlight"
    assert tool["args"]["target_hint"] == "left screw"


def test_invalid_provider_falls_back_to_gemini(monkeypatch):
    monkeypatch.delenv("ASSISTANT_PROVIDER", raising=False)
    config = resolve_model_provider_config({"model_provider": "unknown"})

    assert config.provider == "gemini_live"
