"""Load local configuration without changing the environment or exposing secrets."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = field(default="", repr=False)
    browserbase_api_key: str = field(default="", repr=False)
    browserbase_project_id: str = field(default="", repr=False)


def load_settings(env_file: Path = Path(".env")) -> Settings:
    """Read the chosen file; exported environment values take precedence."""
    values = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}

    def value(name: str) -> str:
        return (os.environ.get(name, values.get(name)) or "").strip()

    return Settings(
        openai_api_key=value("OPENAI_API_KEY"),
        browserbase_api_key=value("BROWSERBASE_API_KEY"),
        browserbase_project_id=value("BROWSERBASE_PROJECT_ID"),
    )
