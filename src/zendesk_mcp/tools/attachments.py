import base64
import json
import tarfile
import zipfile
from pathlib import Path

import pdfplumber
from PIL import Image

from zendesk_mcp.client import get_client, ConfigError
from zendesk_mcp.config import attachment_cache_dir
from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.errors import ToolError


def _list_attachments_data(ticket_id: int) -> str:
    try:
        client = get_client()
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


def _download_attachment_data(
    attachment_url: str,
    filename: str,
    ticket_id: int,
    dest_dir: str | None = None,
) -> str:
    try:
        if dest_dir:
            target_dir = Path(dest_dir).expanduser()
        else:
            target_dir = attachment_cache_dir(ticket_id)
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise ToolError(f"Cannot create the download directory: {e}") from e
    # Strip path components from filename to prevent directory traversal
    safe_filename = Path(filename).name
    dest = target_dir / safe_filename

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
        return _handle_zip(dest)

    if suffix in {".tar", ".gz", ".tgz"} or filename.endswith(".tar.gz"):
        return _handle_tar(dest)

    if suffix == ".pdf":
        return _handle_pdf(dest)

    if suffix in _IMAGE_EXTENSIONS:
        return _handle_image(dest)

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


def _handle_zip(dest: Path) -> str:
    unpack_dir = dest.parent / dest.stem
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


def _handle_tar(dest: Path) -> str:
    unpack_dir = dest.parent / dest.stem.replace(".tar", "")
    try:
        with tarfile.open(dest) as tf:
            safe_members = [
                m for m in tf.getmembers()
                if (unpack_dir / m.name).resolve().parts[:len(unpack_dir.resolve().parts)] == unpack_dir.resolve().parts
            ]
            tf.extractall(unpack_dir, members=safe_members)
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


def _handle_image(dest: Path) -> str:
    try:
        import io
        with Image.open(dest) as img:
            buf = io.BytesIO()
            img.save(buf, format=img.format or "PNG")
            data = base64.b64encode(buf.getvalue()).decode()
        return json.dumps({
            "type": "image",
            "encoding": "base64",
            "data": data,
            "cached_path": str(dest),
        })
    except Exception as e:
        raise ToolError(f"Image processing failed: {e} (the file is downloaded: cached_path {dest})") from e


def register_attachment_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_list_attachments(ticket_id: int) -> str:
        """List all attachments across all comments for a Zendesk ticket. Returns filename, content type, size, and download URL for each. Use zendesk_download_attachment to fetch file contents."""
        return _list_attachments_data(ticket_id)

    @mcp.tool()
    def zendesk_download_attachment(
        attachment_url: str,
        filename: str,
        ticket_id: int,
        dest_dir: str | None = None,
    ) -> str:
        """Download a Zendesk attachment. Obtain attachment_url and filename from zendesk_list_attachments. ticket_id is required for cache organization. Optional dest_dir overrides the default cache location; the file is written there and (for archives) extracted alongside it. Archives return a file list and unpack_dir — read individual files with your normal file tools. PDFs return up to ~500KB of extracted text. Images are base64-encoded."""
        return _download_attachment_data(attachment_url, filename, ticket_id, dest_dir)
