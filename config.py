"""LLM provider configuration.

Defaults live here. The Streamlit UI can override the active provider/model at
runtime via set_provider(); overrides are persisted to runtime_config.json so
the CLI (pipeline.py) and the app stay in sync.
"""

import json
from pathlib import Path

PROVIDERS = ("gemini", "ollama", "nvidia")

DEFAULT_PROVIDER = "gemini"

# Default model per provider. Overridable from the UI.
DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "ollama": "nemotron-3-ultra",
    "nvidia": "openai/gpt-oss-120b",
}

# Which env var holds the API key for each provider.
ENV_VARS = {
    "gemini": "GEMINI_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}

# Per-provider request timeout in seconds, used for both real calls and the
# health check. Ollama cloud can stall indefinitely, so keep it bounded.
TIMEOUTS = {
    "gemini": 120,
    "ollama": 120,
    "nvidia": 120,
}

HEALTH_TIMEOUT = 20  # seconds — health check fails fast, unlike a real call

DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Downloads")

# When a file of the same name already sits in the download folder: True
# replaces it, False keeps it and saves alongside as "name (1).pdf".
DEFAULT_OVERWRITE_DOWNLOADS = True

_OVERRIDE_PATH = Path(__file__).with_name("runtime_config.json")

PROVIDER = DEFAULT_PROVIDER
MODELS = dict(DEFAULT_MODELS)
DOWNLOAD_DIR = DEFAULT_DOWNLOAD_DIR
OVERWRITE_DOWNLOADS = DEFAULT_OVERWRITE_DOWNLOADS


def _write_overrides() -> None:
    try:
        _OVERRIDE_PATH.write_text(
            json.dumps(
                {
                    "provider": PROVIDER,
                    "models": MODELS,
                    "download_dir": DOWNLOAD_DIR,
                    "overwrite_downloads": OVERWRITE_DOWNLOADS,
                },
                indent=2,
            )
        )
    except OSError:
        pass  # read-only fs — runtime switch still applies for this process


def _load_overrides() -> None:
    global PROVIDER, DOWNLOAD_DIR, OVERWRITE_DOWNLOADS
    try:
        saved = json.loads(_OVERRIDE_PATH.read_text())
    except (OSError, ValueError):
        return
    if saved.get("provider") in PROVIDERS:
        PROVIDER = saved["provider"]
    for name, model in (saved.get("models") or {}).items():
        if name in PROVIDERS and model:
            MODELS[name] = model
    if saved.get("download_dir"):
        DOWNLOAD_DIR = saved["download_dir"]
    if isinstance(saved.get("overwrite_downloads"), bool):
        OVERWRITE_DOWNLOADS = saved["overwrite_downloads"]


def set_overwrite_downloads(overwrite: bool, persist: bool = True) -> None:
    """Choose whether saving a PDF replaces a same-named file or keeps both."""
    global OVERWRITE_DOWNLOADS
    OVERWRITE_DOWNLOADS = bool(overwrite)
    if persist:
        _write_overrides()


def set_download_dir(path: str, persist: bool = True) -> None:
    """Switch where downloaded PDFs get saved. Persisted alongside provider settings."""
    global DOWNLOAD_DIR
    DOWNLOAD_DIR = path
    if persist:
        _write_overrides()


def set_provider(provider: str, model: str | None = None, persist: bool = True) -> None:
    """Switch the active provider (and optionally its model) for this process."""
    global PROVIDER
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    PROVIDER = provider
    if model:
        MODELS[provider] = model
    if persist:
        _write_overrides()


def model(provider: str | None = None) -> str:
    return MODELS[provider or PROVIDER]


def env_var(provider: str | None = None) -> str:
    return ENV_VARS[provider or PROVIDER]


def timeout(provider: str | None = None) -> int:
    return TIMEOUTS[provider or PROVIDER]


_load_overrides()
