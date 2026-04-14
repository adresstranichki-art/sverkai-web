#!/usr/bin/env python3
"""
manage_keys.py — управление белым списком API-ключей для sverkAI.

Использование:
  python manage_keys.py add    sk-ant-...  "Иванов Иван"
  python manage_keys.py add-guest sk-ant-... "Общий гостевой ключ"
  python manage_keys.py remove sk-ant-...
  python manage_keys.py list
  python manage_keys.py hash   sk-ant-...   # только показать хеш, не добавлять

Либо через HTTP-эндпоинты (нужна переменная ADMIN_SECRET):
  POST /api/admin/add-key    {"admin_secret":"...", "api_key":"sk-ant-...", "label":"..."}
  POST /api/admin/remove-key {"admin_secret":"...", "api_key":"sk-ant-..."}
  GET  /api/admin/list-keys  (заголовок X-Admin-Secret: ...)
"""
import sys, json, hashlib
from pathlib import Path
from datetime import datetime

_DATA_DIR  = Path(__file__).parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_KEYS_FILE = _DATA_DIR / "allowed_keys.json"


def _user_id(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()[:24]


def load() -> list:
    if _KEYS_FILE.exists():
        try:
            return json.loads(_KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save(entries: list) -> None:
    _KEYS_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cmd_list():
    entries = load()
    if not entries:
        print("Белый список пуст: пользовательский вход закрыт, кроме явного SVERKAI_AUTH_ALLOW_ALL=1.")
        return
    print(f"{'#':<4} {'Метка':<25} {'Роль':<8} {'Вкл':<5} {'Хеш':<28} {'Добавлен'}")
    print("-" * 88)
    for i, e in enumerate(entries, 1):
        print(
            f"{i:<4} {e.get('label','—'):<25} {e.get('role','user'):<8} "
            f"{str(e.get('enabled', True)):<5} {e.get('hash',''):<28} {e.get('added','')}"
        )
    print(f"\nВсего: {len(entries)}")


def cmd_add(key: str, label: str = "", role: str = "user"):
    h = _user_id(key)
    role = role if role in {"user", "guest"} else "user"
    entries = load()
    if any(e.get("hash") == h and e.get("role", "user") == role for e in entries):
        print(f"⚠  Ключ уже в списке (хеш {h})")
        return
    entries.append({
        "hash": h,
        "label": label or "—",
        "role": role,
        "enabled": True,
        "added": datetime.now().strftime("%Y-%m-%d"),
    })
    save(entries)
    print(f"✓ Добавлен: хеш={h}, роль={role}, метка='{label or '—'}'  (всего {len(entries)})")


def cmd_remove(key: str):
    h = _user_id(key)
    entries = load()
    before  = len(entries)
    entries = [e for e in entries if e.get("hash") != h]
    if len(entries) == before:
        print(f"⚠  Ключ не найден в списке (хеш {h})")
    else:
        save(entries)
        print(f"✓ Удалён: хеш={h}  (осталось {len(entries)})")


def cmd_hash(key: str):
    print(f"SHA256[:24] = {_user_id(key)}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    cmd = args[0].lower()
    if cmd == "list":
        cmd_list()
    elif cmd == "add" and len(args) >= 2:
        cmd_add(args[1], " ".join(args[2:]), "user")
    elif cmd == "add-guest" and len(args) >= 2:
        cmd_add(args[1], " ".join(args[2:]), "guest")
    elif cmd == "remove" and len(args) >= 2:
        cmd_remove(args[1])
    elif cmd == "hash" and len(args) >= 2:
        cmd_hash(args[1])
    else:
        print(__doc__)
