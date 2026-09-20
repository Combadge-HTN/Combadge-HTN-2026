"""Load local configuration without changing the environment or exposing secrets."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

# Immutable, public capability declaration; no credentials or payment support.
SHOPIFY_PROFILE_URL = (
    "https://cdn.jsdelivr.net/gh/Combadge-HTN/Combadge-HTN-2026@5279994/docs/ucp-agent.json"
)


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = field(default="", repr=False)
    speechmatics_api_key: str = field(default="", repr=False)
    browserbase_api_key: str = field(default="", repr=False)
    browserbase_project_id: str = field(default="", repr=False)
    composio_api_key: str = field(default="", repr=False)
    composio_user_id: str = field(default="", repr=False)
    composio_accounts: dict[str, str] = field(default_factory=dict, repr=False)
    speaker_references: str = field(default="", repr=False)
    speaker_backend: str = "auto"
    speaker_worker: str = "build/speaker/speaker-worker"
    speaker_model: str = "models/campplus.onnx"
    echo_mode: str = "off"
    echo_delay_ms: str = ""
    echo_library: str = ""
    timezone: str = "America/Toronto"
    live_model: str = "gpt-live-1"
    live_voice: str = "marin"
    backend_model: str = "gpt-5.6-luna"
    shopify_agent_profile_url: str = SHOPIFY_PROFILE_URL
    shopify_country: str = "CA"
    shopify_currency: str = "CAD"
    shopify_merchant_domain: str = ""


def load_settings(env_file: Path = Path(".env")) -> Settings:
    """Read the chosen file; exported environment values take precedence."""
    values = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}

    def value(name: str) -> str:
        return (os.environ.get(name, values.get(name)) or "").strip()

    return Settings(
        openai_api_key=value("OPENAI_API_KEY"),
        speechmatics_api_key=value("SPEECHMATICS_API_KEY"),
        browserbase_api_key=value("BROWSERBASE_API_KEY"),
        browserbase_project_id=value("BROWSERBASE_PROJECT_ID"),
        composio_api_key=value("COMPOSIO_API_KEY"),
        composio_user_id=value("COMPOSIO_USER_ID"),
        composio_accounts={
            app: account
            for app in ("gmail", "googlecalendar", "shopify")
            if (account := value(f"COMPOSIO_{app.upper()}_ACCOUNT_ID"))
        },
        speaker_references=value("COMBADGE_SPEAKERS"),
        speaker_backend=value("COMBADGE_SPEAKER_BACKEND") or "auto",
        speaker_worker=value("COMBADGE_SPEAKER_WORKER") or "build/speaker/speaker-worker",
        speaker_model=value("COMBADGE_SPEAKER_MODEL") or "models/campplus.onnx",
        echo_mode=value("COMBADGE_AEC") or "off",
        echo_delay_ms=value("COMBADGE_AEC_DELAY_MS"),
        echo_library=value("COMBADGE_AEC_LIBRARY"),
        timezone=value("COMBADGE_TIMEZONE") or "America/Toronto",
        live_model=value("OPENAI_LIVE_MODEL") or "gpt-live-1",
        live_voice=value("OPENAI_LIVE_VOICE") or "marin",
        backend_model=value("OPENAI_BACKEND_MODEL") or "gpt-5.6-luna",
        shopify_agent_profile_url=value("SHOPIFY_AGENT_PROFILE_URL") or SHOPIFY_PROFILE_URL,
        shopify_country=value("SHOPIFY_COUNTRY") or "CA",
        shopify_currency=value("SHOPIFY_CURRENCY") or "CAD",
        shopify_merchant_domain=value("SHOPIFY_MERCHANT_DOMAIN"),
    )
