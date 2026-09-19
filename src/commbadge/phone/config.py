"""Separate badge and relay configuration; carrier credentials stay on the relay."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def values(path: Path) -> dict:
    return {**dotenv_values(path, interpolate=False), **os.environ}


def secure_url(url: str, scheme: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError(f"Expected a {scheme}:// host with no path, credentials, or query")
    return url.rstrip("/")


@dataclass(frozen=True)
class PhoneSettings:
    relay_url: str
    token: str = field(repr=False)

    @classmethod
    def load(cls, path: Path):
        env = values(path)
        url = secure_url(env.get("CALL_RELAY_URL", ""), "wss")
        token = env.get("CALL_RELAY_TOKEN", "")
        if len(token) < 32:
            raise ValueError("CALL_RELAY_TOKEN must contain at least 32 characters")
        return cls(url, token)


@dataclass(frozen=True)
class RelaySettings:
    public_url: str
    token: str = field(repr=False)
    account_sid: str
    auth_token: str = field(repr=False)
    from_number: str = field(repr=False)
    contacts: dict[str, str] = field(repr=False)
    max_seconds: int = 300

    @classmethod
    def load(cls, path: Path):
        env = values(path)
        public = secure_url(env.get("CALL_PUBLIC_URL", ""), "https")
        token = env.get("CALL_RELAY_TOKEN", "")
        if len(token) < 32:
            raise ValueError("CALL_RELAY_TOKEN must contain at least 32 characters")
        sid, secret = env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_AUTH_TOKEN", "")
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid) or not secret:
            raise ValueError("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN on the relay")
        contacts = json.loads(env.get("CALL_CONTACTS", "{}"))
        if not isinstance(contacts, dict) or not contacts:
            raise ValueError("CALL_CONTACTS must be a JSON object mapping contact names to numbers")
        normalized = {}
        for name, number in contacts.items():
            if not isinstance(name, str) or not re.fullmatch(r"[\w -]{1,60}", name):
                raise ValueError("Contact names must be 1–60 letters, digits, spaces, or hyphens")
            if name.casefold() in normalized:
                raise ValueError("Contact names must be unique ignoring case")
            normalized[name.casefold()] = number
        sender = env.get("TWILIO_FROM_NUMBER", "")
        for number in [sender, *normalized.values()]:
            if not isinstance(number, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
                raise ValueError("Phone numbers must use E.164 format, such as +14165550123")
        limit = int(env.get("CALL_MAX_SECONDS", "300"))
        if not 10 <= limit <= 3600:
            raise ValueError("CALL_MAX_SECONDS must be between 10 and 3600")
        return cls(public, token, sid, secret, sender, normalized, limit)
