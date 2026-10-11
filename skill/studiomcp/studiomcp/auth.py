"""Load Open Cloud credentials without embedding secrets in project files."""
import json
import os
from pathlib import Path

AUTH_PATH = Path.home() / '.studiomcp' / 'auth.json'
FALLBACK_AUTH_PATH = Path.home() / '.romcp' / 'auth.json'


def api_key():
    key = os.environ.get('ROBLOX_API_KEY')
    if key:
        return key
    try:
        path = AUTH_PATH if AUTH_PATH.is_file() else FALLBACK_AUTH_PATH
        settings = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError(f'Cannot read credentials from {AUTH_PATH}; expected JSON with an api_key string') from None
    if not isinstance(settings, dict) or not isinstance(settings.get('api_key'), str) or not settings['api_key'].strip():
        raise ValueError(f'{AUTH_PATH} must contain a nonempty api_key string')
    return settings['api_key'].strip()


def default_creator_id():
    cid = os.environ.get('ROBLOX_CREATOR_ID')
    if cid:
        return str(cid).strip()
    try:
        path = AUTH_PATH if AUTH_PATH.is_file() else FALLBACK_AUTH_PATH
        settings = json.loads(path.read_text(encoding='utf-8-sig'))
        val = settings.get('creator_id') or settings.get('creatorId') or settings.get('user_id') or settings.get('userId')
        if val:
            return str(val).strip()
    except Exception:
        pass
    return None


def default_universe_id():
    uid = os.environ.get('ROBLOX_UNIVERSE_ID')
    if uid:
        return str(uid).strip()
    try:
        path = AUTH_PATH if AUTH_PATH.is_file() else FALLBACK_AUTH_PATH
        settings = json.loads(path.read_text(encoding='utf-8-sig'))
        val = settings.get('universe_id') or settings.get('universeId')
        if val:
            return str(val).strip()
    except Exception:
        pass
    return None
