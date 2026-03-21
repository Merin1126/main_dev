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


def load_google_api_key():
    path = _secrets_file()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("google_vision_api_key", "")).strip()
    except Exception:
        return ""


def save_google_api_key(api_key):
    _ensure_secret_path()
    path = _secrets_file()
    payload = {"google_vision_api_key": api_key.strip()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_google_api_key():
    path = _secrets_file()
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def mask_api_key(api_key):
    value = (api_key or "").strip()
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
