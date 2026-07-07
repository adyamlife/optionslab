"""AI per-ticker trade assessment.

Calls the configured primary provider (default: Groq / Llama 3.3 70B) with a
structured prompt summarising a ticker's technical signals, IV environment, news
sentiment, and the rulebook-recommended option structure.  Falls back to the
secondary provider (default: Gemini 2.0 Flash Lite) if the primary fails or has
no API key configured.

Provider config  → config/settings.toml  [ai]
API keys         → config/secrets.toml   [api_keys]   ← edit this file
"""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path

import tomllib

_CONFIG_PATH  = Path(__file__).parent.parent / "config" / "settings.toml"
_SECRETS_PATH = Path(__file__).parent.parent / "config" / "secrets.toml"

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_ai_config():
    with open(_CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f).get("ai", {})
    secrets = {}
    if _SECRETS_PATH.exists():
        with open(_SECRETS_PATH, "rb") as f:
            secrets = tomllib.load(f).get("api_keys", {})
    return cfg, secrets


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(ticker, row):
    headlines = "; ".join((row.get("news_headlines") or [])[:3]) or "none available"
    notes     = "; ".join(row.get("signal_notes") or []) or "none"
    return (
        f"You are an expert options trader. Analyse ticker {ticker} given these signals:\n"
        f"- Spot: ${row.get('spot')}, Daily trend: {row.get('trend')}, "
        f"Weekly trend: {row.get('weekly_trend')}\n"
        f"- RSI: {row.get('rsi')}, MACD: {row.get('macd_trend')} (hist {row.get('macd_hist')})\n"
        f"- IV environment: {row.get('iv_env')} (IV rank proxy {row.get('iv_rank_proxy')}%)\n"
        f"- Signal alignment: {row.get('signal_rating')} — {notes}\n"
        f"- News sentiment: {row.get('news_sentiment')} | Recent headlines: {headlines}\n"
        f"- Rulebook-recommended structure: {row.get('recommended_structure')}\n\n"
        f"In 2-3 sentences assess whether {row.get('recommended_structure')} is sound for "
        f"{ticker} right now, noting the single biggest risk. "
        f"End your response with exactly one word on its own line: HIGH, MEDIUM, or LOW "
        f"(your confidence the trade will be profitable)."
    )


# ---------------------------------------------------------------------------
# Provider implementations (raw urllib — no extra packages required)
# ---------------------------------------------------------------------------

_HEADERS_BASE = {
    "Content-Type": "application/json",
    "User-Agent": "python-options-lab/1.0",
}


def _call_groq(api_key, model, prompt, timeout=15):
    url  = "https://api.groq.com/openai/v1/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.3,
    }).encode()
    headers = {**_HEADERS_BASE, "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def _call_gemini(api_key, model, prompt, timeout=15, retries=2):
    url  = (f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}")
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req  = urllib.request.Request(url, data=body, headers=_HEADERS_BASE)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise


_PROVIDERS = {
    "groq":   _call_groq,
    "gemini": _call_gemini,
}


# ---------------------------------------------------------------------------
# Parse the model response into structured fields
# ---------------------------------------------------------------------------

def _parse_response(text):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    confidence = "MEDIUM"
    for line in reversed(lines):
        if line.upper() in ("HIGH", "MEDIUM", "LOW"):
            confidence = line.upper()
            lines = lines[: lines.index(line)]
            break
    assessment = " ".join(lines).strip()
    return assessment, confidence


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_ai_assessment(ticker, row):
    """Return a dict with keys: provider, model, assessment, confidence, error.
    Returns immediately with error set if no API keys are configured."""
    cfg, secrets = _load_ai_config()

    primary_provider  = cfg.get("primary_provider",  "groq")
    primary_model     = cfg.get("primary_model",     "llama-3.3-70b-versatile")
    fallback_provider = cfg.get("fallback_provider", "gemini")
    fallback_model    = cfg.get("fallback_model",    "gemini-2.0-flash-lite")

    attempts = [
        (primary_provider,  primary_model,  secrets.get(primary_provider,  "")),
        (fallback_provider, fallback_model, secrets.get(fallback_provider, "")),
    ]

    prompt = _build_prompt(ticker, row)
    errors = []

    for provider, model, api_key in attempts:
        if not api_key:
            errors.append(f"{provider}: no API key")
            continue
        call_fn = _PROVIDERS.get(provider)
        if not call_fn:
            errors.append(f"unknown provider '{provider}'")
            continue
        try:
            text = call_fn(api_key, model, prompt)
            assessment, confidence = _parse_response(text)
            return {
                "provider": provider, "model": model,
                "assessment": assessment, "confidence": confidence,
                "error": None,
            }
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:120]
            except Exception:
                pass
            errors.append(f"{provider} HTTP {e.code}: {e.reason} {body}".strip())
        except Exception as e:
            errors.append(f"{provider}: {e}")

    return {
        "provider": None, "model": None,
        "assessment": None, "confidence": None,
        "error": " | ".join(errors),
    }
