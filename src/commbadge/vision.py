"""Still-image inputs for the managed Responses backend."""

import base64
from dataclasses import dataclass, field
from pathlib import Path

MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_QUESTION = "Describe the attached image briefly."


@dataclass(frozen=True)
class ImageInput:
    data_url: str = field(repr=False)
    question: str

    @classmethod
    def from_bytes(cls, data: bytes, question: str = DEFAULT_QUESTION) -> "ImageInput":
        """Accept encoded JPEG, PNG, or WebP bytes from a file or camera adapter."""
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image must be non-empty and no larger than 5 MiB.")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError("Unsupported image. Use an encoded JPEG, PNG, or WebP file.")
        if not question.strip():
            raise ValueError("Image question must not be empty.")
        return cls(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", question)

    @classmethod
    def from_file(cls, path: Path, question: str = DEFAULT_QUESTION) -> "ImageInput":
        with path.open("rb") as source:
            data = source.read(MAX_IMAGE_BYTES + 1)
        return cls.from_bytes(data, question)

    def event(self, *, event_id: str = "image_input") -> dict:
        return {
            "type": "response.item.create",
            "event_id": event_id,
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": self.question},
                    {"type": "input_image", "image_url": self.data_url, "detail": "auto"},
                ],
            },
        }
