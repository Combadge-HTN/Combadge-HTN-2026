"""Bounded SIP-over-TLS messages, digest authentication, and encrypted SDP."""

import base64
import hashlib
import ipaddress
import re
import secrets
from dataclasses import dataclass

MAX_BODY = 32768


@dataclass
class Message:
    start: str
    headers: list[tuple[str, str]]
    body: bytes = b""

    def all(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def get(self, name, default=""):
        values = self.all(name)
        return values[0] if values else default

    @property
    def status(self):
        return int(self.start.split()[1]) if self.start.startswith("SIP/2.0 ") else 0

    @property
    def method(self):
        return self.start.split()[0] if not self.status else self.get("CSeq").split()[-1]

    def encode(self):
        lines = [self.start]
        for name, value in self.headers:
            if any(c in name + value for c in "\r\n\0"):
                raise ValueError("Invalid SIP header")
            if name.lower() != "content-length":
                lines.append(f"{name}: {value}")
        if any(c in self.start for c in "\r\n\0") or len(self.body) > MAX_BODY:
            raise ValueError("Invalid SIP message")
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + self.body


async def read_message(reader):
    while True:
        raw = await reader.readline()
        if not raw:
            raise ConnectionError("SIP connection closed")
        if raw != b"\r\n":
            break
    lines, size = [raw], len(raw)
    while True:
        line = await reader.readline()
        if not line:
            raise ConnectionError("Truncated SIP message")
        size += len(line)
        if size > 32768:
            raise ValueError("SIP headers too large")
        if line == b"\r\n":
            break
        lines.append(line)
    headers = []
    compact = {
        "v": "Via",
        "f": "From",
        "t": "To",
        "i": "Call-ID",
        "m": "Contact",
        "l": "Content-Length",
        "c": "Content-Type",
    }
    for line in lines[1:]:
        if line[:1] in (b" ", b"\t") and headers:
            name, value = headers.pop()
            headers.append((name, value + " " + line.decode().strip()))
            continue
        name, separator, value = line.decode().strip().partition(":")
        if not separator or not name:
            raise ValueError("Malformed SIP header")
        headers.append((compact.get(name.lower(), name), value.strip()))
    msg = Message(lines[0].decode().strip(), headers)
    lengths = msg.all("Content-Length")
    if len(lengths) != 1 or not lengths[0].isdigit():
        raise ValueError("Missing or ambiguous SIP body length")
    length = int(lengths[0])
    if length > MAX_BODY:
        raise ValueError("SIP body too large")
    msg.body = await reader.readexactly(length)
    if msg.status and not 100 <= msg.status <= 699:
        raise ValueError("Invalid SIP response")
    return msg


def digest(challenge, username, password, method, uri, *, cnonce=None):
    if not challenge.lower().startswith("digest "):
        raise ValueError("Unsupported SIP authentication")
    fields = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"\r\n]*)"|([^,\s]+))', challenge[7:]):
        fields[m[1].lower()] = m[2] if m[2] is not None else m[3]
    if not fields.get("nonce") or not fields.get("realm"):
        raise ValueError("Incomplete SIP authentication challenge")
    algorithm = fields.get("algorithm", "MD5").upper()
    if algorithm not in ("MD5", "SHA-256"):
        raise ValueError("Unsupported SIP digest algorithm")
    hash_name = "md5" if algorithm == "MD5" else "sha256"

    def hashed(text):
        return hashlib.new(hash_name, text.encode()).hexdigest()

    qop = fields.get("qop")
    if qop and "auth" not in [s.strip() for s in qop.split(",")]:
        raise ValueError("Unsupported SIP digest quality of protection")
    nonce, realm = fields["nonce"], fields["realm"]
    a1, a2 = hashed(f"{username}:{realm}:{password}"), hashed(f"{method}:{uri}")
    cnonce = cnonce or secrets.token_hex(16)
    response = hashed(f"{a1}:{nonce}:00000001:{cnonce}:auth:{a2}" if qop else f"{a1}:{nonce}:{a2}")
    result = {
        "username": username,
        "realm": realm,
        "nonce": nonce,
        "uri": uri,
        "response": response,
    }
    if "opaque" in fields:
        result["opaque"] = fields["opaque"]
    for value in result.values():
        if any(c in value for c in '\r\n"\\\0'):
            raise ValueError("Invalid SIP digest value")
    parts = [f'{k}="{v}"' for k, v in result.items()]
    parts.append(f"algorithm={algorithm}")
    if qop:
        parts.extend(["qop=auth", "nc=00000001", f'cnonce="{cnonce}"'])
    return "Digest " + ", ".join(parts)


def uri(value):
    match = re.search(r"<([^>]+)>", value)
    result = match[1] if match else value.split(";tag=")[0].strip()
    if not result.startswith("sip:") or any(c in result for c in "\r\n <>"):
        raise ValueError("Invalid SIP contact or route")
    return result


def tag(value):
    match = re.search(r";tag=([^;\s>]+)", value)
    return match[1] if match else None


def routes(values):
    # Record-Route may be repeated or comma-separated in a single header.
    result = []
    for value in values:
        quoted, angle, escaped, start = False, False, False, 0
        for index, char in enumerate(value):
            if escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted:
                if char == "<":
                    angle = True
                elif char == ">":
                    angle = False
                elif char == "," and not angle:
                    result.append(value[start:index].strip())
                    start = index + 1
        result.append(value[start:].strip())
    for value in result:
        uri(value)
    return list(reversed(result))


def offer(ip, port, master, session_id):
    key = base64.b64encode(master).decode()
    return (
        f"v=0\r\no=- {session_id} 1 IN IP4 {ip}\r\ns=Combadge\r\n"
        f"c=IN IP4 {ip}\r\nt=0 0\r\nm=audio {port} RTP/SAVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n"
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{key}\r\n"
    ).encode()


@dataclass(frozen=True)
class Media:
    ip: str
    port: int
    key: bytes


def answer(body):
    sections = body.decode("ascii").replace("\r", "").split("\n")
    connection = None
    selected = False
    found = False
    port = None
    key = None
    for line in sections:
        if line.startswith("m="):
            if found:
                break
            parts = line[2:].split()
            selected = parts[0] == "audio"
            if selected:
                if len(parts) < 4 or parts[2] != "RTP/SAVP" or "0" not in parts[3:]:
                    raise ValueError("Call requires encrypted PCMU audio")
                port = int(parts[1])
                found = True
        elif line.startswith("c=") and (selected or not found):
            parts = line[2:].split()
            if len(parts) != 3 or parts[:2] != ["IN", "IP4"]:
                raise ValueError("Call requires IPv4 media")
            connection = str(ipaddress.IPv4Address(parts[2]))
        elif selected and line.startswith("a=crypto:"):
            parts = line.split()
            if (
                len(parts) == 3
                and parts[0] == "a=crypto:1"
                and parts[1] == "AES_CM_128_HMAC_SHA1_80"
            ):
                encoded = parts[2].removeprefix("inline:")
                if "|" in encoded:
                    raise ValueError("SRTP lifetime and MKI parameters are unsupported")
                key = base64.b64decode(encoded, validate=True)
        elif selected and line in ("a=inactive", "a=sendonly", "a=recvonly"):
            raise ValueError("Call requires two-way audio")
    if not connection or not port or not 1 <= port <= 65535 or key is None or len(key) != 30:
        raise ValueError("Incomplete secure media answer")
    if ipaddress.IPv4Address(connection).is_multicast or connection == "0.0.0.0":
        raise ValueError("Invalid media destination")
    return Media(connection, port, key)
