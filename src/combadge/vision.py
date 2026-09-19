"""Still-image inputs for the managed Responses backend."""

import base64
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ENCODED_IMAGE_BYTES = 256 * 1024
MAX_SESSION_IMAGE_BYTES = 2 * 1024 * 1024
DEFAULT_QUESTION = "Describe the attached image briefly."


def _compress_image(data: bytes) -> bytes:
    """Optional native adapter; QNX capture helpers can supply bounded JPEGs directly."""
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise ValueError(
            "Image exceeds 256 KiB. Install automatic resizing with pip install -e '.[images]', "
            "or configure the camera helper to produce a JPEG no larger than 256 KiB."
        ) from error
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 40_000_000:
                raise ValueError("Image exceeds the 40-megapixel decoding limit.")
            picture = ImageOps.exif_transpose(source)
            picture.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            if "A" in picture.getbands() or "transparency" in picture.info:
                rgba = picture.convert("RGBA")
                picture = Image.new("RGB", rgba.size, "white")
                picture.paste(rgba, mask=rgba.getchannel("A"))
            else:
                picture = picture.convert("RGB")
            while True:
                for quality in (85, 70, 55):
                    output = io.BytesIO()
                    picture.save(output, format="JPEG", quality=quality, optimize=True)
                    encoded = output.getvalue()
                    if len(encoded) <= MAX_ENCODED_IMAGE_BYTES:
                        return encoded
                if max(picture.size) <= 480:
                    raise ValueError("Image could not be compressed to the upload limit.")
                picture.thumbnail(
                    (max(1, int(picture.width * 0.75)), max(1, int(picture.height * 0.75))),
                    Image.Resampling.LANCZOS,
                )
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("Image could not be decoded for resizing.") from error


@dataclass(frozen=True)
class ImageInput:
    data_url: str = field(repr=False)
    question: str

    @classmethod
    def from_bytes(cls, data: bytes, question: str = DEFAULT_QUESTION) -> ImageInput:
        """Accept encoded JPEG, PNG, or WebP bytes from a file or camera adapter."""
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image must be non-empty and no larger than 20 MiB.")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError("Unsupported image. Use an encoded JPEG, PNG, or WebP file.")
        if not question.strip() or len(question) > 4000:
            raise ValueError("Image question must contain 1–4000 characters.")
        if len(data) > MAX_ENCODED_IMAGE_BYTES:
            data = _compress_image(data)
            mime = "image/jpeg"
        return cls(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", question)

    @classmethod
    def from_file(cls, path: Path, question: str = DEFAULT_QUESTION) -> ImageInput:
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


class ImageBudget:
    """Bound encoded image history, leaving room for tool results and conversation text."""

    def __init__(self):
        self.used_bytes = 0

    def add(self, image: ImageInput) -> None:
        size = len(json.dumps(image.event()["item"]).encode("utf-8"))
        if self.used_bytes + size > MAX_SESSION_IMAGE_BYTES:
            raise ValueError(
                "This session's image budget is full. Restart the voice session for a new image. "
                "No new image was sent."
            )
        self.used_bytes += size
