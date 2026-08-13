"""LLM provider configuration.

Defaults live here. The Streamlit UI can override the active provider/model at
runtime via set_provider(); overrides are persisted to runtime_config.json so
the CLI (pipeline.py) and the app stay in sync.
"""

import json
from pathlib import Path

PROVIDERS = ("gemini", "ollama")

DEFAULT_PROVIDER = "gemini"

# Default model per provider. Overridable from the UI.
DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "ollama": "gemma4:31b",
}

# Which env var holds the API key for each provider.
ENV_VARS = {
    "gemini": "GEMINI_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}

# Per-provider request timeout in seconds, used for both real calls and the
# health check. Ollama cloud can stall indefinitely, so keep it bounded.
TIMEOUTS = {
    "gemini": 120,
    "ollama": 120,
}

HEALTH_TIMEOUT = 20  # seconds — health check fails fast, unlike a real call

_OVERRIDE_PATH = Path(__file__).with_name("runtime_config.json")

PROVIDER = DEFAULT_PROVIDER
MODELS = dict(DEFAULT_MODELS)


def _load_overrides() -> None:
    global PROVIDER
    try:
        saved = json.loads(_OVERRIDE_PATH.read_text())
    except (OSError, ValueError):
        return
    if saved.get("provider") in PROVIDERS:
        PROVIDER = saved["provider"]
    for name, model in (saved.get("models") or {}).items():
        if name in PROVIDERS and model:
            MODELS[name] = model


def set_provider(provider: str, model: str | None = None, persist: bool = True) -> None:
    """Switch the active provider (and optionally its model) for this process."""
    global PROVIDER
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    PROVIDER = provider
    if model:
        MODELS[provider] = model
    if persist:
        try:
            _OVERRIDE_PATH.write_text(
                json.dumps({"provider": PROVIDER, "models": MODELS}, indent=2)
            )
        except OSError:
            pass  # read-only fs — runtime switch still applies for this process


def model(provider: str | None = None) -> str:
    return MODELS[provider or PROVIDER]


def env_var(provider: str | None = None) -> str:
    return ENV_VARS[provider or PROVIDER]


def timeout(provider: str | None = None) -> int:
    return TIMEOUTS[provider or PROVIDER]


_load_overrides()
