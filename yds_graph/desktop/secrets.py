"""The optional API key for the review app.

Stub mode is the default and needs no key. A saved key stays on this computer:
the operating system keychain when that works, otherwise a file in the app
data folder. The key is never written into the checkout and never returned to
the page.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .. import config
from .errors import ServiceError


SERVICE_NAME = "yds-review"
ACCOUNT_NAME = "anthropic_api_key"


def settings_path() -> Path:
    return config.home() / "settings.json"


def secrets_path() -> Path:
    return config.home() / "secrets.env"


def load_settings() -> dict:
    path = settings_path()
    if not path.is_file():
        return {"mode": "stub"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"mode": "stub"}
    if not isinstance(data, dict):
        return {"mode": "stub"}
    mode = data.get("mode")
    if mode not in ("stub", "anthropic"):
        mode = "stub"
    return {"mode": mode}


def _load_keyring():
    """Return the keyring module, or None when this computer has no keychain."""
    try:
        import keyring
    except Exception:
        return None
    try:
        keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception:
        return None
    return keyring


def _read_file_key() -> str | None:
    path = secrets_path()
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("ANTHROPIC_API_KEY="):
            continue
        value = line.split("=", 1)[1].strip()
        if value and not config._is_placeholder(value):
            return value
    return None


def read_api_key() -> str | None:
    ring = _load_keyring()
    if ring is not None:
        try:
            stored = ring.get_password(SERVICE_NAME, ACCOUNT_NAME)
        except Exception:
            stored = None
        if stored and not config._is_placeholder(stored):
            return stored
    return _read_file_key()


def storage_kind() -> str:
    if read_api_key() is None:
        return "none"
    ring = _load_keyring()
    if ring is not None:
        try:
            stored = ring.get_password(SERVICE_NAME, ACCOUNT_NAME)
        except Exception:
            stored = None
        if stored and not config._is_placeholder(stored):
            return "keychain"
    if _read_file_key():
        return "file"
    return "none"


def _tighten(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _write_file_key(key: str) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
    _tighten(path)


def _delete_file_key() -> None:
    path = secrets_path()
    if path.exists():
        path.unlink()


def store_api_key(key: str) -> str:
    ring = _load_keyring()
    if ring is not None:
        try:
            ring.set_password(SERVICE_NAME, ACCOUNT_NAME, key)
        except Exception:
            _write_file_key(key)
            return "file"
        _delete_file_key()
        return "keychain"
    _write_file_key(key)
    return "file"


def delete_api_key() -> None:
    ring = _load_keyring()
    if ring is not None:
        try:
            ring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        except Exception:
            pass
    _delete_file_key()


def _check_key(key: str) -> str:
    text = key.strip()
    if not text:
        raise ServiceError("Paste a key, or stay on the offline stub.")
    if "\n" in text or "\r" in text:
        raise ServiceError("The key cannot contain a line break.")
    if config._is_placeholder(text):
        raise ServiceError(
            "That key is the example placeholder. Paste a real key, or stay on the offline stub."
        )
    return text


def public_settings() -> dict:
    settings = load_settings()
    key = read_api_key()
    chosen = settings.get("mode", "stub")
    effective = "anthropic" if chosen == "anthropic" and key else "stub"
    where = storage_kind()
    if where == "keychain":
        storage_label = "Saved in the keychain on this computer."
    elif where == "file":
        storage_label = "Saved in a file in the app data folder on this computer."
    else:
        storage_label = "No key is saved. The app is using the offline stub."
    return {
        "mode": chosen,
        "effective_mode": effective,
        "key_saved": key is not None,
        "key_storage": where,
        "storage_label": storage_label,
        "data_dir": str(config.home()),
        "outbox_dir": str(config.outbox_dir()),
        "version": config.PIPELINE_VERSION,
    }


def apply_model_mode() -> dict:
    """Make this process match the saved choice.

    Stub mode sets YDS_GRAPH_STUB and drops any key from the process
    environment. Anthropic mode sets the key and sets YDS_GRAPH_STUB to an
    empty string so a later read of a dotenv file cannot turn the stub back on.
    """
    info = public_settings()
    if info["effective_mode"] == "anthropic":
        key = read_api_key()
        os.environ["YDS_GRAPH_STUB"] = ""
        os.environ["YDS_MODEL_BACKEND"] = config.BACKEND_ANTHROPIC
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
    else:
        os.environ["YDS_GRAPH_STUB"] = "1"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if os.environ.get("YDS_MODEL_BACKEND") == config.BACKEND_ANTHROPIC:
            os.environ.pop("YDS_MODEL_BACKEND", None)
    return public_settings()


def save_settings(mode: str, api_key: str | None = None, clear_key: bool = False) -> dict:
    chosen = mode if mode in ("stub", "anthropic") else "stub"
    if clear_key:
        delete_api_key()
        chosen = "stub"
    elif api_key is not None and str(api_key).strip():
        store_api_key(_check_key(str(api_key)))
    if chosen == "anthropic" and read_api_key() is None:
        raise ServiceError("Save a key before leaving the offline stub.")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": chosen}) + "\n", encoding="utf-8")
    _tighten(path)
    return apply_model_mode()
