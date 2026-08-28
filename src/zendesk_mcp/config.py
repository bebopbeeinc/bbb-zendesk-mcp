import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock


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


def attachment_cache_dir(ticket_id: int, config_file: Path | None = None) -> Path:
    cfg = load_config(config_file)
    base = cfg.get("attachment_cache_dir", "~/.cache/zendesk-mcp/attachments")
    return Path(base).expanduser() / str(ticket_id)
