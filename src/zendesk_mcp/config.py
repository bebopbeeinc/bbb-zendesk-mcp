import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock


def headless_read_only() -> bool:
    mode = os.environ.get("ZENDESK_AUTH_MODE", "").strip()
    if mode not in ("", "client_credentials"):
        raise ValueError("ZENDESK_AUTH_MODE must be unset or client_credentials")
    return mode == "client_credentials"


def require_read_method(method: str) -> None:
    if headless_read_only() and method.upper() not in {"GET", "HEAD"}:
        raise ValueError("Headless OAuth permits only read-only Zendesk requests")


def config_path() -> Path:
    return Path.home() / ".config" / "zendesk-mcp" / "config.json"


def config_lock_path(path: Path | None = None) -> Path:
    resolved = path or config_path()
    return resolved.with_name(f".{resolved.name}.lock")


def config_file_lock(
    path: Path | None = None, timeout: float = -1
) -> FileLock:
    """Return the shared lock for credential read-modify-write transactions."""
    lock_path = config_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_path), timeout=timeout)


def load_config(path: Path | None = None) -> dict:
    if headless_read_only():
        # Never inherit an interactive user's grant, or write rotating tokens to disk.
        return {"subdomain": os.environ.get("ZENDESK_SUBDOMAIN", "").strip()}
    resolved = path or config_path()
    try:
        return json.loads(resolved.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(data: dict, path: Path | None = None) -> None:
    resolved = path or config_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        dir=resolved.parent,
        prefix=f".{resolved.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, resolved)
        resolved.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


ATTACHMENT_ROOT_ENV = "ZENDESK_MCP_ATTACHMENT_ROOT"
_DEFAULT_ATTACHMENT_CACHE = "~/.cache/zendesk-mcp/attachments"


class AttachmentRootError(ValueError):
    """ZENDESK_MCP_ATTACHMENT_ROOT is unusable, or a write would land outside it."""


def attachment_root() -> Path | None:
    """The directory every attachment write must stay inside, symlinks resolved.

    When unset, use the default cache in headless mode and no confinement otherwise.
    A blank or relative setting is an error.
    """
    raw = os.environ.get(ATTACHMENT_ROOT_ENV)
    if raw is None:
        if not headless_read_only():
            return None
        raw = _DEFAULT_ATTACHMENT_CACHE
    root = Path(raw.strip()).expanduser()
    if not raw.strip() or not root.is_absolute():
        raise AttachmentRootError(
            f"{ATTACHMENT_ROOT_ENV} must be an absolute directory path, got {raw!r}"
        )
    return Path(os.path.realpath(root))


def attachment_cache_dir(ticket_id: int, config_file: Path | None = None) -> Path:
    root = attachment_root()
    if root is not None:
        return root / str(ticket_id)
    cfg = load_config(config_file)
    base = cfg.get("attachment_cache_dir", _DEFAULT_ATTACHMENT_CACHE)
    return Path(base).expanduser() / str(ticket_id)
