import json
import os


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _secrets_dir():
    return os.path.join(_project_root(), ".secrets")


def _secrets_file():
    return os.path.join(_secrets_dir(), "api_config.json")


def _ensure_secret_path():
    secret_dir = _secrets_dir()
    if not os.path.exists(secret_dir):
        os.makedirs(secret_dir, exist_ok=True)
    try:
        os.chmod(secret_dir, 0o700)
    except OSError:
        pass


def _read_config():
    path = _secrets_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(payload):
    _ensure_secret_path()
    path = _secrets_file()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_google_api_key():
    data = _read_config()
    value = data.get("google_gemini_api_key") or data.get("google_vision_api_key") or ""
    return str(value).strip()


def save_google_api_key(api_key):
    payload = _read_config()
    payload["google_gemini_api_key"] = api_key.strip()
    payload["google_vision_api_key"] = api_key.strip()  # 向后兼容旧读取键
    _write_config(payload)


def clear_google_api_key():
    payload = _read_config()
    changed = False
    for key in ("google_gemini_api_key", "google_vision_api_key"):
        if key in payload:
            payload.pop(key, None)
            changed = True
    if not changed:
        return False
    try:
        if payload:
            _write_config(payload)
        else:
            os.remove(_secrets_file())
        return True
    except OSError:
        return False


def load_trace_config():
    data = _read_config()
    return {
        "enabled": bool(data.get("trace_gemini_io_enabled", False)),
        "include_full_text": bool(data.get("trace_gemini_io_full_text", True)),
    }


def save_trace_config(*, enabled=None, include_full_text=None):
    payload = _read_config()
    if enabled is not None:
        payload["trace_gemini_io_enabled"] = bool(enabled)
    if include_full_text is not None:
        payload["trace_gemini_io_full_text"] = bool(include_full_text)
    _write_config(payload)


def mask_api_key(api_key):
    value = (api_key or "").strip()
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
