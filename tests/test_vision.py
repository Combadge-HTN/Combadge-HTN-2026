import base64

import pytest

from commbadge.vision import MAX_IMAGE_BYTES, ImageInput


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
