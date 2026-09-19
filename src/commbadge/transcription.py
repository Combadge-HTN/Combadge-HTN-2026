"""Bounded standard-library HTTPS adapter for speaker transcription."""

import base64
import json
import urllib.error
import urllib.request
import uuid

URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_RESPONSE_BYTES = 262144


def read_bounded(stream, limit: int) -> bytes:
    """Read through short reads until EOF, retaining at most limit bytes."""
    chunks = []
    size = 0
    while chunk := stream.read(min(65536, limit + 1 - size)):
        size += len(chunk)
        if size > limit:
            raise ValueError("Input exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(payload: dict) -> dict:
    boundary = uuid.uuid4().hex
    parts = []

    def part(name, data, filename=None):
        header = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            header += f'; filename="{filename}"\r\nContent-Type: audio/wav'
        parts.append(f"--{boundary}\r\n{header}\r\n\r\n".encode() + data + b"\r\n")

    part("model", b"gpt-4o-transcribe-diarize")
    part("response_format", b"diarized_json")
    part("chunking_strategy", b"auto")
    part("file", base64.b64decode(payload["audio"], validate=True), "audio.wav")
    for reference in payload["references"]:
        part("known_speaker_names[]", reference["name"].encode())
        part("known_speaker_references[]", ("data:audio/wav;base64," + reference["audio"]).encode())
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        URL,
        data=body,
        headers={
            "Authorization": "Bearer " + payload["api_key"],
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=8) as response:
            try:
                raw = read_bounded(response, MAX_RESPONSE_BYTES)
            except ValueError:
                return {"error": "oversized_response"}
        return {"result": json.loads(raw)}
    except urllib.error.HTTPError as error:
        error.close()
        return {"status": error.code}
    except Exception:
        # Remote bodies, authorization headers and transcripts never enter diagnostics.
        return {"error": "request_failed"}
