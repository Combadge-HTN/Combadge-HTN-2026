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
    browserbase_api_key: str = field(default="", repr=False)
    browserbase_project_id: str = field(default="", repr=False)
    composio_api_key: str = field(default="", repr=False)
    composio_user_id: str = field(default="", repr=False)
    composio_accounts: dict[str, str] = field(default_factory=dict, repr=False)
    timezone: str = "America/Toronto"
    live_model: str = "gpt-live-1"
    live_voice: str = "marin"
    voice_conversion_url: str = ""
    voice_conversion_token: str = field(default="", repr=False)
    voice_conversion_autostart: bool = False
    backend_model: str = "gpt-5.6-luna"
    shopify_agent_profile_url: str = SHOPIFY_PROFILE_URL
    shopify_country: str = "CA"
    shopify_currency: str = "CAD"


def load_settings(env_file: Path = Path(".env")) -> Settings:
    """Read the chosen file; exported environment values take precedence."""
    values = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}

    def value(name: str) -> str:
        return (os.environ.get(name, values.get(name)) or "").strip()

    return Settings(
        openai_api_key=value("OPENAI_API_KEY"),
        browserbase_api_key=value("BROWSERBASE_API_KEY"),
        browserbase_project_id=value("BROWSERBASE_PROJECT_ID"),
        composio_api_key=value("COMPOSIO_API_KEY"),
        composio_user_id=value("COMPOSIO_USER_ID"),
        composio_accounts={
            app: account
            for app in ("gmail", "googlecalendar")
            if (account := value(f"COMPOSIO_{app.upper()}_ACCOUNT_ID"))
        },
        timezone=value("COMBADGE_TIMEZONE") or "America/Toronto",
        live_model=value("OPENAI_LIVE_MODEL") or "gpt-live-1",
        live_voice=value("OPENAI_LIVE_VOICE") or "marin",
        voice_conversion_url=value("COMBADGE_VOICE_CONVERSION_URL"),
        voice_conversion_token=value("COMBADGE_VOICE_CONVERSION_TOKEN"),
        voice_conversion_autostart=value("COMBADGE_VOICE_CONVERSION_AUTOSTART").lower()
        in ("1", "true", "yes"),
        backend_model=value("OPENAI_BACKEND_MODEL") or "gpt-5.6-luna",
        shopify_agent_profile_url=value("SHOPIFY_AGENT_PROFILE_URL") or SHOPIFY_PROFILE_URL,
        shopify_country=value("SHOPIFY_COUNTRY") or "CA",
        shopify_currency=value("SHOPIFY_CURRENCY") or "CAD",
    )
