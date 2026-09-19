import base64
import io
import json
import random
from unittest.mock import patch

import pytest

from commbadge.vision import (
    MAX_ENCODED_IMAGE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_SESSION_IMAGE_BYTES,
    ImageBudget,
    ImageInput,
)


@pytest.mark.parametrize(
    "data,mime",
    [
        (b"\x89PNG\r\n\x1a\nencoded", "image/png"),
        (b"\xff\xd8\xffencoded", "image/jpeg"),
        (b"RIFF1234WEBPencoded", "image/webp"),
    ],
)
def test_encoded_image_preserves_bytes_and_question(data, mime, tmp_path):
    path = tmp_path / "capture.bin"
    path.write_bytes(data)
    image = ImageInput.from_file(path, "What is shown?")
    item = image.event()["item"]
    assert item["role"] == "user"
    assert item["content"][0] == {"type": "input_text", "text": "What is shown?"}
    content = item["content"][1]
    prefix, payload = content["image_url"].split(",", 1)
    assert prefix == f"data:{mime};base64"
    assert base64.b64decode(payload) == data
    assert payload not in repr(image)


@pytest.mark.parametrize("data", [b"", b"plain text", b"x" * (MAX_IMAGE_BYTES + 1)])
def test_invalid_images_rejected(data):
    with pytest.raises(ValueError):
        ImageInput.from_bytes(data)


def test_empty_question_rejected():
    with pytest.raises(ValueError, match="question"):
        ImageInput.from_bytes(b"\x89PNG\r\n\x1a\nencoded", " ")


def test_large_screenshot_fits_live_history_after_base64_encoding():
    from PIL import Image

    source = Image.frombytes("RGB", (2560, 1440), random.Random(7).randbytes(2560 * 1440 * 3))
    png = io.BytesIO()
    source.save(png, format="PNG")
    # This input would exhaust Live's entire 4 MiB history even before base64 encoding.
    assert len(png.getvalue()) > 4 * 1024 * 1024
    image = ImageInput.from_bytes(png.getvalue(), "Find the full box on the right")
    prefix, data = image.data_url.split(",", 1)
    assert prefix == "data:image/jpeg;base64"
    encoded = base64.b64decode(data)
    assert len(encoded) <= MAX_ENCODED_IMAGE_BYTES
    assert len(json.dumps(image.event()).encode()) < 360_000
    with Image.open(io.BytesIO(encoded)) as resized:
        assert max(resized.size) <= 1920
        assert resized.width / resized.height == pytest.approx(2560 / 1440, abs=0.01)
        assert resized.format == "JPEG"


def test_large_image_without_optional_resizer_fails_before_upload():
    with patch.dict("sys.modules", {"PIL": None}), pytest.raises(ValueError, match="images"):
        ImageInput.from_bytes(b"\x89PNG\r\n\x1a\n" + bytes(MAX_ENCODED_IMAGE_BYTES))


def test_invalid_large_image_is_reported_as_a_tool_error():
    with pytest.raises(ValueError, match="decoded"):
        ImageInput.from_bytes(b"\x89PNG\r\n\x1a\n" + bytes(MAX_ENCODED_IMAGE_BYTES))


def test_cumulative_image_budget_leaves_room_for_other_backend_history():
    image = ImageInput.from_bytes(b"\xff\xd8\xff" + bytes(MAX_ENCODED_IMAGE_BYTES - 3))
    budget = ImageBudget()
    accepted = 0
    for _ in range(10):
        previous = budget.used_bytes
        try:
            budget.add(image)
            accepted += 1
        except ValueError as error:
            assert "Restart" in str(error)
            assert budget.used_bytes == previous
            break
    assert 1 < accepted < 10
    assert budget.used_bytes <= MAX_SESSION_IMAGE_BYTES
