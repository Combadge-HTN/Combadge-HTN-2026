"""Direct SIP configuration, with the previous relay transport available explicitly."""

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
    def load(cls, path: Path, *, optional: bool = False):
        env = values(path)
        transport = env.get("CALL_TRANSPORT") or ("sip" if env.get("CALL_SIP_DOMAIN") else "relay")
        if transport == "sip":
            if optional and not all(
                (env.get(key) or "").strip()
                for key in (
                    "CALL_SIP_DOMAIN",
                    "CALL_SIP_USERNAME",
                    "CALL_SIP_PASSWORD",
                    "TWILIO_FROM_NUMBER",
                    "CALL_CONTACTS",
                )
            ):
                return None
            return SipSettings.load(path)
        if transport != "relay":
            raise ValueError("CALL_TRANSPORT must be sip or relay")
        if optional and not all(
            (env.get(key) or "").strip() for key in ("CALL_RELAY_URL", "CALL_RELAY_TOKEN")
        ):
            return None
        url = secure_url(env.get("CALL_RELAY_URL", ""), "wss")
        token = env.get("CALL_RELAY_TOKEN", "")
        if len(token) < 32:
            raise ValueError("CALL_RELAY_TOKEN must contain at least 32 characters")
        return cls(url, token)


@dataclass(frozen=True)
class SipSettings:
    domain: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    from_number: str = field(repr=False)
    contacts: dict[str, str] = field(repr=False)
    max_seconds: int = 300

    @classmethod
    def load(cls, path: Path):
        env = values(path)
        domain = env.get("CALL_SIP_DOMAIN", "").lower()
        if not re.fullmatch(r"[a-z0-9-]+\.pstn(?:\.[a-z0-9-]+)?\.twilio\.com", domain):
            raise ValueError("CALL_SIP_DOMAIN must be a Twilio termination hostname")
        username = env.get("CALL_SIP_USERNAME", "")
        password = env.get("CALL_SIP_PASSWORD", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", username) or len(password) < 12:
            raise ValueError("Set CALL_SIP_USERNAME and CALL_SIP_PASSWORD (at least 12 characters)")
        if any(c in password for c in "\r\n\0"):
            raise ValueError("Invalid SIP password")
        contacts = json.loads(env.get("CALL_CONTACTS", "{}"))
        if not isinstance(contacts, dict) or not contacts:
            raise ValueError("CALL_CONTACTS must map contact names to E.164 numbers")
        normalized = {}
        for name, number in contacts.items():
            if not isinstance(name, str) or not re.fullmatch(r"[\w -]{1,60}", name):
                raise ValueError("Invalid contact name")
            if name.casefold() in normalized:
                raise ValueError("Contact names must be unique ignoring case")
            normalized[name.casefold()] = number
        sender = env.get("TWILIO_FROM_NUMBER", "")
        if any(
            not isinstance(n, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", n)
            for n in [sender, *normalized.values()]
        ):
            raise ValueError("Phone numbers must use E.164 format")
        limit = int(env.get("CALL_MAX_SECONDS", "300"))
        if not 10 <= limit <= 3600:
            raise ValueError("CALL_MAX_SECONDS must be between 10 and 3600")
        return cls(domain, username, password, sender, normalized, limit)


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
