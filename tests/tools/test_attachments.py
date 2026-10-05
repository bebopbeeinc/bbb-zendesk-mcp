import pytest
import json
import zipfile
import tarfile
import base64
from pathlib import Path
from unittest.mock import patch, MagicMock
from tests.conftest import make_mock_attachment, make_mock_comment
from zendesk_mcp.client import ConfigError
from zendesk_mcp.errors import ToolError


def _client_with_comments(comments):
    mock_client = MagicMock()
    mock_client.tickets.comments.return_value = iter(comments)
    return mock_client


@patch("zendesk_mcp.tools.attachments.get_client")
def test_list_attachments_aggregates_across_comments(mock_get_client):
    att1 = make_mock_attachment("debug.log", "text/plain", 512, "https://cdn.zendesk.com/1")
    att2 = make_mock_attachment("bundle.zip", "application/zip", 4096, "https://cdn.zendesk.com/2")
    c1 = make_mock_comment(comment_id=1, attachments=[att1])
    c2 = make_mock_comment(comment_id=2, attachments=[att2])
    mock_get_client.return_value = _client_with_comments([c1, c2])

    from zendesk_mcp.tools.attachments import _list_attachments_data
    result = json.loads(_list_attachments_data(12345))

    assert len(result) == 2
    assert result[0]["comment_id"] == 1
    assert result[0]["filename"] == "debug.log"
    assert result[1]["filename"] == "bundle.zip"
    assert result[1]["size_bytes"] == 4096


@patch("zendesk_mcp.tools.attachments.get_client")
def test_list_attachments_returns_empty_list_when_no_attachments(mock_get_client):
    c = make_mock_comment(comment_id=1, attachments=[])
    mock_get_client.return_value = _client_with_comments([c])

    from zendesk_mcp.tools.attachments import _list_attachments_data
    result = json.loads(_list_attachments_data(12345))

    assert result == []


@patch("zendesk_mcp.tools.attachments.get_client")
def test_list_attachments_raises_on_config_error(mock_get_client):
    mock_get_client.side_effect = ConfigError("Zendesk not configured. Run: zendesk-mcp setup")

    from zendesk_mcp.tools.attachments import _list_attachments_data
    with pytest.raises(ToolError) as err:
        _list_attachments_data(12345)
    result = str(err.value)

    assert "zendesk-mcp setup" in result


@patch("zendesk_mcp.tools.attachments.attachment_cache_dir")
@patch("zendesk_mcp.tools.attachments.auth.request")
def test_download_text_file_returns_content(mock_httpx_get, mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path / "attachments" / "12345"
    mock_httpx_get.return_value = MagicMock(
        content=b"ERROR: disk full\nstack trace here",
        raise_for_status=lambda: None,
    )

    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data("https://cdn.zendesk.com/debug.log", "debug.log", 12345))

    assert result["type"] == "text"
    assert "disk full" in result["content"]
    assert result["cached_path"].endswith("12345/debug.log")


@patch("zendesk_mcp.tools.attachments.attachment_cache_dir")
@patch("zendesk_mcp.tools.attachments.auth.request")
def test_download_zip_returns_file_tree(mock_httpx_get, mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path / "attachments" / "12345"
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello from zip")
    mock_httpx_get.return_value = MagicMock(
        content=buf.getvalue(),
        raise_for_status=lambda: None,
    )

    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data("https://cdn.zendesk.com/bundle.zip", "bundle.zip", 12345))

    assert result["type"] == "archive"
    assert any("readme.txt" in f for f in result["files"])
    assert result["file_count"] == 1
    assert result["truncated"] is False
    assert result["unpack_dir"].endswith("12345/bundle")
    # Body of files is not inlined — caller reads from unpack_dir
    assert "text_contents" not in result
    unpack_dir = Path(result["unpack_dir"])
    assert (unpack_dir / "readme.txt").read_text() == "hello from zip"


@patch("zendesk_mcp.tools.attachments.attachment_cache_dir")
@patch("zendesk_mcp.tools.attachments.auth.request")
def test_download_zip_caps_file_list_when_large(mock_httpx_get, mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path / "attachments" / "12345"
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(600):
            zf.writestr(f"f{i:04d}.log", "x")
    mock_httpx_get.return_value = MagicMock(
        content=buf.getvalue(),
        raise_for_status=lambda: None,
    )

    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data("https://cdn.zendesk.com/big.zip", "big.zip", 12345))

    assert result["file_count"] == 600
    assert len(result["files"]) == 500
    assert result["truncated"] is True


@patch("zendesk_mcp.tools.attachments.attachment_cache_dir")
@patch("zendesk_mcp.tools.attachments.auth.request")
def test_download_with_dest_dir_uses_override(mock_httpx_get, mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path / "cache" / "12345"
    mock_httpx_get.return_value = MagicMock(
        content=b"hello",
        raise_for_status=lambda: None,
    )

    override = tmp_path / "workspace" / "bundles" / "12345"
    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data(
        "https://cdn.zendesk.com/notes.txt", "notes.txt", 12345, str(override)
    ))

    assert result["cached_path"] == str(override / "notes.txt")
    assert not (tmp_path / "cache").exists()


def _image_bytes(size, mode="RGB", fmt="JPEG", noise=False, orientation=None):
    import io
    import os
    from PIL import Image
    if noise:
        img = Image.frombytes(mode, size, os.urandom(size[0] * size[1] * len(mode)))
    else:
        img = Image.new(mode, size, color=(200, 30, 30, 128)[:len(mode)])
    params = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        params["exif"] = exif
    buf = io.BytesIO()
    img.save(buf, format=fmt, **params)
    return buf.getvalue()


def _download_image(tmp_path, content, filename):
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with patch("zendesk_mcp.tools.attachments.attachment_cache_dir",
               return_value=tmp_path / "attachments" / "12345"), \
            patch("zendesk_mcp.tools.attachments.auth.request",
                  return_value=MagicMock(content=content, raise_for_status=lambda: None)):
        return _download_attachment_data(f"https://example.zendesk.com/sc/attachments/v2/c/{filename}", filename, 12345)


def _field(model, snake, camel):
    # mcp 2.x's models name fields in snake_case, 1.x's in camelCase.
    return getattr(model, snake, getattr(model, camel, None))


def _image_result_parts(result):
    """(metadata dict, preview PIL image, preview bytes, preview mime type) of an image result."""
    import io
    from PIL import Image
    text, image = result.content
    assert text.type == "text" and image.type == "image"
    preview = base64.b64decode(image.data)
    return (json.loads(text.text), Image.open(io.BytesIO(preview)), preview,
            _field(image, "mime_type", "mimeType"))


def test_download_image_returns_an_image_block_and_short_metadata_not_base64_text(tmp_path):
    original = _image_bytes((4000, 3000), noise=True)
    result = _download_image(tmp_path, original, "pickedMedia.jpg")

    meta, preview, preview_bytes, mime = _image_result_parts(result)
    # The text is metadata only: no base64 payload inside it.
    assert len(result.content[0].text) < 1000
    assert "data" not in meta and "encoding" not in meta
    assert meta["type"] == "image"
    assert meta["cached_path"] == str(tmp_path / "attachments" / "12345" / "pickedMedia.jpg")
    assert meta["content_type"] == "image/jpeg"
    assert meta["size_bytes"] == len(original)
    assert (meta["width"], meta["height"]) == (4000, 3000)
    # The copy the model sees is downscaled to the 1568 px long edge and well under 1 MB.
    assert mime == "image/jpeg" and preview.format == "JPEG"
    assert preview.size == (1568, 1176)
    assert len(preview_bytes) <= 500_000
    assert meta["preview"] == {"content_type": "image/jpeg", "width": 1568, "height": 1176,
                               "size_bytes": len(preview_bytes)}
    # The original is saved untouched.
    assert Path(meta["cached_path"]).read_bytes() == original
    # The structured copy mirrors the text, so the declared {"result": str} output still holds.
    assert _field(result, "structured_content", "structuredContent") == {"result": result.content[0].text}


def test_download_small_image_is_not_upscaled(tmp_path):
    result = _download_image(tmp_path, _image_bytes((320, 200)), "tiny.jpeg")
    meta, preview, _, _ = _image_result_parts(result)
    assert preview.size == (320, 200)
    assert (meta["width"], meta["height"]) == (320, 200)


def test_download_transparent_png_stays_png(tmp_path):
    result = _download_image(tmp_path, _image_bytes((2000, 500), mode="RGBA", fmt="PNG"), "overlay.png")
    meta, preview, _, mime = _image_result_parts(result)
    assert mime == "image/png" and preview.format == "PNG" and preview.mode == "RGBA"
    assert preview.size == (1568, 392)
    assert meta["content_type"] == "image/png"


def test_download_rgba_screenshot_with_nothing_transparent_becomes_jpeg(tmp_path):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (1080, 2400), (20, 40, 60, 255)).save(buf, format="PNG")
    result = _download_image(tmp_path, buf.getvalue(), "Screenshot_20260101-120000.png")
    _, preview, _, mime = _image_result_parts(result)
    assert mime == "image/jpeg" and preview.mode == "RGB"
    assert preview.size == (706, 1568)


def test_download_opaque_png_screenshot_becomes_jpeg(tmp_path):
    result = _download_image(tmp_path, _image_bytes((1080, 2400), fmt="PNG"), "Screenshot_20260101-120000.png")
    meta, preview, _, mime = _image_result_parts(result)
    assert mime == "image/jpeg"
    assert preview.size == (706, 1568)
    assert meta["content_type"] == "image/png"


def test_download_transparent_png_too_big_for_the_budget_falls_back_to_jpeg(tmp_path):
    result = _download_image(tmp_path, _image_bytes((1568, 1568), mode="RGBA", fmt="PNG", noise=True), "noise.png")
    _, preview, preview_bytes, mime = _image_result_parts(result)
    assert mime == "image/jpeg" and preview.mode == "RGB"
    assert len(preview_bytes) <= 500_000


def test_download_noisy_image_steps_down_until_it_fits_the_budget(tmp_path):
    # Random noise does not compress: even JPEG at the lowest quality step is over budget at
    # 1568 px, so the copy shrinks until it fits.
    result = _download_image(tmp_path, _image_bytes((3000, 3000), noise=True, fmt="PNG"), "noise.png")
    _, preview, preview_bytes, _ = _image_result_parts(result)
    assert len(preview_bytes) <= 500_000
    assert max(preview.size) <= 1568


def test_download_image_is_shown_upright_from_its_exif_orientation(tmp_path):
    # Orientation 6: stored landscape, displayed rotated 90 degrees -> portrait.
    result = _download_image(tmp_path, _image_bytes((300, 100), orientation=6), "phone.jpg")
    meta, preview, _, _ = _image_result_parts(result)
    assert (meta["width"], meta["height"]) == (300, 100)
    assert preview.size == (100, 300)


def test_download_palette_gif_with_transparency(tmp_path):
    import io
    from PIL import Image
    img = Image.new("P", (40, 40), 0)
    buf = io.BytesIO()
    img.save(buf, format="GIF", transparency=0)
    result = _download_image(tmp_path, buf.getvalue(), "sticker.gif")
    meta, preview, _, mime = _image_result_parts(result)
    assert meta["content_type"] == "image/gif"
    assert mime == "image/png" and preview.mode == "RGBA"


def test_download_corrupt_image_raises_naming_the_cached_file(tmp_path):
    with pytest.raises(ToolError) as err:
        _download_image(tmp_path, b"not an image", "broken.jpg")
    assert "Image processing failed" in str(err.value)
    assert str(tmp_path / "attachments" / "12345" / "broken.jpg") in str(err.value)


@patch("zendesk_mcp.tools.attachments.attachment_cache_dir")
@patch("zendesk_mcp.tools.attachments.auth.request")
def test_download_corrupt_zip_raises_naming_the_cached_file(mock_httpx_get, mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path / "attachments" / "12345"
    mock_httpx_get.return_value = MagicMock(
        content=b"this is not a zip",
        raise_for_status=lambda: None,
    )

    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError) as err:
        _download_attachment_data("https://cdn.zendesk.com/bad.zip", "bad.zip", 12345)

    message = str(err.value)
    assert "unpack zip" in message.lower()
    # The download itself worked: the error says where the file is, so it can still be read.
    assert "cached_path" in message
    assert str(tmp_path / "attachments" / "12345" / "bad.zip") in message


@patch("zendesk_mcp.tools.attachments.attachment_cache_dir")
@patch("zendesk_mcp.tools.attachments.auth.request")
def test_download_tar_returns_file_tree(mock_httpx_get, mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path / "attachments" / "12345"
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        content = b"hello from tar"
        info = tarfile.TarInfo(name="readme.txt")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    mock_httpx_get.return_value = MagicMock(
        content=buf.getvalue(),
        raise_for_status=lambda: None,
    )

    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data("https://cdn.zendesk.com/logs.tar.gz", "logs.tar.gz", 12345))

    assert result["type"] == "archive"
    assert any("readme.txt" in f for f in result["files"])
    assert result["file_count"] == 1
    assert "text_contents" not in result
    unpack_dir = Path(result["unpack_dir"])
    assert (unpack_dir / "readme.txt").read_text() == "hello from tar"


@patch("zendesk_mcp.tools.attachments.attachment_cache_dir")
@patch("zendesk_mcp.tools.attachments.auth.request")
def test_download_failure_raises(mock_httpx_get, mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path / "attachments" / "12345"
    mock_httpx_get.side_effect = Exception("503 Service Unavailable")

    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="Download failed: Zendesk API error: 503"):
        _download_attachment_data("https://cdn.zendesk.com/x.log", "x.log", 12345)


@patch("zendesk_mcp.tools.attachments.load_config", return_value={"subdomain": "example"})
@patch("zendesk_mcp.tools.attachments.get_client")
def test_list_attachments_includes_messaging_transcript_uploads(mock_get_client, _config):
    from tests.conftest import IN_GAME_TRANSCRIPT
    att = make_mock_attachment("debug.log", "text/plain", 512, "https://cdn.zendesk.com/1")
    c1 = make_mock_comment(comment_id=1, attachments=[att])
    c2 = make_mock_comment(comment_id=2, body=IN_GAME_TRANSCRIPT)
    mock_get_client.return_value = _client_with_comments([c1, c2])

    from zendesk_mcp.tools.attachments import _list_attachments_data
    result = json.loads(_list_attachments_data(12345))

    # The comment attachment is exactly as before.
    assert result[0] == {
        "comment_id": 1,
        "filename": "debug.log",
        "content_type": "text/plain",
        "size_bytes": 512,
        "download_url": "https://cdn.zendesk.com/1",
    }
    uploads = result[1:]
    assert [u["filename"] for u in uploads] == [
        "pickedMedia.jpg", "Screenshot_20260101-120000.jpg", "how-to-restore.png",
    ]
    first = uploads[0]
    assert first == {
        "comment_id": 2,
        "filename": "pickedMedia.jpg",
        "content_type": "image/jpeg",
        "size_bytes": 98304,
        "download_url": "https://example.zendesk.com/sc/attachments/v2/01J8ZR5D3F7H9K1M3P5R7T9V1X/pickedMedia.jpg",
        "source": "messaging_transcript",
        "uploaded_by": "Player Two",
        "time": "09:14:20",
    }
    # No attachment id is made up for an upload Zendesk never gave one.
    assert all("id" not in u for u in uploads)
    assert uploads[2]["uploaded_by"] == "Support Agent"


@patch("zendesk_mcp.tools.attachments.load_config", return_value={"subdomain": "other"})
@patch("zendesk_mcp.tools.attachments.get_client")
def test_list_attachments_ignores_uploads_on_another_account(mock_get_client, _config):
    from tests.conftest import MESSENGER_TRANSCRIPT
    mock_get_client.return_value = _client_with_comments([make_mock_comment(body=MESSENGER_TRANSCRIPT)])

    from zendesk_mcp.tools.attachments import _list_attachments_data
    assert json.loads(_list_attachments_data(12345)) == []
