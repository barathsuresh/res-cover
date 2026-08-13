import json
import os
import re
import time

from dotenv import load_dotenv
import config
import jsonschema

load_dotenv()

_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_API_RETRIES = 5
_BACKOFF_BASE    = 2  # seconds

SYSTEM_PROMPT = (
    "You are a senior technical recruiter and hiring manager with 15 years of experience "
    "hiring software engineers at top-tier tech companies. "
    "Your job is to rewrite this candidate's resume so it passes ATS filters AND impresses a human recruiter in 6 seconds. "
    "You know exactly what makes a resume get tossed vs. get a callback. "
    "Rules: "
    "- Replace weak responsibility statements with measurable wins. "
    "'Worked on X' becomes 'Reduced X by Y% by doing Z.' "
    "- Use strong, specific action verbs: Engineered, Eliminated, Accelerated, Reduced, Scaled, Shipped — "
    "not Worked, Assisted, Helped, Participated. "
    "- Every bullet must pass the 'so what?' test — if you can't answer why it matters, rewrite it. "
    "- ATS: mirror exact keywords and phrases from the JD naturally throughout the resume. "
    "- Never invent entire roles, companies, or degrees that do not exist. "
    "- Light embellishment is allowed: round up metrics conservatively, "
    "extrapolate adjacent technologies the candidate clearly worked with, "
    "frame accomplishments at their strongest honest interpretation. "
    "Extend logically from what exists — do not invent from nothing. Examples: "
    "FastAPI → async Python, ASGI, Pydantic, REST design; "
    "MQTT + FastAPI → IoT pipelines, real-time telemetry; "
    "Redis Lua → distributed systems, cache design; "
    "Spring Security + OAuth 2.0 → IAM, identity management; "
    "Docker Compose → container orchestration; "
    "Zipkin + Prometheus → observability, SRE. "
    "If a JD heavily features any technology present anywhere in the resume, surface and expand it — "
    "do not let relevant experience stay buried. "
    "- Cover letter must feel like it was written by a real person, not generated. "
    "Use the style sample as tone reference only. "
    "Write with a natural human voice: confident, direct, specific to this company and role. "
    "- Return only valid JSON matching the provided schema, no markdown, no explanation."
)


def _api_key(provider: str) -> str:
    var = config.env_var(provider)
    api_key = os.getenv(var, "")
    if not api_key:
        raise RuntimeError(f"Missing required environment variable: {var}")
    return api_key


def _retry_wait(attempt: int, reason: str, attempts: int) -> None:
    wait = _BACKOFF_BASE ** attempt
    print(f"  [llm] {reason} — retrying in {wait}s (attempt {attempt + 1}/{attempts})...")
    time.sleep(wait)


def _call_ollama(system: str, user: str, model: str, timeout: int, attempts: int) -> str:
    import httpx
    from ollama import Client, ResponseError

    client = Client(
        host="https://ollama.com",
        headers={"Authorization": "Bearer " + _api_key("ollama")},
        timeout=timeout,
    )

    for attempt in range(attempts):
        try:
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            )
            return response.message.content
        except ResponseError as e:
            code = getattr(e, "status_code", None)
            if code not in _RETRYABLE_CODES or attempt == attempts - 1:
                raise
            _retry_wait(attempt, f"API error {code}", attempts)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == attempts - 1:
                raise
            _retry_wait(attempt, f"{type(e).__name__} after {timeout}s", attempts)


def _call_gemini(system: str, user: str, model: str, timeout: int, attempts: int) -> str:
    import httpx
    from google import genai
    from google.genai import types, errors as genai_errors

    client = genai.Client(
        api_key=_api_key("gemini"),
        http_options=types.HttpOptions(timeout=timeout * 1000),  # SDK wants ms
    )

    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=model,
                contents=user,
                config=types.GenerateContentConfig(system_instruction=system),
            )
            return response.text
        except (genai_errors.ServerError, genai_errors.ClientError) as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if code not in _RETRYABLE_CODES or attempt == attempts - 1:
                raise
            _retry_wait(attempt, f"API error {code}", attempts)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == attempts - 1:
                raise
            _retry_wait(attempt, f"{type(e).__name__} after {timeout}s", attempts)


def _call_provider(
    system: str,
    user: str,
    provider: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
    attempts: int = _MAX_API_RETRIES,
) -> str:
    provider = provider or config.PROVIDER
    model    = model or config.model(provider)
    timeout  = timeout or config.timeout(provider)

    if provider == "ollama":
        return _call_ollama(system, user, model, timeout, attempts)
    if provider == "gemini":
        return _call_gemini(system, user, model, timeout, attempts)
    raise ValueError(f"Unsupported provider: {provider}")


def _list_ollama_models() -> list[str]:
    from ollama import Client

    client = Client(
        host="https://ollama.com",
        headers={"Authorization": "Bearer " + _api_key("ollama")},
        timeout=config.HEALTH_TIMEOUT,
    )
    return [m.model for m in client.list().models]


def _list_gemini_models() -> list[str]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=_api_key("gemini"),
        http_options=types.HttpOptions(timeout=config.HEALTH_TIMEOUT * 1000),
    )
    return [
        m.name.removeprefix("models/")
        for m in client.models.list()
        if "generateContent" in (m.supported_actions or [])
    ]


def list_models(provider: str | None = None) -> list[str]:
    """Live model list from the provider. Raises on failure — caller decides."""
    provider = provider or config.PROVIDER
    if provider == "ollama":
        models = _list_ollama_models()
    elif provider == "gemini":
        models = _list_gemini_models()
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    return sorted(models)


def health_check(provider: str | None = None, model: str | None = None) -> dict:
    """Fire one tiny request at the provider. Never raises — returns a report.

    Used by the Streamlit sidebar so a dead provider is visible before a
    pipeline run burns minutes on timeouts.
    """
    provider = provider or config.PROVIDER
    model    = model or config.model(provider)
    report   = {"provider": provider, "model": model, "ok": False,
                "latency_s": None, "detail": ""}

    start = time.time()
    try:
        raw = _call_provider(
            "Reply with JSON only.",
            'Return exactly {"ok":true}',
            provider=provider,
            model=model,
            timeout=config.HEALTH_TIMEOUT,
            attempts=1,
        )
        report["latency_s"] = round(time.time() - start, 2)
        report["ok"]     = True
        report["detail"] = (raw or "").strip()[:120]
    except Exception as e:
        report["latency_s"] = round(time.time() - start, 2)
        report["detail"]    = f"{type(e).__name__}: {e}"[:300]
    return report


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return json.loads(cleaned)


_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["company", "role", "resume_data", "cover_letter_text"],
    "properties": {
        "company": {"type": "string"},
        "role": {"type": "string"},
        "resume_data": {
            "type": "object",
            "required": ["name", "contact", "education", "skills", "experience", "projects"],
            "properties": {
                "name": {"type": "string"},
                "contact": {"type": "string"},
                "summary": {"type": ["string", "null"]},
                "education": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["school", "location", "degree", "dates", "extra"],
                        "properties": {
                            "school": {"type": "string"},
                            "location": {"type": "string"},
                            "degree": {"type": "string"},
                            "dates": {"type": "string"},
                            "extra": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
                },
                "experience": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["company", "role", "location", "dates", "bullets"],
                        "properties": {
                            "company": {"type": "string"},
                            "role": {"type": "string"},
                            "location": {"type": "string"},
                            "dates": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "projects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["title", "link", "link_url", "tech", "bullets"],
                        "properties": {
                            "title": {"type": "string"},
                            "link": {"type": "string"},
                            "link_url": {"type": "string"},
                            "tech": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "open_source": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["title", "link", "link_url", "tech", "bullets"],
                        "properties": {
                            "title": {"type": "string"},
                            "link": {"type": "string"},
                            "link_url": {"type": "string"},
                            "tech": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "cover_letter_text": {"type": "string"},
    },
}


def call_llm(system: str, user: str, schema: dict | None = _OUTPUT_SCHEMA) -> dict:
    last_error = None
    current_user = user

    for attempt in range(3):
        raw = _call_provider(system, current_user)
        try:
            parsed = _parse_json(raw)
            if schema is not None:
                jsonschema.validate(parsed, schema)
            return parsed
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            last_error = e
            current_user = (
                user
                + "\n\nYour previous response was not valid JSON or did not match the required schema. "
                "Return ONLY the JSON object — no markdown fences, no explanation, nothing else."
            )

    raise ValueError(f"LLM returned invalid JSON after 3 attempts: {last_error}")
