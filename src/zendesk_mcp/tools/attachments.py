import base64
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Annotated

import pdfplumber
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, ImageOps

from zendesk_mcp.client import get_client, ConfigError
from zendesk_mcp.config import (
    ATTACHMENT_ROOT_ENV,
    AttachmentRootError,
    attachment_cache_dir,
    attachment_root,
    load_config,
)
from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.errors import ToolError
from zendesk_mcp.transcript_uploads import parse_transcript_uploads


def _list_attachments_data(ticket_id: int) -> str:
    try:
        client = get_client()
        subdomain = load_config().get("subdomain", "")
        comments = client.tickets.comments(ticket_id)
        result = []
        for comment in comments:
            for att in (comment.attachments or []):
                result.append({
                    "comment_id": comment.id,
                    "filename": att.file_name,
                    "content_type": att.content_type,
                    "size_bytes": att.size,
                    "download_url": att.content_url,
                })
            # Messaging uploads live in the transcript body, not in comment.attachments.
            for upload in parse_transcript_uploads(comment.body, subdomain):
                result.append({
                    "comment_id": comment.id,
                    "filename": upload["file_name"],
                    "content_type": upload["content_type"],
                    "size_bytes": upload["size"],
                    "download_url": upload["url"],
                    "source": "messaging_transcript",
                    "uploaded_by": upload["uploaded_by"],
                    "time": upload["time"],
                })
        return json.dumps(result, indent=2)
    except (ConfigError, TokenExpiredError) as e:
        raise ToolError(str(e)) from e
    except Exception as e:
        if "RecordNotFound" in str(e) or "404" in str(e):
            raise ToolError(f"Ticket #{ticket_id} not found or not accessible with current credentials.") from e
        raise ToolError(api_error_message(e)) from e


_TEXT_EXTENSIONS = {".log", ".txt", ".json", ".yaml", ".yml", ".xml", ".csv", ".sh", ".py", ".go", ".md"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

# Bounds applied to tool response payloads to stay within MCP transport limits.
_ARCHIVE_FILE_LIST_CAP = 500
_PDF_TEXT_CAP_BYTES = 500_000
# An image comes back as an image block the model can see: a copy no larger than the
# long edge vision models use, encoded under a byte budget. The original stays on disk.
_PREVIEW_MAX_EDGE = 1568
_PREVIEW_MAX_BYTES = 500_000
_PREVIEW_JPEG_QUALITIES = (85, 75, 65, 50)


def _confine(path: Path, root: Path | None, what: str) -> None:
    """Refuse a write whose real path (symlinks resolved) leaves the attachment root."""
    if root is None:
        return
    real = Path(os.path.realpath(path))
    if not real.is_relative_to(root):
        raise ToolError(
            f"Refusing to write {what} {path}: it resolves to {real}, outside "
            f"{ATTACHMENT_ROOT_ENV} ({root})"
        )


def _download_attachment_data(
    attachment_url: str,
    filename: str,
    ticket_id: int,
    dest_dir: str | None = None,
) -> str | CallToolResult:
    try:
        root = attachment_root()
        if dest_dir:
            target_dir = Path(dest_dir).expanduser()
        else:
            target_dir = attachment_cache_dir(ticket_id)
    except AttachmentRootError as e:
        raise ToolError(str(e)) from e
    except Exception as e:
        raise ToolError(f"Cannot create the download directory: {e}") from e
    _confine(target_dir, root, "the download directory")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise ToolError(f"Cannot create the download directory: {e}") from e
    # Strip path components from filename to prevent directory traversal
    safe_filename = Path(filename).name
    dest = target_dir / safe_filename
    # Checked again now the directory exists, and for the file itself: either may be a symlink.
    _confine(dest, root, "the download")

    try:
        response = auth.request("GET", attachment_url, follow_redirects=True)
        response.raise_for_status()
    except Exception as e:
        raise ToolError(f"Download failed: {api_error_message(e)}") from e
    try:
        dest.write_bytes(response.content)
    except Exception as e:
        raise ToolError(f"Downloaded, but could not write {dest}: {e}") from e

    suffix = Path(filename).suffix.lower()

    if suffix in _TEXT_EXTENSIONS:
        try:
            text = dest.read_text(errors="replace")
            return json.dumps({"type": "text", "content": text, "cached_path": str(dest)})
        except Exception as e:
            raise ToolError(f"{e} (the file is downloaded: cached_path {dest})") from e

    if suffix == ".zip":
        return _handle_zip(dest, root)

    if suffix in {".tar", ".gz", ".tgz"} or filename.endswith(".tar.gz"):
        return _handle_tar(dest, root)

    if suffix == ".pdf":
        return _handle_pdf(dest)

    if suffix in _IMAGE_EXTENSIONS:
        return _handle_image(dest, len(response.content))

    return json.dumps({
        "type": "binary",
        "message": "Binary file — content not returned. Use cached_path to access it.",
        "cached_path": str(dest),
        "size_bytes": len(response.content),
    })


def _safe_zip_members(zf: zipfile.ZipFile, unpack_dir: Path) -> list:
    safe = []
    for member in zf.infolist():
        member_path = (unpack_dir / member.filename).resolve()
        if member_path.parts[:len(unpack_dir.resolve().parts)] == unpack_dir.resolve().parts:
            safe.append(member)
    return safe


def _archive_summary(dest: Path, unpack_dir: Path) -> str:
    all_paths = sorted(p for p in unpack_dir.rglob("*") if p.is_file())
    total_bytes = sum(p.stat().st_size for p in all_paths)
    file_list = [str(p.relative_to(unpack_dir)) for p in all_paths[:_ARCHIVE_FILE_LIST_CAP]]
    return json.dumps({
        "type": "archive",
        "unpack_dir": str(unpack_dir),
        "cached_path": str(dest),
        "file_count": len(all_paths),
        "total_bytes": total_bytes,
        "files": file_list,
        "truncated": len(all_paths) > _ARCHIVE_FILE_LIST_CAP,
    })


def _handle_zip(dest: Path, root: Path | None = None) -> str:
    unpack_dir = dest.parent / dest.stem
    _confine(unpack_dir, root, "the archive contents into")
    try:
        with zipfile.ZipFile(dest) as zf:
            safe_members = _safe_zip_members(zf, unpack_dir)
            for member in safe_members:
                zf.extract(member, unpack_dir)
        return _archive_summary(dest, unpack_dir)
    except Exception as e:
        # Not only BadZipFile: an encrypted member (RuntimeError), a compression method zipfile
        # lacks (NotImplementedError), a truncated archive (EOFError, zlib.error), a disk error.
        raise ToolError(f"Failed to unpack zip: {e} (the file is downloaded: cached_path {dest})") from e


def _handle_tar(dest: Path, root: Path | None = None) -> str:
    unpack_dir = dest.parent / dest.stem.replace(".tar", "")
    _confine(unpack_dir, root, "the archive contents into")
    try:
        with tarfile.open(dest) as tf:
            safe_members = [
                m for m in tf.getmembers()
                if (unpack_dir / m.name).resolve().parts[:len(unpack_dir.resolve().parts)] == unpack_dir.resolve().parts
            ]
            extract_kwargs = {}
            if root is not None:
                # Under a root, a link member could point a later member's write outside it:
                # extract regular files and directories only.
                safe_members = [m for m in safe_members if m.isfile() or m.isdir()]
                if hasattr(tarfile, "data_filter"):
                    extract_kwargs["filter"] = "data"
            tf.extractall(unpack_dir, members=safe_members, **extract_kwargs)
        return _archive_summary(dest, unpack_dir)
    except Exception as e:
        # Not only TarError: a truncated gzip stream (EOFError, zlib.error), a disk error.
        raise ToolError(f"Failed to unpack tar: {e} (the file is downloaded: cached_path {dest})") from e


def _handle_pdf(dest: Path) -> str:
    try:
        with pdfplumber.open(dest) as pdf:
            chunks: list[str] = []
            size = 0
            truncated = False
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                chunks.append(page_text)
                size += len(page_text) + 1
                if size >= _PDF_TEXT_CAP_BYTES:
                    truncated = True
                    break
            text = "\n".join(chunks)
        if truncated:
            text = text[:_PDF_TEXT_CAP_BYTES] + "\n[truncated]"
        return json.dumps({
            "type": "text",
            "content": text,
            "cached_path": str(dest),
            "truncated": truncated,
        })
    except Exception as e:
        raise ToolError(f"PDF text extraction failed: {e} (the file is downloaded: cached_path {dest})") from e


def _encode(img: Image.Image, fmt: str, **params) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **params)
    return buf.getvalue()


def _image_preview(img: Image.Image) -> tuple[bytes, str, tuple[int, int]]:
    """A copy the model can see: upright, long edge <= _PREVIEW_MAX_EDGE, under the byte budget.

    PNG when the picture has transparency and fits the budget, otherwise JPEG; a copy that
    still does not fit is stepped down in quality, then in size.
    """
    transparent = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    frame = ImageOps.exif_transpose(img).convert("RGBA" if transparent else "RGB")
    if transparent and frame.getchannel("A").getextrema()[0] == 255:
        # An alpha channel with nothing see-through in it (most RGBA screenshots).
        transparent = False
        frame = frame.convert("RGB")
    frame.thumbnail((_PREVIEW_MAX_EDGE, _PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
    while True:
        if transparent:
            data = _encode(frame, "PNG")
            if len(data) <= _PREVIEW_MAX_BYTES:
                return data, "image/png", frame.size
            flat = Image.new("RGB", frame.size, (255, 255, 255))
            flat.paste(frame, mask=frame.getchannel("A"))
        else:
            flat = frame
        for quality in _PREVIEW_JPEG_QUALITIES:
            data = _encode(flat, "JPEG", quality=quality, optimize=True)
            if len(data) <= _PREVIEW_MAX_BYTES:
                return data, "image/jpeg", flat.size
        frame = frame.resize(
            (max(1, frame.width * 3 // 4), max(1, frame.height * 3 // 4)),
            Image.Resampling.LANCZOS,
        )


def _handle_image(dest: Path, size_bytes: int) -> CallToolResult:
    try:
        with Image.open(dest) as img:
            meta = {
                "type": "image",
                "cached_path": str(dest),
                "content_type": img.get_format_mimetype(),
                "size_bytes": size_bytes,
                "width": img.width,
                "height": img.height,
            }
            data, mime_type, (width, height) = _image_preview(img)
    except Exception as e:
        raise ToolError(f"Image processing failed: {e} (the file is downloaded: cached_path {dest})") from e
    meta["preview"] = {
        "content_type": mime_type,
        "width": width,
        "height": height,
        "size_bytes": len(data),
    }
    text = json.dumps(meta)
    # An image block, not base64 inside the text: a client shows it to the model, where a
    # few hundred KB of base64 text overflowed the client's tool-output limit. The
    # structured copy is the declared {"result": str} output every other answer carries.
    return CallToolResult(
        content=[
            TextContent(type="text", text=text),
            ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime_type),
        ],
        structuredContent={"result": text},
    )


def register_attachment_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_list_attachments(ticket_id: int) -> str:
        """List all attachments across all comments for a Zendesk ticket. Returns filename, content type, size, and download URL for each. Files a customer sent through Zendesk Messaging (Messenger, in-app chat) are not comment attachments; they are listed from the transcript with "source": "messaging_transcript" and the uploader and time when the transcript names them. Use zendesk_download_attachment to fetch file contents."""
        return _list_attachments_data(ticket_id)

    @mcp.tool()
    def zendesk_download_attachment(
        attachment_url: str,
        filename: str,
        ticket_id: int,
        dest_dir: str | None = None,
    ) -> Annotated[CallToolResult, str]:
        """Download a Zendesk attachment or Messaging upload. Obtain attachment_url and filename from zendesk_list_attachments. ticket_id is required for cache organization. Optional dest_dir overrides the default cache location; the file is written there and (for archives) extracted alongside it. Archives return a file list and unpack_dir — read individual files with your normal file tools. PDFs return up to ~500KB of extracted text. Images return the picture itself (a downscaled copy, long edge at most 1568 px) and its metadata; the original is saved at cached_path."""
        return _download_attachment_data(attachment_url, filename, ticket_id, dest_dir)
