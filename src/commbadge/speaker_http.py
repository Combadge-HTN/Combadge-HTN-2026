"""One bounded transcription request; stdin/stdout keep secrets out of arguments."""

import base64
import json
import sys
import urllib.error
import urllib.request
import uuid

URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_RESPONSE_BYTES = 262144


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
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return {"error": "oversized_response"}
        return {"result": json.loads(raw)}
    except urllib.error.HTTPError as error:
        error.close()
        return {"status": error.code}
    except Exception:
        # Remote bodies, authorization headers and transcripts never enter diagnostics.
        return {"error": "request_failed"}


def main():
    try:
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("oversized request")
        result = request(json.loads(raw))
    except Exception:
        result = {"error": "invalid_request"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
