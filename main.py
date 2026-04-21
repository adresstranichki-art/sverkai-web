"""sverkAI v2.1 — веб-версия (FastAPI) с поддержкой личных API-ключей"""
import os, re, json, tempfile, shutil, uuid, hashlib, base64, io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pdfplumber
import xlsxwriter
from anthropic import Anthropic

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Настройки ────────────────────────────────────────────────────────────────
MODEL_MAIN = "claude-sonnet-4-6"
MODEL_FAST = "claude-haiku-4-5-20251001"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AUTH_ALLOW_ALL = os.environ.get("SVERKAI_AUTH_ALLOW_ALL", "").strip().lower() in {"1", "true", "yes", "on"}
APP_ENV = os.environ.get("SVERKAI_ENV") or os.environ.get("RAILWAY_ENVIRONMENT_NAME") or "local"
APP_VERSION = (
    os.environ.get("SVERKAI_VERSION")
    or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
    or os.environ.get("GIT_COMMIT")
    or ""
)[:12]

app = FastAPI(title="sverkAI API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_REPORT_DIR = Path(tempfile.gettempdir()) / "sverkai_reports"
_REPORT_DIR.mkdir(exist_ok=True)
_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_HISTORY_FILE      = _DATA_DIR / "history.json"         # legacy (не используется)
_ALLOWED_KEYS_FILE = _DATA_DIR / "allowed_keys.json"    # белый список (хеши ключей)
_GUEST_USAGE_FILE  = _DATA_DIR / "guest_usage.json"     # счетчик гостевых сверок без документов
ADMIN_SECRET       = os.environ.get("ADMIN_SECRET", "") # для управления белым списком


def _env_int(name: str, default: int, min_value: int = 0, max_value: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, default))
    except Exception:
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_mb(name: str, default: float) -> int:
    try:
        mb = float(os.environ.get(name, default))
    except Exception:
        mb = default
    return max(0, int(mb * 1024 * 1024))


GUEST_RECONCILE_LIMIT = _env_int("SVERKAI_GUEST_RECONCILE_LIMIT", 2, 0, 50)
GUEST_USAGE_WINDOW_DAYS = _env_int("SVERKAI_GUEST_USAGE_WINDOW_DAYS", 30, 1, 365)
GUEST_MAX_FILE_BYTES = _env_mb("SVERKAI_GUEST_MAX_FILE_MB", 2)
USER_MAX_FILE_BYTES = _env_mb("SVERKAI_USER_MAX_FILE_MB", 10)
SUPPORTED_UPLOAD_EXTS = {".xlsx", ".xls", ".pdf"}
SUPPORTED_UPLOAD_EXTS_LABEL = ".xlsx, .xls и .pdf"


# ════════════════════════════════════════════════════════════════════
#  АВТОРИЗАЦИЯ / ИСТОРИЯ ПО ПОЛЬЗОВАТЕЛЯМ
# ════════════════════════════════════════════════════════════════════

def _user_id(api_key: str) -> str:
    """Хеш API-ключа — безопасный идентификатор пользователя."""
    return hashlib.sha256(api_key.strip().encode()).hexdigest()[:24]


def _normalize_key_entries(entries: list, source: str = "file") -> list:
    """Приводит записи whitelist к единому виду.

    role=user разрешает вход в приложение, role=guest документирует общий ключ и
    запрещает использовать его как пользовательскую учетку.
    """
    normalized = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        h = str(entry.get("hash", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{24,64}", h):
            continue
        role = str(entry.get("role") or "user").strip().lower()
        if role not in {"user", "guest"}:
            role = "user"
        normalized.append({
            "hash": h[:24],
            "label": str(entry.get("label") or "—"),
            "role": role,
            "enabled": entry.get("enabled", True) is not False,
            "added": str(entry.get("added") or ""),
            "source": source,
        })
    return normalized


def _parse_env_key_hashes(raw: str, source: str) -> list:
    """Парсит SVERKAI_ALLOWED_KEY_HASHES: hash или hash:label через запятую/перенос."""
    entries = []
    for item in re.split(r"[\n,;]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        h = parts[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{24,64}", h):
            continue
        entries.append({
            "hash": h[:24],
            "label": parts[1].strip() if len(parts) > 1 and parts[1].strip() else source,
            "role": parts[2].strip().lower() if len(parts) > 2 and parts[2].strip() else "user",
            "enabled": True,
            "added": "env",
        })
    return _normalize_key_entries(entries, source)


def _load_allowed_keys_from_file() -> list:
    """Загружает белый список из файла. Каждая запись: {hash, label, role, enabled, added}."""
    if _ALLOWED_KEYS_FILE.exists():
        try:
            with open(_ALLOWED_KEYS_FILE, encoding="utf-8") as f:
                return _normalize_key_entries(json.load(f), "file")
        except Exception:
            pass
    return []


def _load_allowed_keys_from_env() -> list:
    entries = []
    for name in ("SVERKAI_ALLOWED_KEY_HASHES", "ALLOWED_API_KEY_HASHES"):
        entries.extend(_parse_env_key_hashes(os.environ.get(name, ""), name))
    return entries


def _load_allowed_keys() -> list:
    """Возвращает белый список из файла и env-переменных без реальных ключей."""
    seen = set()
    merged = []
    for entry in [*_load_allowed_keys_from_file(), *_load_allowed_keys_from_env()]:
        key = (entry["hash"], entry["role"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def _save_allowed_keys(entries: list) -> None:
    entries = _normalize_key_entries(entries, "file")
    for entry in entries:
        entry.pop("source", None)
    with open(_ALLOWED_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _guest_key_hashes() -> set[str]:
    hashes = set()
    if ANTHROPIC_API_KEY.strip():
        hashes.add(_user_id(ANTHROPIC_API_KEY))
    for name in ("SVERKAI_GUEST_KEY_HASHES", "GUEST_API_KEY_HASHES"):
        for entry in _parse_env_key_hashes(os.environ.get(name, ""), name):
            hashes.add(entry["hash"])
    for entry in _load_allowed_keys():
        if entry.get("role") == "guest" and entry.get("enabled", True):
            hashes.add(entry["hash"])
    return hashes


def _key_access_status(api_key: str) -> tuple[bool, str]:
    """Проверяет, может ли ключ быть пользовательским логином SverkAI."""
    key = (api_key or "").strip()
    if not key:
        return False, "missing"
    if not key.startswith("sk-ant-"):
        return False, "bad_format"
    h = _user_id(key)
    if h in _guest_key_hashes():
        return False, "guest_key"
    entries = _load_allowed_keys()
    user_entries = [e for e in entries if e.get("role", "user") == "user" and e.get("enabled", True)]
    if not user_entries:
        return (True, "dev_allow_all") if AUTH_ALLOW_ALL else (False, "no_allowlist")
    return (True, "allowed") if any(e.get("hash") == h for e in user_entries) else (False, "not_allowed")


def _is_key_allowed(api_key: str) -> bool:
    return _key_access_status(api_key)[0]


def _key_error_message(reason: str) -> str:
    messages = {
        "missing": "Ключ не указан",
        "bad_format": "Неверный формат ключа",
        "guest_key": "Этот ключ используется как общий гостевой доступ и не может быть пользовательским входом.",
        "no_allowlist": "Вход по личным ключам временно закрыт: список разрешенных ключей не настроен.",
        "not_allowed": "Ключ не входит в список разрешенных. Обратитесь к администратору SverkAI.",
    }
    return messages.get(reason, "Ключ не прошел проверку доступа")


def _authorized_user_key_or_raise(request: Request) -> str:
    """Возвращает пользовательский ключ или пустую строку для гостя.

    Если клиент прислал X-Api-Key, он обязан быть разрешенным пользовательским
    ключом. Это закрывает обход формы входа прямыми API-запросами.
    """
    user_key = _get_user_key(request)
    if not user_key:
        return ""
    ok, reason = _key_access_status(user_key)
    if not ok:
        raise HTTPException(status_code=403, detail=_key_error_message(reason))
    return user_key

def _user_history_file(api_key: str) -> Path:
    return _DATA_DIR / f"history_{_user_id(api_key)}.json"

def _load_user_history(api_key: str = "") -> list:
    if not api_key or not api_key.strip():
        return []
    f = _user_history_file(api_key)
    if f.exists():
        try:
            with open(f, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return []

def _save_user_history(history: list, api_key: str = "") -> None:
    """Сохраняет историю только для авторизованных пользователей."""
    if not api_key or not api_key.strip():
        return
    f = _user_history_file(api_key)
    with open(f, "w", encoding="utf-8") as fp:
        json.dump(history[-50:], fp, ensure_ascii=False, indent=2)


def _clear_user_history(api_key: str = "") -> None:
    """Очищает историю текущего авторизованного пользователя."""
    if not api_key or not api_key.strip():
        return
    f = _user_history_file(api_key)
    if f.exists():
        f.unlink()

def _get_user_key(request: Request) -> str:
    """Извлекает личный API-ключ пользователя из заголовка запроса."""
    return request.headers.get("X-Api-Key", "").strip()

def _effective_key(user_key: str) -> str:
    """Возвращает ключ пользователя, либо системный как fallback."""
    return user_key if user_key else ANTHROPIC_API_KEY


def _format_bytes(size: int) -> str:
    mb = size / 1024 / 1024
    if mb >= 1:
        text = f"{mb:.1f}".rstrip("0").rstrip(".")
        return f"{text} МБ"
    kb = max(1, round(size / 1024))
    return f"{kb} КБ"


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"


def _guest_subject(request: Request) -> str:
    browser_id = request.headers.get("X-Guest-Id", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{12,96}", browser_id):
        browser_id = "no-browser-id"
    raw = f"{_client_ip(request)}|{browser_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _load_guest_usage() -> dict:
    if _GUEST_USAGE_FILE.exists():
        try:
            with open(_GUEST_USAGE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_guest_usage(data: dict) -> None:
    with open(_GUEST_USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _guest_usage_entry(data: dict, subject: str, now: datetime) -> dict:
    entry = data.get(subject) if isinstance(data.get(subject), dict) else {}
    try:
        start = datetime.fromisoformat(str(entry.get("period_start", "")))
    except Exception:
        start = now
        entry = {}
    if now - start >= timedelta(days=GUEST_USAGE_WINDOW_DAYS):
        entry = {}
        start = now
    entry.setdefault("period_start", start.isoformat(timespec="seconds"))
    entry["used"] = max(0, int(entry.get("used") or 0))
    data[subject] = entry
    return entry


def _guest_usage_status(request: Request) -> dict:
    if GUEST_RECONCILE_LIMIT <= 0:
        return {"limit": 0, "used": 0, "remaining": None, "window_days": GUEST_USAGE_WINDOW_DAYS}
    data = _load_guest_usage()
    now = datetime.now()
    entry = _guest_usage_entry(data, _guest_subject(request), now)
    used = entry["used"]
    remaining = max(0, GUEST_RECONCILE_LIMIT - used)
    return {
        "limit": GUEST_RECONCILE_LIMIT,
        "used": used,
        "remaining": remaining,
        "window_days": GUEST_USAGE_WINDOW_DAYS,
        "reset_at": (
            datetime.fromisoformat(entry["period_start"]) + timedelta(days=GUEST_USAGE_WINDOW_DAYS)
        ).isoformat(timespec="seconds"),
    }


def _guest_limit_or_raise(request: Request) -> None:
    status = _guest_usage_status(request)
    if status["limit"] > 0 and status["remaining"] <= 0:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Гостевой лимит исчерпан: доступны {status['limit']} бесплатные сверки "
                f"за {status['window_days']} дней. Для продолжения войдите с выданным тестовым доступом."
            ),
        )


def _record_guest_reconcile(request: Request) -> dict:
    if GUEST_RECONCILE_LIMIT <= 0:
        return _guest_usage_status(request)
    data = _load_guest_usage()
    now = datetime.now()
    subject = _guest_subject(request)
    entry = _guest_usage_entry(data, subject, now)
    entry["used"] += 1
    cutoff = now - timedelta(days=GUEST_USAGE_WINDOW_DAYS * 2)
    for key, value in list(data.items()):
        try:
            if datetime.fromisoformat(str(value.get("period_start", ""))) < cutoff:
                data.pop(key, None)
        except Exception:
            data.pop(key, None)
    _save_guest_usage(data)
    used = entry["used"]
    return {
        "limit": GUEST_RECONCILE_LIMIT,
        "used": used,
        "remaining": max(0, GUEST_RECONCILE_LIMIT - used),
        "window_days": GUEST_USAGE_WINDOW_DAYS,
        "reset_at": (
            datetime.fromisoformat(entry["period_start"]) + timedelta(days=GUEST_USAGE_WINDOW_DAYS)
        ).isoformat(timespec="seconds"),
    }


async def _save_upload_to_path(upload: UploadFile, path: str, user_key: str, label: str) -> int:
    filename = upload.filename or label
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_UPLOAD_EXTS:
        raise HTTPException(status_code=400, detail=f"Поддерживаются только файлы {SUPPORTED_UPLOAD_EXTS_LABEL}")
    max_bytes = USER_MAX_FILE_BYTES if user_key else GUEST_MAX_FILE_BYTES
    total = 0
    with open(path, "wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes and total > max_bytes:
                mode = "В гостевом режиме" if not user_key else "Для текущего доступа"
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{label} слишком большой ({_format_bytes(total)}). "
                        f"{mode} можно загружать файлы до {_format_bytes(max_bytes)}."
                    ),
                )
            f.write(chunk)
    if total <= 0:
        raise HTTPException(status_code=400, detail=f"{label} пустой или не загрузился")
    return total


# ════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════════════════════════════

def _to_float(s: str) -> Optional[float]:
    if s is None or (not isinstance(s, str) and pd.isna(s)):
        return None
    if not s or s == 'nan':
        return None
    try:
        cleaned = str(s).replace('\u2212', '-').replace(',', '.').replace(' ', '').replace('\xa0', '')
        v = float(cleaned)
        if pd.isna(v):
            return None
        return None if v == 0 else v
    except Exception:
        return None


def _normalize_doc_num(num: str) -> str:
    if not num:
        return num
    cleaned = re.sub(r'^[А-ЯA-Zа-яa-z]+-', '', str(num).strip())
    cleaned = cleaned.lstrip('0') or cleaned
    return cleaned.lower()


_DOC_NUM_PATTERNS = (
    re.compile(r'\bРГО\s*([A-Za-zА-Яа-я]*-?\d+[\w/]*)', re.IGNORECASE),
    re.compile(r'(?:сч[её]т[-\s]?фактура|упд)\s*[№#]?\s*([A-Za-zА-Яа-я]*-?\d+[\w/]*)', re.IGNORECASE),
    re.compile(r'\(([A-Za-zА-Яа-я]*-?\d+[\w/-]*)\s+от', re.IGNORECASE),
    re.compile(r'\b([A-Za-zА-Яа-я]+-?\d[\w/-]*)\s+от\b', re.IGNORECASE),
    re.compile(r'(?:№|#|No)\s*(М-\d+|\d[\w/-]*)', re.IGNORECASE),
    re.compile(r'\b(\d{4,})\b'),
)


def _extract_doc_num(doc: str) -> Optional[str]:
    if not doc:
        return None
    text = str(doc)
    has_full_date = bool(re.search(r'\b\d{2}\.\d{2}\.(?:19|20)\d{2}\b', text))
    for idx, pattern in enumerate(_DOC_NUM_PATTERNS):
        for match in pattern.finditer(text):
            raw_num = match.group(1)
            if idx == len(_DOC_NUM_PATTERNS) - 1 and has_full_date and re.fullmatch(r'(?:19|20)\d{2}', raw_num):
                continue
            num = _normalize_doc_num(match.group(1))
            if num:
                return num
    return None


def _extract_doc_date(doc: str) -> Optional[pd.Timestamp]:
    if not doc:
        return None
    text = str(doc)
    for pattern in (
        r'\bот\s*(\d{2}\.\d{2}\.\d{2,4})\b',
        r'\((\d{2}\.\d{2}\.\d{2,4})(?:\s*,|\))',
        r'\((\d{2}\.\d{2}\.\d{2,4})\)',
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        dt = pd.to_datetime(match.group(1), dayfirst=True, errors='coerce')
        if pd.notna(dt):
            return dt
    return None


def _extract_any_date(text: str) -> Optional[pd.Timestamp]:
    if not text:
        return None
    match = re.search(r'(\d{2}\.\d{2}\.\d{2,4})', str(text))
    if not match:
        return None
    return pd.to_datetime(match.group(1), dayfirst=True, errors='coerce')


def _extract_period_bounds(text: str) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    if not text:
        return (None, None)
    compact = ' '.join(str(text).split())
    for pattern in (
        r'за период с (\d{2}\.\d{2}\.\d{4}) по (\d{2}\.\d{2}\.\d{4})',
        r'за период[:\s]+(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})',
    ):
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            return (
                pd.to_datetime(match.group(1), dayfirst=True, errors='coerce'),
                pd.to_datetime(match.group(2), dayfirst=True, errors='coerce'),
            )
    match = re.search(r'за период[:\s]+(\d{4})\s*г', compact, re.IGNORECASE)
    if match:
        year = int(match.group(1))
        return (
            pd.Timestamp(year=year, month=1, day=1),
            pd.Timestamp(year=year, month=12, day=31),
        )
    match = re.search(r'([1-4])\s*квартал\s*(\d{4})', compact, re.IGNORECASE)
    if match:
        quarter = int(match.group(1))
        year = int(match.group(2))
        month_from = (quarter - 1) * 3 + 1
        month_to = month_from + 2
        period_from = pd.Timestamp(year=year, month=month_from, day=1)
        period_to = (pd.Timestamp(year=year, month=month_to, day=1) + pd.offsets.MonthEnd(1)).normalize()
        return period_from, period_to
    match = re.search(r'по состоянию на (\d{2}\.\d{2}\.\d{4})', compact, re.IGNORECASE)
    if match:
        return (None, pd.to_datetime(match.group(1), dayfirst=True, errors='coerce'))
    return (None, None)


def _extract_balance_meta(raw: pd.DataFrame) -> dict:
    header_rows = min(8, len(raw))
    header_text = ' '.join(
        str(v) for v in raw.iloc[:header_rows].values.flatten()
        if pd.notna(v) and str(v).strip() and str(v).strip().lower() != 'nan'
    )
    period_from, period_to = _extract_period_bounds(header_text)
    start_balance = None
    end_balance = None
    start_row_text = ''
    end_row_text = ''
    for idx in range(len(raw)):
        row_vals = [
            str(v).strip() for v in raw.iloc[idx].tolist()
            if pd.notna(v) and str(v).strip() and str(v).strip().lower() != 'nan'
        ]
        if not row_vals:
            continue
        row_text = ' '.join(row_vals).lower()
        numeric_candidates = []
        for col_idx, value in enumerate(raw.iloc[idx].tolist()):
            parsed = _to_float(value)
            if parsed is not None:
                numeric_candidates.append((col_idx, float(parsed)))
        if not numeric_candidates:
            continue
        amount_candidates = numeric_candidates
        # В строках сальдо некоторых актов по краям стоят порядковые номера строк
        # обеих сторон. Если крайние значения одинаковые и между ними есть
        # существенно большая сумма, отбрасываем эти служебные номера.
        if len(numeric_candidates) >= 3:
            first_val = numeric_candidates[0][1]
            last_val = numeric_candidates[-1][1]
            middle_candidates = numeric_candidates[1:-1]
            if (
                middle_candidates
                and abs(first_val - last_val) <= 0.01
                and abs(first_val) < max(abs(v) for _, v in middle_candidates)
            ):
                amount_candidates = middle_candidates
        amount = amount_candidates[-1][1]
        has_saldo = 'сальдо' in row_text
        if not has_saldo:
            continue
        if start_balance is None and 'началь' in row_text:
            start_balance = amount
            start_row_text = ' '.join(row_vals)
            continue
        if end_balance is None and 'конеч' in row_text:
            end_balance = amount
            end_row_text = ' '.join(row_vals)
            continue
        if 'началь' not in row_text and 'конеч' not in row_text:
            if start_balance is None:
                start_balance = amount
                start_row_text = ' '.join(row_vals)
            else:
                end_balance = amount
                end_row_text = ' '.join(row_vals)
    if period_from is None:
        period_from = _extract_any_date(start_row_text)
    if period_to is None:
        period_to = _extract_any_date(end_row_text)
    meta = {}
    if start_balance is not None:
        meta['start_balance'] = float(start_balance)
    if end_balance is not None:
        meta['end_balance'] = float(end_balance)
    if period_from is not None and pd.notna(period_from):
        meta['period_from'] = period_from
    if period_to is not None and pd.notna(period_to):
        meta['period_to'] = period_to
    return meta


def _attach_meta(df: pd.DataFrame, **meta) -> pd.DataFrame:
    for key, value in meta.items():
        if value is None:
            continue
        if pd.isna(value):
            continue
        df.attrs[key] = value
    return df


def _balance_state_doc_type(doc: str) -> str:
    text = str(doc or '').lower()
    if 'оплата' in text:
        return 'оплата'
    if 'ксф' in text or 'коррект' in text:
        return 'корректировка'
    if 'поступление тмц' in text:
        return 'поставка'
    return ''


def _balance_state_doc_num(doc: str) -> Optional[str]:
    text = str(doc or '')
    doc_type = _balance_state_doc_type(text)
    if doc_type == 'поставка':
        match = re.search(r'№\s*([A-Za-zА-Яа-я-]*\d[\w/-]*)', text, re.IGNORECASE)
        if match:
            return _normalize_doc_num(match.group(1))
        return _extract_doc_num(text)
    if doc_type == 'корректировка':
        match = re.search(r',\s*([A-Za-zА-Яа-я]+-\d+)\)', text, re.IGNORECASE)
        if match:
            return match.group(1).strip().lower().replace('-', '_')
    return None


# ════════════════════════════════════════════════════════════════════
#  ПАРСЕРЫ
# ════════════════════════════════════════════════════════════════════

def parse_proopt(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    rows = []
    meta = _extract_balance_meta(raw)
    date_re = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}$')

    layout_candidates = []
    max_date_col = min(len(raw.columns) - 1, 6)
    for candidate_date_col in range(max_date_col):
        candidate_doc_col = candidate_date_col + 1
        scan_rows = []
        for idx in range(6, min(60, len(raw))):
            date_val = str(raw.iloc[idx, candidate_date_col]).strip() if pd.notna(raw.iloc[idx, candidate_date_col]) else ''
            doc_val = str(raw.iloc[idx, candidate_doc_col]).strip() if pd.notna(raw.iloc[idx, candidate_doc_col]) else ''
            if date_re.match(date_val) and doc_val and doc_val.lower() != 'nan':
                scan_rows.append(idx)
        if scan_rows:
            layout_candidates.append((len(scan_rows), candidate_date_col, candidate_doc_col, scan_rows))

    if layout_candidates:
        _, date_col, doc_col, scan_rows = max(layout_candidates, key=lambda item: item[0])
    else:
        date_col, doc_col, scan_rows = 1, 2, []

    from collections import Counter
    col_hits: Counter = Counter()
    amount_scan_end = min(len(raw.columns), max(doc_col + 5, 8))
    for idx in scan_rows[:30]:
        for col in range(doc_col + 1, amount_scan_end):
            if _to_float(raw.iloc[idx, col]) is not None:
                col_hits[col] += 1

    if col_hits:
        amount_cols = sorted(col_hits.keys())
        if len(amount_cols) >= 2:
            debit_col, credit_col = amount_cols[0], amount_cols[1]
        else:
            debit_col = amount_cols[0]
            credit_col = doc_col + 4
    elif date_col == 2:
        debit_col, credit_col = 5, 6
    else:
        debit_col, credit_col = 4, 6

    for idx in range(6, len(raw)):
        row = raw.iloc[idx]
        date_val = str(row[date_col]).strip() if date_col < len(row) and pd.notna(row[date_col]) else ''
        doc_val  = str(row[doc_col]).strip() if doc_col < len(row) and pd.notna(row[doc_col]) else ''
        if not date_val or date_val == 'nan' or not date_re.match(date_val):
            continue
        if not doc_val or doc_val == 'nan':
            continue
        if any(kw in doc_val.lower() for kw in ['обороты', 'сальдо конечное', 'сальдо начальное']):
            continue
        debit  = str(row[debit_col]).strip() if debit_col < len(row) and pd.notna(row[debit_col]) else ''
        credit = str(row[credit_col]).strip() if credit_col < len(row) and pd.notna(row[credit_col]) else ''
        debit_val = _to_float(debit)
        credit_val = _to_float(credit)
        if debit_val is None and credit_val is None:
            continue
        doc_num = _extract_doc_num(doc_val)
        date_parsed = pd.to_datetime(date_val, dayfirst=True, errors='coerce')
        rows.append({'date': date_parsed, 'date_str': date_val, 'document': doc_val,
                     'doc_num': doc_num, 'debit': debit_val, 'credit': credit_val,
                     'match_date': _extract_doc_date(doc_val) or date_parsed,
                     'signed_amount': float(debit_val or 0) - float(credit_val or 0),
                     'raw_row': idx})
    return _attach_meta(pd.DataFrame(rows), **meta)


def parse_partner_ledger_act(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    rows = []
    meta = _extract_balance_meta(raw)
    for idx in range(12, len(raw)):
        row = raw.iloc[idx]
        doc_val = str(row[2]).strip() if pd.notna(row[2]) else ''
        if not doc_val or doc_val == 'nan':
            continue
        doc_low = doc_val.lower()
        if 'сальдо конечное' in doc_low:
            break
        if 'обороты' in doc_low or 'сальдо начальное' in doc_low:
            continue
        ref_val = str(row[4]).strip() if pd.notna(row[4]) else ''
        posted_date = str(row[5]).strip() if pd.notna(row[5]) else ''
        debit_raw = str(row[6]).strip() if pd.notna(row[6]) else ''
        credit_raw = str(row[7]).strip() if pd.notna(row[7]) else ''
        debit = _to_float(debit_raw)
        credit = _to_float(credit_raw)
        if debit is None and credit is None:
            continue
        date_parsed = pd.to_datetime(posted_date, dayfirst=True, errors='coerce') if posted_date else pd.NaT
        match_date = _extract_doc_date(doc_val) or date_parsed
        if pd.isna(date_parsed):
            date_parsed = match_date
            date_str = date_parsed.strftime('%d.%m.%Y') if pd.notna(date_parsed) else posted_date
        else:
            date_str = posted_date
        doc_num = _extract_doc_num(doc_val) or _extract_doc_num(ref_val)
        rows.append({
            'date': date_parsed,
            'date_str': date_str,
            'document': doc_val,
            'doc_num': doc_num,
            'debit': debit,
            'credit': credit,
            'match_date': match_date,
            'signed_amount': float(debit or 0) - float(credit or 0),
            'raw_row': idx,
        })
    return _attach_meta(pd.DataFrame(rows), **meta)


def parse_balance_state_act(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    rows = []
    meta = _extract_balance_meta(raw)
    for idx in range(6, len(raw)):
        row = raw.iloc[idx]
        doc_val = str(row[2]).strip() if pd.notna(row[2]) else ''
        if not doc_val or doc_val == 'nan':
            continue
        doc_low = doc_val.lower()
        if 'сальдо' in doc_low or 'обороты' in doc_low:
            continue
        debit = _to_float(row[3]) if len(row) > 3 else None
        credit = _to_float(row[4]) if len(row) > 4 else None
        if debit is None and credit is None:
            continue
        match_date = _extract_any_date(doc_val)
        date_parsed = match_date if pd.notna(match_date) else pd.NaT
        date_str = date_parsed.strftime('%d.%m.%Y') if pd.notna(date_parsed) else ''
        doc_type = _balance_state_doc_type(doc_val)
        rows.append({
            'date': date_parsed,
            'date_str': date_str,
            'document': doc_val,
            'doc_num': _balance_state_doc_num(doc_val),
            'doc_type': doc_type,
            'debit': debit,
            'credit': credit,
            'match_date': match_date,
            'signed_amount': float(credit or 0) - float(debit or 0),
            'raw_row': idx,
        })
    return _attach_meta(pd.DataFrame(rows), **meta)


def parse_emex(path: str) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None, dtype=str)
    rows = []
    meta = _extract_balance_meta(raw)
    for idx in range(12, len(raw)):
        row = raw.iloc[idx]
        date_val = str(row[0]).strip() if pd.notna(row[0]) else ''
        doc_val  = str(row[1]).strip() if pd.notna(row[1]) else ''
        if not date_val or date_val == 'nan':
            continue
        if 'справочно' in date_val.lower():
            break
        doc_type = str(row[9]).strip() if pd.notna(row[9]) else ''
        debit  = str(row[13]).strip() if pd.notna(row[13]) else ''
        credit = str(row[16]).strip() if pd.notna(row[16]) else ''
        if not doc_val or doc_val == 'nan':
            continue
        m = re.search(r'^(\d+)', doc_val)
        doc_num = m.group(1) if m else None
        date_parsed = pd.to_datetime(date_val, dayfirst=True, errors='coerce')
        debit_val = _to_float(debit)
        credit_val = _to_float(credit)
        rows.append({'date': date_parsed, 'date_str': date_val, 'document': doc_val,
                     'doc_num': doc_num, 'doc_type': doc_type,
                     'debit': debit_val, 'credit': credit_val,
                     'match_date': _extract_doc_date(doc_val) or date_parsed,
                     'signed_amount': float(debit_val or 0) - float(credit_val or 0),
                     'raw_row': idx})
    return _attach_meta(pd.DataFrame(rows), **meta)


def parse_counterparty(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    date_pattern = re.compile(r'\((\d{2}\.\d{2}\.\d{4})')
    rows = []
    meta = _extract_balance_meta(raw)
    for idx in range(6, len(raw)):
        row = raw.iloc[idx]
        doc_val = str(row[2]).strip() if pd.notna(row[2]) else ''
        if not doc_val or doc_val == 'nan':
            continue
        if any(kw in doc_val.lower() for kw in
               ['сальдо', 'обороты', 'итого', 'нижеподписавшиеся', 'наименование', 'документ', 'дебет', 'кредит']):
            continue
        if not date_pattern.search(doc_val):
            continue
        debit  = str(row[3]).strip() if pd.notna(row[3]) else ''
        credit = str(row[4]).strip() if pd.notna(row[4]) else ''
        m_date = date_pattern.search(doc_val)
        date_str = m_date.group(1) if m_date else ''
        date_parsed = pd.to_datetime(date_str, dayfirst=True, errors='coerce') if date_str else pd.NaT
        doc_num = _extract_doc_num(doc_val)
        debit_val = _to_float(debit)
        credit_val = _to_float(credit)
        rows.append({'date': date_parsed, 'date_str': date_str, 'document': doc_val,
                     'doc_num': doc_num, 'debit': debit_val, 'credit': credit_val,
                     'match_date': _extract_doc_date(doc_val) or date_parsed,
                     'signed_amount': float(credit_val or 0) - float(debit_val or 0),
                     'raw_row': idx})
    return _attach_meta(pd.DataFrame(rows), **meta)


def parse_standard_act(path: str) -> pd.DataFrame:
    """Парсит одностороннний акт сверки формата М221:
    col 0 = дата, col 1 = документ, col 10 = дебет, col 12 = кредит."""
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    date_re = re.compile(r'^\d{2}\.\d{2}\.\d{4}$')
    meta = _extract_balance_meta(raw)
    # Автодетект колонок дебет/кредит: сканируем 30 строк, ищем два чередующихся числовых столбца.
    # Столбец «сумма документа» присутствует в каждой строке → его исключаем.
    debit_col, credit_col = 10, 12
    from collections import Counter
    col_hits: Counter = Counter()
    scan_rows = []
    for i in range(6, min(50, len(raw))):
        if date_re.match(str(raw.iloc[i, 0]).strip()):
            scan_rows.append(i)
            if len(scan_rows) >= 30: break
    if scan_rows:
        for i in scan_rows:
            for c in range(2, len(raw.columns)):
                v = str(raw.iloc[i, c]).strip()
                if v in ('', 'nan', '-', '—'): continue
                try: float(v.replace(',', '.').replace(' ', '').replace('\xa0', '')); col_hits[c] += 1
                except: pass
        # столбец с максимальным числом попаданий = сумма документа → исключаем
        if col_hits:
            max_col = col_hits.most_common(1)[0][0]
            candidates = {c: n for c, n in col_hits.items() if c != max_col}
            sorted_cands = sorted(candidates.keys())
            if len(sorted_cands) >= 2:
                debit_col, credit_col = sorted_cands[0], sorted_cands[1]
            elif len(sorted_cands) == 1:
                credit_col = sorted_cands[0]
    rows = []
    for idx in range(6, len(raw)):
        row = raw.iloc[idx]
        date_val = str(row[0]).strip() if pd.notna(row[0]) else ''
        doc_val  = str(row[1]).strip() if pd.notna(row[1]) else ''
        if not date_val or date_val == 'nan' or not date_re.match(date_val): continue
        if not doc_val or doc_val == 'nan': continue
        if any(kw in doc_val.lower() for kw in ['обороты', 'сальдо конечное']): break
        d_raw = str(row[debit_col]).strip()  if debit_col  < len(row) and pd.notna(row[debit_col])  else ''
        c_raw = str(row[credit_col]).strip() if credit_col < len(row) and pd.notna(row[credit_col]) else ''
        debit  = _to_float(d_raw)
        credit = _to_float(c_raw)
        if debit is None and credit is None: continue
        date_parsed = pd.to_datetime(date_val, dayfirst=True, errors='coerce')
        doc_num = _extract_doc_num(doc_val)
        rows.append({'date': date_parsed, 'date_str': date_val, 'document': doc_val,
                     'doc_num': doc_num, 'debit': debit, 'credit': credit,
                     'match_date': _extract_doc_date(doc_val) or date_parsed,
                     'signed_amount': float(debit or 0) - float(credit or 0),
                     'raw_row': idx})
    return _attach_meta(pd.DataFrame(rows), **meta)


def parse_two_sided_act(path: str, side: str = 'left') -> pd.DataFrame:
    """Парсит двусторонний акт сверки (формат 220): обе стороны в одном листе.
    Читает только сторону организации (левая половина):
    col 1 = дата, col 2 = документ, col 4 = дебет, col 6 = кредит (позиции авто-определяются)."""
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    date_re = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}$')
    meta = _extract_balance_meta(raw)
    # Автодетект колонок дебет/кредит: сканируем левую половину листа по нескольким строкам.
    if side == 'right':
        date_col, doc_col, debit_col, credit_col = 9, 10, 12, 14
    else:
        date_col, doc_col, debit_col, credit_col = 1, 2, 4, 6
    from collections import Counter
    col_hits: Counter = Counter()
    mid = max(len(raw.columns) // 2, 8)
    scan_rows = []
    for i in range(5, min(50, len(raw))):
        scan_idx = 9 if side == 'right' else 1
        if date_re.match(str(raw.iloc[i, scan_idx]).strip()):
            scan_rows.append(i)
            if len(scan_rows) >= 30: break
    for i in scan_rows:
        if side == 'right':
            scan_range = range(mid, len(raw.columns))
        else:
            scan_range = range(3, mid)
        for c in scan_range:
            v = str(raw.iloc[i, c]).strip()
            if v in ('', 'nan', '-', '—'): continue
            try: float(v.replace(',', '.').replace(' ', '').replace('\xa0', '')); col_hits[c] += 1
            except: pass
    if col_hits:
        sorted_cands = sorted(col_hits.keys())
        if len(sorted_cands) >= 2:
            debit_col, credit_col = sorted_cands[0], sorted_cands[1]
        elif len(sorted_cands) == 1:
            credit_col = sorted_cands[0]
    rows = []
    for idx in range(5, len(raw)):
        row = raw.iloc[idx]
        date_val = str(row[date_col]).strip() if pd.notna(row[date_col]) else ''
        doc_val  = str(row[doc_col]).strip()  if pd.notna(row[doc_col])  else ''
        if not date_val or date_val == 'nan' or not date_re.match(date_val): continue
        if not doc_val  or doc_val  == 'nan': continue
        if any(kw in doc_val.lower() for kw in ['обороты', 'сальдо конечное']): break
        # Деб/кред: у двустороннего акта числа могут быть отрицательными (сторно)
        d_raw = str(row[debit_col]).strip()  if debit_col  < len(row) and pd.notna(row[debit_col])  else ''
        c_raw = str(row[credit_col]).strip() if credit_col < len(row) and pd.notna(row[credit_col]) else ''
        debit  = _to_float(d_raw)
        credit = _to_float(c_raw)
        if debit is None and credit is None: continue
        # Конвертация даты (поддержка 2-значного года)
        date_parsed = pd.to_datetime(date_val, dayfirst=True, errors='coerce')
        if pd.isna(date_parsed):
            try:
                parts = date_val.split('.')
                if len(parts) == 3 and len(parts[2]) == 2:
                    parts[2] = '20' + parts[2]
                    date_parsed = pd.to_datetime('.'.join(parts), dayfirst=True, errors='coerce')
            except Exception:
                pass
        date_str = date_parsed.strftime('%d.%m.%Y') if pd.notna(date_parsed) else date_val
        doc_num = _extract_doc_num(doc_val)
        rows.append({'date': date_parsed, 'date_str': date_str, 'document': doc_val,
                     'doc_num': doc_num, 'debit': debit, 'credit': credit,
                     'match_date': _extract_doc_date(doc_val) or date_parsed,
                     'signed_amount': (
                         float(debit or 0) - float(credit or 0)
                         if side == 'right'
                         else float(credit or 0) - float(debit or 0)
                     ),
                     'raw_row': idx})
    return _attach_meta(pd.DataFrame(rows), **meta)


class PDFTextLayerMissing(Exception):
    pass


_PDF_DATE_RE = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}$')


def _pdf_cell(value) -> str:
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\n', ' ')).strip()


def _pdf_extract_text(path: str, max_pages: int = 2) -> str:
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:max_pages]:
            parts.append(page.extract_text() or '')
    return '\n'.join(p for p in parts if p)


def _pdf_table_side_specs(header: list[str]) -> dict:
    cells = [_pdf_cell(c).lower() for c in header]
    width = len(cells)
    specs = {}

    def spec_for(start: int):
        if start + 3 >= width:
            return None
        first = cells[start]
        second = cells[start + 1] if start + 1 < width else ''
        third = cells[start + 2] if start + 2 < width else ''
        fourth = cells[start + 3] if start + 3 < width else ''
        if 'дебет' not in third or 'кредит' not in fourth:
            return None
        date_col = start if 'дата' in first else None
        doc_col = start + 1 if date_col is not None else start + 1
        if date_col is None and not any(k in second for k in ('операц', 'документ', 'наименование')):
            return None
        return {'date': date_col, 'doc': doc_col, 'debit': start + 2, 'credit': start + 3}

    if width >= 8:
        left = spec_for(0)
        right = spec_for(4)
        if left:
            specs['left'] = left
        if right:
            specs['right'] = right
    if not specs:
        for start in range(max(1, width - 3)):
            spec = spec_for(start)
            if spec:
                specs['left'] = spec
                break
    return specs


def _pdf_parse_date(value: str) -> tuple[str, Optional[pd.Timestamp]]:
    text = _pdf_cell(value)
    if not _PDF_DATE_RE.match(text):
        return '', pd.NaT
    parsed = pd.to_datetime(text, dayfirst=True, errors='coerce')
    return (parsed.strftime('%d.%m.%Y') if pd.notna(parsed) else text, parsed)


def _pdf_balance_amount(debit, credit) -> Optional[float]:
    debit_v = _to_float(debit)
    credit_v = _to_float(credit)
    if debit_v is None and credit_v is None:
        return None
    if debit_v is None:
        return abs(float(credit_v))
    if credit_v is None:
        return abs(float(debit_v))
    return abs(float(debit_v)) if abs(float(debit_v)) >= abs(float(credit_v)) else abs(float(credit_v))


def _parse_pdf_tables_to_structured(tables: list, side: str = 'left') -> pd.DataFrame:
    rows = []
    meta = {}
    raw_idx = 0
    side = side if side in {'left', 'right'} else 'left'

    for table in tables or []:
        clean_table = [[_pdf_cell(c) for c in (row or [])] for row in table if row]
        if not clean_table:
            continue
        header_idx = None
        specs = {}
        for idx, row in enumerate(clean_table[:12]):
            row_text = ' '.join(row).lower()
            if 'дебет' in row_text and 'кредит' in row_text:
                found = _pdf_table_side_specs(row)
                if found:
                    header_idx = idx
                    specs = found
                    break
        if header_idx is None or side not in specs:
            continue

        spec = specs[side]
        start_balance = None
        end_balance = None
        start_row_text = ''
        end_row_text = ''

        for row_idx, row in enumerate(clean_table[header_idx + 1:], header_idx + 1):
            max_col = max(spec['doc'], spec['debit'], spec['credit'], spec['date'] or 0)
            if len(row) <= max_col:
                row = row + [''] * (max_col + 1 - len(row))
            document = _pdf_cell(row[spec['doc']])
            date_cell = _pdf_cell(row[spec['date']]) if spec['date'] is not None else ''
            debit = _to_float(row[spec['debit']])
            credit = _to_float(row[spec['credit']])
            row_text = ' '.join(row).lower()
            doc_lower = document.lower()
            operation_lower = f"{date_cell} {document}".lower()

            if not document and debit is None and credit is None:
                continue
            if any(kw in row_text for kw in ('генеральный директор', 'нижеподписавшиеся', 'м.п.')):
                continue

            if 'сальдо' in operation_lower:
                amount = _pdf_balance_amount(debit, credit)
                if amount is not None:
                    if start_balance is None:
                        start_balance = amount
                        start_row_text = document or date_cell
                    else:
                        end_balance = amount
                        end_row_text = document or date_cell
                continue
            if 'оборот' in operation_lower:
                continue
            if debit is None and credit is None:
                continue

            date_str, date_parsed = _pdf_parse_date(date_cell)
            if not date_str:
                date_parsed = _extract_doc_date(document) or _extract_any_date(document)
                date_str = date_parsed.strftime('%d.%m.%Y') if date_parsed is not None and pd.notna(date_parsed) else ''
            if not date_str:
                continue

            rows.append({
                'date': date_parsed,
                'date_str': date_str,
                'document': document,
                'doc_num': _extract_doc_num(document),
                'debit': debit,
                'credit': credit,
                'match_date': _extract_doc_date(document) or date_parsed,
                'signed_amount': float(debit or 0) - float(credit or 0),
                'raw_row': raw_idx,
                'pdf_side': side,
            })
            raw_idx += 1

        if start_balance is not None:
            meta['start_balance'] = start_balance
        if end_balance is not None:
            meta['end_balance'] = end_balance
        header_text = ' '.join(' '.join(r) for r in clean_table[:header_idx + 1])
        period_from, period_to = _extract_period_bounds(header_text)
        if period_from is None:
            period_from = _extract_any_date(start_row_text)
        if period_to is None:
            period_to = _extract_any_date(end_row_text)
        if period_from is not None and pd.notna(period_from):
            meta['period_from'] = period_from
        if period_to is not None and pd.notna(period_to):
            meta['period_to'] = period_to

    return _attach_meta(pd.DataFrame(rows), **meta)


def _extract_pdf_tables(path: str) -> list:
    tables = []
    text_chars = 0
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text_chars += len(page.extract_text() or '')
            tables.extend(page.extract_tables() or [])
    if text_chars == 0:
        raise PDFTextLayerMissing("PDF не содержит извлекаемого текстового слоя")
    return tables


def _parse_pdf_generic(path: str) -> pd.DataFrame:
    try:
        tables = _extract_pdf_tables(path)
    except PDFTextLayerMissing:
        return pd.DataFrame()
    for side in ('left', 'right'):
        df = _parse_pdf_tables_to_structured(tables, side=side)
        if not df.empty:
            return df
    rows = []
    text = _pdf_extract_text(path, max_pages=3)
    for line in text.split('\n'):
        parts = line.strip().split()
        if len(parts) >= 2 and _PDF_DATE_RE.match(parts[0]):
            rows.append(parts)
    if not rows:
        return pd.DataFrame()
    max_cols = max(len(r) for r in rows)
    padded = [r + [''] * (max_cols - len(r)) for r in rows]
    return pd.DataFrame(padded, columns=[f"Col{i}" for i in range(max_cols)])


def _extract_json_payload(text: str):
    text = (text or '').strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    try:
        return json.loads(text)
    except Exception:
        pass
    starts = [i for i in (text.find('{'), text.find('[')) if i >= 0]
    if not starts:
        raise ValueError("JSON не найден в ответе AI")
    start = min(starts)
    end = max(text.rfind('}'), text.rfind(']'))
    if end <= start:
        raise ValueError("JSON не найден в ответе AI")
    return json.loads(text[start:end + 1])


def _render_pdf_pages_for_ai(path: str, max_pages: int = 2) -> list:
    try:
        import pypdfium2 as pdfium
    except Exception as exc:
        raise RuntimeError("Для OCR/AI-разбора PDF нужен пакет pypdfium2") from exc
    doc = pdfium.PdfDocument(path)
    blocks = []
    for idx in range(min(len(doc), max_pages)):
        page = doc[idx]
        image = page.render(scale=2).to_pil().convert('RGB')
        if image.width > 1600:
            ratio = 1600 / image.width
            image = image.resize((1600, int(image.height * ratio)))
        buf = io.BytesIO()
        image.save(buf, format='PNG', optimize=True)
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(buf.getvalue()).decode('ascii'),
            },
        })
    return blocks


def parse_pdf_act_with_vision(path: str, api_key: str) -> pd.DataFrame:
    if not api_key:
        raise Exception("PDF не содержит текстового слоя; для такого файла нужен OCR/AI-разбор")
    image_blocks = _render_pdf_pages_for_ai(path, max_pages=2)
    if not image_blocks:
        raise Exception("PDF не удалось отрендерить для OCR/AI-разбора")
    client = Anthropic(api_key=api_key)
    prompt = """Извлеки операции из изображения акта сверки.
Читай заполненную сторону таблицы с операциями. Если вторая половина таблицы пустая, игнорируй ее.
Не включай строки сальдо, оборотов, подписей и печатей в rows, но верни start_balance/end_balance, если они видны.
Даты нормализуй в ДД.ММ.ГГГГ. Суммы верни числами с точкой, пустые значения — null.

Верни только JSON:
{
  "period_from": "ДД.ММ.ГГГГ или null",
  "period_to": "ДД.ММ.ГГГГ или null",
  "start_balance": 123.45,
  "end_balance": 123.45,
  "rows": [
    {"date_str": "ДД.ММ.ГГГГ", "document": "текст операции", "doc_num": "номер или null", "debit": 123.45, "credit": null}
  ]
}"""
    msg = client.messages.create(
        model=MODEL_MAIN,
        max_tokens=4096,
        temperature=0,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, *image_blocks]}],
    )
    response_text = '\n'.join(getattr(block, 'text', '') for block in msg.content if getattr(block, 'text', ''))
    payload = _extract_json_payload(response_text)
    if isinstance(payload, list):
        rows_payload = payload
        meta_payload = {}
    else:
        rows_payload = payload.get('rows', []) if isinstance(payload, dict) else []
        meta_payload = payload if isinstance(payload, dict) else {}
    rows = []
    for idx, item in enumerate(rows_payload):
        if not isinstance(item, dict):
            continue
        document = str(item.get('document') or '').strip()
        if not document:
            continue
        debit = _to_float(item.get('debit'))
        credit = _to_float(item.get('credit'))
        if debit is None and credit is None:
            continue
        date_str = str(item.get('date_str') or item.get('date') or '').strip()
        date_parsed = pd.to_datetime(date_str, dayfirst=True, errors='coerce') if date_str else pd.NaT
        if pd.isna(date_parsed):
            date_parsed = _extract_doc_date(document) or _extract_any_date(document)
        date_str = date_parsed.strftime('%d.%m.%Y') if date_parsed is not None and pd.notna(date_parsed) else date_str
        rows.append({
            'date': date_parsed,
            'date_str': date_str,
            'document': document,
            'doc_num': _normalize_doc_num(str(item.get('doc_num')).strip()) if item.get('doc_num') else _extract_doc_num(document),
            'debit': debit,
            'credit': credit,
            'match_date': _extract_doc_date(document) or date_parsed,
            'signed_amount': float(debit or 0) - float(credit or 0),
            'raw_row': idx,
            'pdf_side': 'vision',
        })
    meta = {}
    for key in ('start_balance', 'end_balance'):
        value = _to_float(meta_payload.get(key)) if isinstance(meta_payload, dict) else None
        if value is not None:
            meta[key] = abs(float(value))
    for key in ('period_from', 'period_to'):
        value = meta_payload.get(key) if isinstance(meta_payload, dict) else None
        parsed = pd.to_datetime(value, dayfirst=True, errors='coerce') if value else pd.NaT
        if pd.notna(parsed):
            meta[key] = parsed
    return _attach_meta(pd.DataFrame(rows), **meta)


def parse_pdf_act_to_structured(path: str, side: str = 'left') -> pd.DataFrame:
    tables = _extract_pdf_tables(path)
    return _parse_pdf_tables_to_structured(tables, side=side)


def parse_generic(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    if ext == '.pdf':
        return _parse_pdf_generic(path)
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    HEADER_KEYWORDS = {'дата', 'документ', 'дебет', 'кредит', 'сумма', 'номер', 'наименование', 'операция'}
    header_row = None
    for i in range(min(30, len(raw))):
        cells = [str(v).strip() for v in raw.iloc[i] if pd.notna(v) and str(v).strip()]
        if len(cells) < 2: continue
        if any(kw in ' '.join(cells).lower() for kw in HEADER_KEYWORDS):
            header_row = i; break
    df = pd.read_excel(path, engine=engine, header=header_row, dtype=str)
    FOOTER = ('обороты за период', 'сальдо конечное', 'генеральный директор', 'м.п.')
    keep = []
    for i, row in df.iterrows():
        first = ''
        for v in row:
            if pd.notna(v) and str(v).strip():
                first = str(v).strip().lower(); break
        if any(first.startswith(kw) for kw in FOOTER): break
        keep.append(i)
    df = df.loc[keep].dropna(how='all').reset_index(drop=True)
    df = df.loc[:, df.notna().any()]
    df.columns = [str(c).strip() for c in df.columns]
    return df


def parse_with_profile(path: str, profile: dict) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    data_start   = int(profile.get('data_start_row', 1))
    date_col     = profile.get('date_col')
    doc_col      = profile.get('doc_col')
    doc_num_col  = profile.get('doc_num_col')
    doc_type_col = profile.get('doc_type_col')
    debit_col    = profile.get('debit_col')
    credit_col   = profile.get('credit_col')
    amount_col   = profile.get('amount_col')
    amount_sign  = profile.get('amount_sign', 'unknown')
    footer_kws   = [k.lower() for k in profile.get('footer_keywords', [])]
    rows = []
    for idx in range(data_start, len(raw)):
        row = raw.iloc[idx]
        date_val = str(row[date_col]).strip() if date_col is not None and pd.notna(row[date_col]) else ''
        doc_val  = str(row[doc_col]).strip()  if doc_col  is not None and pd.notna(row[doc_col])  else ''
        if not date_val or date_val == 'nan': continue
        if not doc_val  or doc_val  == 'nan': continue
        row_text = (date_val + ' ' + doc_val).lower()
        if any(row_text.startswith(kw) or kw in row_text[:40] for kw in footer_kws): break
        doc_num = (_normalize_doc_num(str(row[doc_num_col]).strip())
                   if doc_num_col is not None and pd.notna(row[doc_num_col])
                   else _extract_doc_num(doc_val))
        doc_type = str(row[doc_type_col]).strip() if doc_type_col is not None and pd.notna(row[doc_type_col]) else ''
        date_p = pd.to_datetime(date_val, dayfirst=True, errors='coerce')
        if amount_col is not None:
            raw_amount = _to_float(str(row[amount_col]) if pd.notna(row[amount_col]) else '')
            if raw_amount is None: debit = credit = None
            elif amount_sign == 'positive_is_debit':
                debit = abs(raw_amount) if raw_amount > 0 else None
                credit = abs(raw_amount) if raw_amount < 0 else None
            elif amount_sign in ('positive_is_credit', 'unknown'):
                credit = abs(raw_amount) if raw_amount > 0 else None
                debit  = abs(raw_amount) if raw_amount < 0 else None
            elif amount_sign == 'signed':
                debit  = raw_amount if raw_amount < 0 else None
                credit = raw_amount if raw_amount > 0 else None
            else:
                credit = abs(raw_amount) if raw_amount else None; debit = None
        else:
            debit  = _to_float(str(row[debit_col])  if debit_col  is not None and pd.notna(row[debit_col])  else '')
            credit = _to_float(str(row[credit_col]) if credit_col is not None and pd.notna(row[credit_col]) else '')
        rows.append({'date': date_p, 'date_str': date_val, 'document': doc_val,
                     'doc_num': doc_num, 'doc_type': doc_type,
                     'debit': debit, 'credit': credit, 'raw_row': idx})
    return pd.DataFrame(rows)


# ════════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ТИПА ФАЙЛА
# ════════════════════════════════════════════════════════════════════

def detect_file_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == '.pdf':
        try:
            text = _pdf_extract_text(path, max_pages=2)
            text_lower = text.lower()
            if 'акт сверки' in text_lower or 'взаимных расчетов' in text_lower or 'взаимных счетов' in text_lower:
                return 'pdf_act'
            if not text.strip():
                return 'pdf_no_text'
        except Exception:
            return 'pdf_no_text'
        return 'generic'
    try:
        raw = pd.read_excel(path, header=None, dtype=str, nrows=15)
        text = ' '.join(str(v) for v in raw.values.flatten() if pd.notna(v))
        text_lower = text.lower()
        if 'Номер документа' in text and 'Эмекс' in text: return 'emex'
        if 'Дата операции' in text and 'Тип документа' in text: return 'emex'
        if ('№ п/п' in text and 'наименование операции, документы' in text_lower
                and 'по состоянию на' in text_lower and 'акт сверки' in text_lower):
            return 'balance_state_act'
        if ('Наименование договора' in text and 'Номер С/Ф' in text and 'Дата С/Ф' in text
                and 'По данным' in text and 'Сальдо начальное' in text):
            return 'partner_ledger_act'
        is_act = ('акт сверки' in text_lower or 'взаимных расчетов' in text_lower or 'По данным ООО' in text)
        if is_act:
            raw_full = None
            has_two_sided = False
            has_date_col0 = False
            try:
                raw_full = pd.read_excel(path, header=None, dtype=str, nrows=20)
                date_re = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}$')
                has_two_sided = any(
                    pd.isna(raw_full.iloc[i, 0]) and date_re.match(str(raw_full.iloc[i, 1]).strip())
                    for i in range(8, min(15, len(raw_full)))
                )
                has_date_col0 = any(
                    date_re.match(str(raw_full.iloc[i, 0]).strip())
                    for i in range(8, min(15, len(raw_full)))
                )
            except Exception:
                pass
            if has_two_sided:
                return 'two_sided_act'
            proopt_pos = text.find('По данным ООО "ПРООПТ"')
            other_pos = -1
            for match in re.finditer(r'По данным [А-Яа-я]+ "(?!ПРООПТ)', text):
                other_pos = match.start(); break
            if proopt_pos != -1 and (other_pos == -1 or proopt_pos < other_pos): return 'proopt'
            if other_pos != -1 and (proopt_pos == -1 or other_pos < proopt_pos):
                # Определяем: двусторонний или обычный контрагентский формат
                if has_date_col0: return 'standard_act'
                return 'counterparty'
            if 'ПРООПТ' in text: return 'proopt'
            # Проверяем также без явного «контрагента»
            if has_date_col0: return 'standard_act'
            return 'counterparty'
    except Exception:
        pass
    return 'generic'


# ════════════════════════════════════════════════════════════════════
#  АВТОДЕТЕКТ ЧЕРЕЗ CLAUDE
# ════════════════════════════════════════════════════════════════════

def claude_detect_columns(path: str, api_key: str) -> Optional[dict]:
    """Определяет структуру колонок файла через Claude API.
    Кеширование профилей выполняется в _load_and_parse по оригинальному имени файла."""
    try:
        ext = Path(path).suffix.lower()
        engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
        raw = pd.read_excel(path, engine=engine, header=None, dtype=str, nrows=30)
    except Exception:
        return None
    preview = raw.to_string()
    prompt = f"""Ты анализируешь фрагмент Excel-файла акта сверки взаимных расчётов.
Нужно определить структуру таблицы для программной обработки.

ФРАГМЕНТ ФАЙЛА (первые 30 строк, колонки пронумерованы с 0):
{preview}

ВАЖНЫЕ ПРАВИЛА АНАЛИЗА:
1. Если лист двусторонний (обе стороны рядом — «По данным X» и «По данным Y»),
   читай ТОЛЬКО левую сторону организации (меньшие индексы колонок).
2. Дата может быть в формате ДД.ММ.ГГ или ДД.ММ.ГГГГ.
3. «Сумма документа» — вспомогательная колонка (присутствует в каждой строке).
   Дебет и Кредит — взаимоисключающие: в каждой строке заполнена только одна из них.
4. Суммы со знаком минус (сторно/возврат) — это нормально, не путай с кредитом.
5. Строки-заголовки, сальдо начальное, «обороты за период», «сальдо конечное» — НЕ данные.
6. Если есть отдельная колонка с типом операции (Оплата / Продажа / Приход) — укажи doc_type_col.

Верни ТОЛЬКО корректный JSON без markdown, без комментариев:
{{
  "header_row": <номер строки с заголовками (0-based), или null>,
  "data_start_row": <номер первой строки с данными транзакций (0-based)>,
  "date_col": <индекс колонки с датой операции (0-based)>,
  "doc_col": <индекс колонки с наименованием/описанием документа (0-based)>,
  "doc_num_col": <индекс колонки ТОЛЬКО с номером документа (0-based), или null>,
  "doc_type_col": <индекс колонки с типом операции (0-based), или null>,
  "debit_col": <индекс колонки Дебет — поступления/продажи (0-based), или null>,
  "credit_col": <индекс колонки Кредит — оплаты/погашения (0-based), или null>,
  "amount_col": <индекс единственной колонки суммы если нет раздельных дебет/кредит (0-based), или null>,
  "amount_sign": <"positive_is_credit" | "positive_is_debit" | "signed" | "unknown">,
  "footer_keywords": ["обороты за период", "сальдо конечное"],
  "confidence": <"high" | "medium" | "low">
}}"""
    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(model=MODEL_MAIN, max_tokens=500, temperature=0,
                                     messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"): part = part[4:].strip()
                try:
                    profile = json.loads(part); break
                except Exception: continue
            else:
                return None
        else:
            profile = json.loads(text)
        if profile.get('confidence') != 'low':
            pass  # кеширование выполняется в _load_and_parse
        return profile
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ СВЕРКИ
# ════════════════════════════════════════════════════════════════════

DEFAULT_RECON_SETTINGS = {
    'find_missing': True, 'find_amount_diff': True,
    'find_date_diff': True, 'find_sign_mismatch': True,
    'date_window_payment': 5, 'date_window_delivery': 3,
    'min_amount': 0.0, 'ai_comment': True,
}


# ════════════════════════════════════════════════════════════════════
#  АЛГОРИТМ СВЕРКИ
# ════════════════════════════════════════════════════════════════════

def _reconcile_structured(df1, df2, type1, type2, client, log, cfg=None):
    if cfg is None: cfg = DEFAULT_RECON_SETTINGS
    if 'raw_row' not in df1.columns: df1 = df1.copy(); df1['raw_row'] = range(len(df1))
    if 'raw_row' not in df2.columns: df2 = df2.copy(); df2['raw_row'] = range(len(df2))
    if df1.empty or df2.empty: return _reconcile_via_claude(df1, df2, client, log)

    dw_payment  = int(cfg.get('date_window_payment', 5))
    dw_delivery = int(cfg.get('date_window_delivery', 3))
    min_amount  = float(cfg.get('min_amount', 0.0))

    log("Шаг 1/3: Поиск точных совпадений...")
    matched1, matched2 = set(), set()
    fuzzy_matches = []

    idx2_by_docnum = {}
    for _, r in df2.iterrows():
        if r.get('doc_num'):
            idx2_by_docnum.setdefault(_normalize_doc_num(r['doc_num']), []).append(r)

    def _sf(v):
        try:
            if v is not None and pd.notna(v): return float(v)
        except: pass
        return None

    def _md(r):
        d = r.get('match_date')
        if d is not None and pd.notna(d):
            return d
        return r.get('date')

    def _meta(df, key):
        try:
            return df.attrs.get(key)
        except Exception:
            return None

    _sign_pat = re.compile(
        r'корректировк|ксф|возврат|сторно|исправлени|аннулирован|зачет|зачёт|'
        r'adjustment|correction|credit.?note|reversal|refund|write.?off|reverse', re.IGNORECASE)

    def _row_side(r):
        if _sf(r.get('debit')) is not None: return 'debit'
        if _sf(r.get('credit')) is not None: return 'credit'
        return None

    def _display_effect(r):
        return round((_sf(r.get('debit')) or 0.0) - (_sf(r.get('credit')) or 0.0), 2)

    def _side_label(r):
        debit = _sf(r.get('debit'))
        credit = _sf(r.get('credit'))
        if debit is not None:
            return f"Дебет: {debit:+,.2f} руб."
        if credit is not None:
            return f"Кредит: {credit:+,.2f} руб."
        return ''

    def _has_sign_hint(r):
        text = f"{r.get('doc_type', '')} {r.get('document', '')}"
        return bool(_sign_pat.search(text))

    def _has_negative_amount(r):
        return any((_sf(r.get(col)) or 0) < 0 for col in ('debit', 'credit'))

    def _is_sign_mismatch_pair(r1, r2):
        v1 = _sf(r1.get('debit')) or _sf(r1.get('credit'))
        v2 = _sf(r2.get('debit')) or _sf(r2.get('credit'))
        if v1 is None or v2 is None or abs(abs(v1) - abs(v2)) > 0.01:
            return False
        if not (_has_sign_hint(r1) or _has_sign_hint(r2) or _has_negative_amount(r1) or _has_negative_amount(r2)):
            return False
        return (v1 * v2 < 0) or (_row_side(r1) != _row_side(r2))

    _DOC_TOKEN_PATTERNS = (
        re.compile(r'(?:№|#|No)\s*([A-Za-zА-Яа-яЁё]*-?\d[\w/-]*)', re.IGNORECASE),
        re.compile(r'\b([A-Za-zА-Яа-яЁё]+-\d[\w/-]*)\b', re.IGNORECASE),
        re.compile(r'\b(\d{4,}[\w/-]*)\b', re.IGNORECASE),
    )

    def _doc_identity_tokens(r):
        tokens = {'norm': set(), 'raw': set(), 'prefix': set()}
        doc_num = str(r.get('doc_num') or '').strip()
        if doc_num:
            tokens['norm'].add(_normalize_doc_num(doc_num))
        text = f"{r.get('document', '')} {r.get('doc_type', '')}"
        for pattern in _DOC_TOKEN_PATTERNS:
            for match in pattern.finditer(text):
                raw = str(match.group(1)).strip().lower()
                if not raw or re.fullmatch(r'(?:19|20)\d{2}', raw):
                    continue
                tokens['raw'].add(raw)
                tokens['norm'].add(_normalize_doc_num(raw))
                prefix = re.match(r'([a-zа-яё]+)-', raw, re.IGNORECASE)
                if prefix:
                    tokens['prefix'].add(prefix.group(1).lower())
        return tokens

    def _doc_identity_score(r1, r2, sum1=None, sum2=None):
        score = 0.0
        t1 = _doc_identity_tokens(r1)
        t2 = _doc_identity_tokens(r2)
        if t1['raw'] & t2['raw']:
            score += 35
        if t1['norm'] & t2['norm']:
            score += 25
        if t1['prefix'] and t2['prefix'] and t1['prefix'] & t2['prefix']:
            score += 8
        if sum1 is not None and sum2 is not None:
            diff = abs(abs(sum1) - abs(sum2))
            if diff <= 0.01:
                score += 45
            else:
                score += max(0, 18 - min(diff / 1000, 18))
        d1, d2 = _md(r1), _md(r2)
        if pd.notna(d1) and pd.notna(d2):
            dd = abs((d1 - d2).days)
            if dd == 0:
                score += 18
            elif dd <= 7:
                score += max(0, 12 - dd)
        type1 = str(r1.get('doc_type') or '').strip().lower()
        type2 = str(r2.get('doc_type') or '').strip().lower()
        if type1 and type1 not in ('nan', 'none') and type1 == type2:
            score += 5
        return score

    def _has_strong_doc_identity(r1, r2):
        t1 = _doc_identity_tokens(r1)
        t2 = _doc_identity_tokens(r2)
        return bool((t1['raw'] & t2['raw']) or (t1['norm'] & t2['norm']))

    def _sign_pair_financial_effects(r1, r2, same_document: bool):
        """Возвращает эффекты для классификации зеркал.

        Если это один и тот же документ, разные технические способы записи
        debit/credit могут давать одинаковый эффект через debit-credit. Если
        сильного совпадения документа нет, похожая по сумме/дате зеркальная
        операция считается финансовой: одинаковый display-эффект разворачиваем
        у второй стороны, чтобы показать реальное влияние на расхождение.
        """
        effect1 = _display_effect(r1)
        effect2 = _display_effect(r2)
        if same_document or abs(effect1 - effect2) > 0.01:
            return effect1, effect2
        if _is_sign_mismatch_pair(r1, r2):
            return effect1, -effect2
        return effect1, effect2

    exact_pairs, amount_diff_pairs, seed_sign_mismatch_pairs = [], [], []
    for _, r1 in df1.iterrows():
        norm1 = _normalize_doc_num(r1['doc_num']) if r1.get('doc_num') else None
        if not norm1 or norm1 not in idx2_by_docnum: continue
        candidates = [c for c in idx2_by_docnum[norm1] if c['raw_row'] not in matched2]
        if not candidates: continue
        sum1 = _sf(r1.get('debit')) or _sf(r1.get('credit'))
        best = None
        best_score = None
        best_amount_diff = None
        amount_diff_scored = []
        for r2 in candidates:
            sum2 = _sf(r2.get('debit')) or _sf(r2.get('credit'))
            if sum1 is not None and sum2 is not None and abs(abs(sum1) - abs(sum2)) <= 0.01:
                score = _doc_identity_score(r1, r2, sum1, sum2)
                if best is None or score > best_score:
                    best = r2
                    best_score = score
            elif sum1 is not None and sum2 is not None:
                amount_diff_scored.append((_doc_identity_score(r1, r2, sum1, sum2), r2, sum2))
        if best is None and len(candidates) == 1:
            best = candidates[0]
            sum2 = _sf(best.get('debit')) or _sf(best.get('credit'))
            if sum1 is not None and sum2 is not None and abs(abs(sum1) - abs(sum2)) > 0.01:
                best_amount_diff = (r1, best, sum1, sum2)
        elif best is None and amount_diff_scored:
            top_score, top_candidate, top_sum = max(amount_diff_scored, key=lambda item: item[0])
            other_scores = [score for score, candidate, _ in amount_diff_scored if candidate is not top_candidate]
            next_score = max(other_scores) if other_scores else None
            if top_score >= 45 and (next_score is None or top_score - next_score >= 8):
                best = top_candidate
                best_amount_diff = (r1, best, sum1, top_sum)
        if best is not None:
            matched1.add(r1['raw_row']); matched2.add(best['raw_row'])
            if best_amount_diff is not None:
                amount_diff_pairs.append(best_amount_diff)
            if _is_sign_mismatch_pair(r1, best):
                seed_sign_mismatch_pairs.append((r1, best))
            else:
                exact_pairs.append((r1, best))

    log("Шаг 2/3: Нечёткое сопоставление...")

    PENALTY_KW = {'штраф','санкции','пени','неустойка','контрафакт','fine','penalty','interest charge','forfeit'}
    _CAT_PAYMENT    = {'оплата','платеж','платёж','п/п','пп ','выплата','строка выписки','банковская выписка','payment','pay ','transfer','wire','receipt','расходный кассов','приходный кассов'}
    _CAT_DELIVERY   = {'продажа','реализация','поставка','приход','поступление','отгрузка','накладная','упд','торг-12','торг12','счет-фактура','счёт-фактура','invoice','delivery','shipment','purchase','sale','supply','waybill'}
    _CAT_ADJUSTMENT = {'корректировка','ксф','возврат','сторно','кредит-нота','исправление','аннулирование','зачет','зачёт','adjustment','correction','credit note','reversal','refund','write-off','reverse'}

    def _cat(r):
        dt = str(r.get('doc_type', '')).lower().strip()
        if dt and dt not in ('nan','','none'):
            if any(kw in dt for kw in _CAT_PAYMENT): return 'оплата'
            if any(kw in dt for kw in _CAT_ADJUSTMENT): return 'корректировка'
            if any(kw in dt for kw in _CAT_DELIVERY): return 'поставка'
        d = str(r.get('document', '')).lower()
        if any(kw in d for kw in _CAT_PAYMENT): return 'оплата'
        if any(kw in d for kw in _CAT_ADJUSTMENT): return 'корректировка'
        if any(kw in d for kw in _CAT_DELIVERY): return 'поставка'
        return 'прочее'

    has_proopt_statement = _meta(df1, 'parser_id') == 'proopt' or _meta(df2, 'parser_id') == 'proopt'

    def _match_window(cat: str) -> int:
        if cat == 'оплата':
            return dw_payment
        if cat == 'корректировка' and has_proopt_statement:
            return max(dw_delivery, dw_payment)
        return dw_delivery

    def _date_report_window(cat: str) -> int:
        return dw_payment if cat == 'оплата' else dw_delivery

    unmatched1 = df1[~df1['raw_row'].isin(matched1)].copy()
    unmatched2 = df2[~df2['raw_row'].isin(matched2)].copy()
    unmatched2_amount_index = {}
    for _, r2 in unmatched2.iterrows():
        for side in ('debit', 'credit'):
            v2 = _sf(r2.get(side))
            if v2 is None:
                continue
            key = (side, round(abs(v2), 2))
            unmatched2_amount_index.setdefault(key, []).append(r2)

    for _, r1 in unmatched1.iterrows():
        d1 = _md(r1); cat1 = _cat(r1); found = False
        for s1, s2 in [('debit','credit'),('credit','debit'),('debit','debit'),('credit','credit')]:
            v1 = _sf(r1.get(s1))
            if v1 is None: continue
            for r2 in unmatched2_amount_index.get((s2, round(abs(v1), 2)), []):
                if r2['raw_row'] in matched2: continue
                if any(t in str(r2.get('doc_type','')).lower()+' '+str(r2.get('document','')).lower() for t in PENALTY_KW): continue
                cat2 = _cat(r2)
                if cat1 != 'прочее' and cat2 != 'прочее' and cat1 != cat2: continue
                v2 = _sf(r2.get(s2))
                if v2 is None: continue
                if cat1 == 'корректировка' and cat2 == 'корректировка' and s1 != s2 and v1 * v2 < 0: continue
                d2 = _md(r2)
                dw = _match_window(cat1)
                if pd.notna(d1) and pd.notna(d2):
                    if abs((d1 - d2).days) <= dw:
                        matched1.add(r1['raw_row']); matched2.add(r2['raw_row'])
                        fuzzy_matches.append((r1, r2)); found = True; break
                elif not pd.notna(d1) or not pd.notna(d2):
                    if cat1 == cat2 and cat1 != 'прочее':
                        matched1.add(r1['raw_row']); matched2.add(r2['raw_row'])
                        fuzzy_matches.append((r1, r2)); found = True; break
            if found: break

    missing_in_2 = df1[~df1['raw_row'].isin(matched1)].copy()
    missing_in_1 = df2[~df2['raw_row'].isin(matched2)].copy()

    def _find_smm(side_a, side_b, col_a, col_b):
        pairs, rem_a, used_b = [], set(), set()
        sa = side_a[side_a['document'].str.contains(_sign_pat, na=False) & side_a[col_a].notna()]
        sb = side_b[side_b['document'].str.contains(_sign_pat, na=False) & side_b[col_b].notna()]
        for _, ra in sa.iterrows():
            va = _sf(ra.get(col_a))
            if va is None: continue
            da = _md(ra)
            for _, rb in sb.iterrows():
                if rb['raw_row'] in used_b: continue
                vb = _sf(rb.get(col_b))
                if vb is None: continue
                if abs(abs(va) - abs(vb)) > 0.01: continue
                db = _md(rb)
                if ((pd.notna(da) and pd.notna(db) and abs((da - db).days) <= 5) or pd.isna(da) or pd.isna(db)):
                    pairs.append((ra, rb)); rem_a.add(ra['raw_row']); used_b.add(rb['raw_row']); break
        return pairs, rem_a, used_b

    p1, ra1, rb1 = _find_smm(missing_in_2, missing_in_1, 'credit', 'debit')
    m2r = missing_in_2[~missing_in_2['raw_row'].isin(ra1)]
    m1r = missing_in_1[~missing_in_1['raw_row'].isin(rb1)]
    p2, ra2, rb2 = _find_smm(m2r, m1r, 'debit', 'credit')
    smm_pairs = seed_sign_mismatch_pairs + p1 + p2
    missing_in_2 = missing_in_2[~missing_in_2['raw_row'].isin(ra1 | ra2)]
    missing_in_1 = missing_in_1[~missing_in_1['raw_row'].isin(rb1 | rb2)]

    def _build_window_suggestion(side_a, side_b):
        max_pay_scan = min(max(dw_payment + 7, 10), 14)
        max_del_scan = min(max(dw_delivery + 7, 10), 14)
        if max_pay_scan <= dw_payment and max_del_scan <= dw_delivery:
            return None

        candidates = []
        used_b = set()
        for _, ra in side_a.iterrows():
            da = _md(ra)
            cat_a = _cat(ra)
            if cat_a == 'прочее' or da is None or pd.isna(da):
                continue
            best = None
            best_score = None
            norm_a = _normalize_doc_num(ra.get('doc_num')) if ra.get('doc_num') else ''
            for col_a, col_b in [('debit', 'credit'), ('credit', 'debit'), ('debit', 'debit'), ('credit', 'credit')]:
                va = _sf(ra.get(col_a))
                if va is None:
                    continue
                for _, rb in side_b.iterrows():
                    if rb['raw_row'] in used_b:
                        continue
                    text_b = f"{rb.get('doc_type', '')} {rb.get('document', '')}".lower()
                    if any(term in text_b for term in PENALTY_KW):
                        continue
                    cat_b = _cat(rb)
                    if cat_a != cat_b or cat_b == 'прочее':
                        continue
                    vb = _sf(rb.get(col_b))
                    if vb is None or abs(abs(va) - abs(vb)) > 0.01:
                        continue
                    if cat_a == 'корректировка' and col_a != col_b and va * vb < 0:
                        continue
                    db = _md(rb)
                    if db is None or pd.isna(db):
                        continue
                    dd = abs((da - db).days)
                    base_dw = _match_window(cat_a)
                    scan_dw = max_pay_scan if cat_a == 'оплата' else max_del_scan
                    if dd <= base_dw or dd > scan_dw:
                        continue
                    norm_b = _normalize_doc_num(rb.get('doc_num')) if rb.get('doc_num') else ''
                    same_doc = 1 if norm_a and norm_b and norm_a == norm_b else 0
                    score = same_doc * 100 - dd
                    if best_score is None or score > best_score:
                        best_score = score
                        best = (rb, dd, cat_a)
            if best is None:
                continue
            rb, dd, cat_name = best
            used_b.add(rb['raw_row'])
            candidates.append({
                'category': cat_name,
                'days': int(dd),
                'row_a': int(ra.get('raw_row', 0)),
                'row_b': int(rb.get('raw_row', 0)),
            })

        if len(candidates) < 4:
            return None

        payment_pairs = [item for item in candidates if item['category'] == 'оплата']
        delivery_pairs = [item for item in candidates if item['category'] == 'поставка']
        adjustment_pairs = [item for item in candidates if item['category'] == 'корректировка']
        non_payment_pairs = delivery_pairs + adjustment_pairs

        rec_pay = max([dw_payment] + [item['days'] for item in payment_pairs])
        rec_del = max([dw_delivery] + [item['days'] for item in non_payment_pairs])
        if rec_pay <= dw_payment and rec_del <= dw_delivery:
            return None

        return {
            'candidate_pairs': len(candidates),
            'payment_pairs': len(payment_pairs),
            'delivery_pairs': len(delivery_pairs),
            'adjustment_pairs': len(adjustment_pairs),
            'current_payment_window': dw_payment,
            'current_delivery_window': dw_delivery,
            'recommended_payment_window': int(rec_pay),
            'recommended_delivery_window': int(rec_del),
            'max_shift_days': max(item['days'] for item in candidates),
        }

    log("Шаг 3/3: Формирование отчёта...")

    def _ga(r):
        for col in ('debit','credit'):
            v = r.get(col)
            try:
                if v is not None and pd.notna(v) and float(v) != 0: return float(v)
            except: pass
        return 0.0

    discrepancies = []
    technical_mirror_pairs = []
    technical_mirror_row_pairs = set()

    if cfg.get('find_missing', True):
        for _, r in missing_in_2.iterrows():
            amount = _ga(r)
            if min_amount and abs(amount) < min_amount: continue
            discrepancies.append({'type':'missing_in_counterparty','document_number':r.get('document',''),
                'description':'Есть у организации, отсутствует у контрагента',
                'company_value':f"{amount:,.2f} руб." if amount else '','supplier_value':'-',
                'difference':f"{abs(amount):,.2f}" if amount else '','severity':'high',
                'row_company':r.get('raw_row',0),'row_supplier':0,'date':r.get('date_str','')})
        for _, r in missing_in_1.iterrows():
            amount = _ga(r)
            if min_amount and abs(amount) < min_amount: continue
            discrepancies.append({'type':'missing_in_company','document_number':r.get('document',''),
                'description':'Есть у контрагента, отсутствует у организации',
                'company_value':'-','supplier_value':f"{amount:,.2f} руб." if amount else '',
                'difference':f"{abs(amount):,.2f}" if amount else '','severity':'high',
                'row_company':0,'row_supplier':r.get('raw_row',0),'date':r.get('date_str','')})

    if cfg.get('find_sign_mismatch', True):
        for ro, rc in smm_pairs:
            same_document = _has_strong_doc_identity(ro, rc)
            effect_company, effect_supplier = _sign_pair_financial_effects(ro, rc, same_document)
            effect_diff = round(abs(effect_company - effect_supplier), 2)
            amount = max(abs(effect_company), abs(effect_supplier))
            if min_amount and amount < min_amount: continue
            is_technical = same_document and effect_diff <= 0.01
            if is_technical:
                technical_mirror_pairs.append((ro, rc))
                technical_mirror_row_pairs.add((ro['raw_row'], rc['raw_row']))
                continue
            discrepancies.append({
                'type':'sign_mismatch',
                'document_number':ro.get('document',''),
                'description':(
                    'Похожая зеркальная операция без совпадающего номера документа влияет на сальдо'
                    if not same_document else
                    'Одна операция отражена с противоположным влиянием на сальдо'
                ),
                'company_value':f"{_side_label(ro)}; эффект {effect_company:+,.2f} руб.",
                'supplier_value':f"{_side_label(rc)}; эффект {effect_supplier:+,.2f} руб.",
                'difference':f"{effect_diff:,.2f}",
                'severity':'high',
                'row_company':ro.get('raw_row',0),'row_supplier':rc.get('raw_row',0),
                'date':ro.get('date_str','')
            })

    if cfg.get('find_amount_diff', True):
        for r1, r2, s1, s2 in amount_diff_pairs:
            diff = abs(abs(s1) - abs(s2))
            if min_amount and diff < min_amount: continue
            discrepancies.append({'type':'amount_diff','document_number':r1.get('document',''),
                'description':'Документ совпадает, но суммы расходятся',
                'company_value':f"{s1:,.2f} руб.",'supplier_value':f"{s2:,.2f} руб.",
                'difference':f"{diff:,.2f}",'severity':'high' if diff > 1000 else 'medium',
                'row_company':r1.get('raw_row',0),'row_supplier':r2.get('raw_row',0),'date':r1.get('date_str','')})

    if cfg.get('find_date_diff', True):
        fuzzy_rr_pairs = {(a['raw_row'], b['raw_row']) for a, b in fuzzy_matches}
        for r1, r2 in exact_pairs + fuzzy_matches:
            d1, d2 = r1.get('date'), r2.get('date')
            if pd.notna(d1) and pd.notna(d2) and abs((d1 - d2).days) > 0:
                dd = abs((d1 - d2).days)
                cat = _cat(r1)
                dw = _date_report_window(cat)
                if dd <= dw:
                    continue
                pfx = 'Нечёткое совпадение: ' if (r1['raw_row'], r2['raw_row']) in fuzzy_rr_pairs else ''
                discrepancies.append({'type':'date_diff','document_number':r1.get('document',''),
                    'description':f'{pfx}Даты расходятся на {dd} дн.',
                    'company_value':r1.get('date_str',''),'supplier_value':r2.get('date_str',''),
                    'difference':f'{dd} дн.','severity':'low',
                    'row_company':r1.get('raw_row',0),'row_supplier':r2.get('raw_row',0),'date':r1.get('date_str','')})

    def _sfz(v):
        try:
            if v is not None and not (isinstance(v, float) and pd.isna(v)): return float(v)
        except: pass
        return 0.0

    def _balance_effect(df):
        if 'signed_amount' in df.columns:
            try:
                return float(pd.to_numeric(df['signed_amount'], errors='coerce').fillna(0).sum())
            except Exception:
                pass
        total = 0.0
        for _, r in df.iterrows():
            total += _sfz(r.get('debit')) - _sfz(r.get('credit'))
        return total

    net_period_txn = round(_balance_effect(df1) - _balance_effect(df2), 2)
    opening_diff = None
    closing_diff = None
    start1 = _meta(df1, 'start_balance')
    start2 = _meta(df2, 'start_balance')
    end1 = _meta(df1, 'end_balance')
    end2 = _meta(df2, 'end_balance')
    if start1 is not None and start2 is not None:
        opening_diff = round(float(start1) - float(start2), 2)
    if end1 is not None and end2 is not None:
        closing_diff = round(float(end1) - float(end2), 2)

    transaction_net_diff = net_period_txn
    if opening_diff is not None and closing_diff is not None:
        transaction_net_diff = round(closing_diff - opening_diff, 2)
    elif closing_diff is not None and opening_diff is None:
        transaction_net_diff = closing_diff

    net_period = net_period_txn
    if closing_diff is not None:
        if abs(abs(net_period_txn) - abs(closing_diff)) <= 0.01 and abs(net_period_txn) >= 0.01:
            net_period = abs(closing_diff) if net_period_txn > 0 else -abs(closing_diff)
        else:
            net_period = closing_diff
    if abs(net_period) < 0.01:
        net_period = 0.0

    period1_from = _meta(df1, 'period_from')
    period1_to = _meta(df1, 'period_to')
    period2_from = _meta(df2, 'period_from')
    period2_to = _meta(df2, 'period_to')

    def _fmt_period(start, end):
        if start is not None and pd.notna(start) and end is not None and pd.notna(end):
            return f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}"
        if end is not None and pd.notna(end):
            return end.strftime('%d.%m.%Y')
        return ''

    period1_str = _fmt_period(period1_from, period1_to)
    period2_str = _fmt_period(period2_from, period2_to)
    if period1_str and period2_str:
        period_str = period1_str if period1_str == period2_str else f"Док.1: {period1_str}; Док.2: {period2_str}"
    else:
        all_dates = []
        for df in (df1, df2):
            if 'date' in df.columns:
                all_dates += [d for d in df['date'] if pd.notna(d)]
        period_str = (f"{min(all_dates).strftime('%d.%m.%Y')} - {max(all_dates).strftime('%d.%m.%Y')}"
                      if all_dates else '')

    if abs(net_period) < 0.01:
        debt_label = f'Взаиморасчёты совпадают (за период {period_str})' if period_str else 'Взаиморасчёты совпадают'
    elif net_period > 0:
        debt_label = f'Расхождение конечного сальдо в пользу организации: {net_period:,.2f} руб. (за период {period_str})'
    else:
        debt_label = f'Расхождение конечного сальдо в пользу контрагента: {abs(net_period):,.2f} руб. (за период {period_str})'

    window_suggestion = _build_window_suggestion(missing_in_2, missing_in_1)
    critical = sum(1 for d in discrepancies if d['severity'] == 'high')
    technical_mirror_count = len(technical_mirror_pairs)
    ai_comment = ''
    if client and discrepancies and cfg.get('ai_comment', True):
        try:
            sample = [d for d in discrepancies if d['type'] not in ('date_diff', 'technical_mirror')][:30]
            if sample:
                msg = client.messages.create(model=MODEL_MAIN, max_tokens=600, temperature=0,
                    system="Ты бухгалтер-аналитик. Дай краткое резюме расхождений в акте сверки. 3-5 предложений на русском языке. Только суть, без лишних слов.",
                    messages=[{"role": "user", "content":
                        f"{debt_label}\nРасхождений: {len(discrepancies)}, критических: {critical}.\n"
                        f"Примеры:\n{json.dumps(sample, ensure_ascii=False, indent=2)[:2000]}"}])
                ai_comment = msg.content[0].text.strip()
        except Exception as e:
            ai_comment = f"(Комментарий AI недоступен: {e})"

    return {
        'discrepancies': discrepancies,
        'summary': {'total_discrepancies': len(discrepancies), 'critical_count': critical,
                    'fuzzy_count': len(fuzzy_matches), 'exact_matches': len(exact_pairs),
                    'net_period': net_period, 'debt_label': debt_label,
                    'ai_comment': ai_comment, 'period': period_str,
                    'opening_balance_doc1': float(start1) if start1 is not None else None,
                    'opening_balance_doc2': float(start2) if start2 is not None else None,
                    'closing_balance_doc1': float(end1) if end1 is not None else None,
                    'closing_balance_doc2': float(end2) if end2 is not None else None,
                    'period_doc1': period1_str or None,
                    'period_doc2': period2_str or None,
                    'period_mismatch': bool(period1_str and period2_str and period1_str != period2_str),
                    'opening_balance_difference': opening_diff,
                    'closing_balance_difference': closing_diff,
                    'transaction_net_difference': transaction_net_diff,
                    'window_suggestion': window_suggestion,
                    'technical_mirror_count': technical_mirror_count},
        'matched1': list(matched1), 'matched2': list(matched2),
        'missing_rows1': list(missing_in_2['raw_row'].tolist()),
        'missing_rows2': list(missing_in_1['raw_row'].tolist()),
        'amount_diff_rows1': [r1['raw_row'] for r1,r2,s1,s2 in amount_diff_pairs],
        'amount_diff_rows2': [r2['raw_row'] for r1,r2,s1,s2 in amount_diff_pairs],
        'fuzzy_rows1': [r1['raw_row'] for r1,_ in fuzzy_matches],
        'fuzzy_rows2': [r2['raw_row'] for _,r2 in fuzzy_matches],
        'sign_mismatch_rows1': [ro['raw_row'] for ro, rc in smm_pairs if (ro['raw_row'], rc['raw_row']) not in technical_mirror_row_pairs],
        'sign_mismatch_rows2': [rc['raw_row'] for ro, rc in smm_pairs if (ro['raw_row'], rc['raw_row']) not in technical_mirror_row_pairs],
        'date_diff_rows1': list({r1['raw_row'] for r1,r2 in exact_pairs+fuzzy_matches
            if pd.notna(r1.get('date')) and pd.notna(r2.get('date')) and abs((r1['date']-r2['date']).days) > 0}),
        'date_diff_rows2': list({r2['raw_row'] for r1,r2 in exact_pairs+fuzzy_matches
            if pd.notna(r1.get('date')) and pd.notna(r2.get('date')) and abs((r1['date']-r2['date']).days) > 0}),
    }


def _reconcile_via_claude(df1, df2, client, log):
    if not client:
        return {'discrepancies':[],'summary':{'total_discrepancies':0,'critical_count':0,
                'total_amount_difference':0,'ai_comment':'API ключ не указан — сверка невозможна'},
                'matched1':[],'matched2':[],'missing_rows1':[],'missing_rows2':[],'fuzzy_rows1':[],'fuzzy_rows2':[]}
    CHUNK = 150; all_disc = []; total_diff = 0.0
    n1, n2 = len(df1), len(df2)
    chunks = max(1, max(n1,n2)//CHUNK + (1 if max(n1,n2)%CHUNK else 0))
    for ci in range(chunks):
        start = ci * CHUNK
        s1 = df1.iloc[start:start+CHUNK]; s2 = df2.iloc[start:start+CHUNK]
        if s1.empty and s2.empty: break
        if chunks > 1: log(f"AI-анализ блок {ci+1}/{chunks}...")
        prompt = f"""Сравни два фрагмента документов сверки. Найди расхождения.
Верни ТОЛЬКО JSON без markdown:
{{"discrepancies":[{{"type":"missing_in_counterparty|missing_in_company|amount_diff|date_diff","document_number":"","description":"","company_value":"","supplier_value":"","difference":"","severity":"high|medium|low","row_company":0,"row_supplier":0}}],"total_amount_difference":0}}
ДОКУМЕНТ 1 (строки {start}–{start+len(s1)-1}):\n{s1.to_string()}
ДОКУМЕНТ 2 (строки {start}–{start+len(s2)-1}):\n{s2.to_string()}"""
        try:
            msg = client.messages.create(model=MODEL_MAIN, max_tokens=4096, temperature=0,
                                         messages=[{"role":"user","content":prompt}])
            text = msg.content[0].text
            if "```json" in text: text = text.split("```json")[1].split("```")[0]
            elif "```" in text: text = text.split("```")[1].split("```")[0]
            cr = json.loads(text.strip())
            all_disc.extend(cr.get('discrepancies',[])); total_diff += float(cr.get('total_amount_difference',0) or 0)
        except Exception as e:
            log(f"Ошибка блока {ci+1}: {e}"); continue
    critical = sum(1 for d in all_disc if d.get('severity') == 'high')
    return {'discrepancies':all_disc,'summary':{'total_discrepancies':len(all_disc),'critical_count':critical,
            'total_amount_difference':total_diff,'fuzzy_count':0,'exact_matches':0,
            'debt_label':f'Расхождение: {total_diff:,.2f}' if total_diff else '','ai_comment':''},
            'matched1':[],'matched2':[],'missing_rows1':[],'missing_rows2':[],'fuzzy_rows1':[],'fuzzy_rows2':[]}


def _has_structured_rows(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False
    if not {'date', 'document'}.issubset(df.columns):
        return False
    return 'debit' in df.columns or 'credit' in df.columns


def _score_parsed_dataframe(df: pd.DataFrame) -> dict:
    rows = len(df)
    if rows == 0:
        return {
            'rows': 0,
            'date_ratio': 0.0,
            'doc_ratio': 0.0,
            'amount_ratio': 0.0,
            'doc_num_ratio': 0.0,
            'has_start_balance': False,
            'has_end_balance': False,
            'score': -999.0,
        }

    date_ratio = float(pd.notna(df['date']).mean()) if 'date' in df.columns else 0.0
    doc_ratio = float(df['document'].astype(str).str.strip().ne('').mean()) if 'document' in df.columns else 0.0
    doc_num_ratio = float(df['doc_num'].fillna('').astype(str).str.strip().ne('').mean()) if 'doc_num' in df.columns else 0.0

    amount_rows = 0
    for _, row in df.iterrows():
        if _to_float(row.get('debit')) is not None or _to_float(row.get('credit')) is not None:
            amount_rows += 1
    amount_ratio = amount_rows / rows if rows else 0.0

    has_start_balance = df.attrs.get('start_balance') is not None
    has_end_balance = df.attrs.get('end_balance') is not None

    score = 0.0
    score += min(rows, 1500) * 0.02
    score += date_ratio * 22
    score += doc_ratio * 18
    score += amount_ratio * 24
    score += doc_num_ratio * 6
    if has_start_balance:
        score += 8
    if has_end_balance:
        score += 12
    if rows < 5:
        score -= 20
    if amount_ratio < 0.5:
        score -= 25
    if doc_ratio < 0.8:
        score -= 12

    return {
        'rows': rows,
        'date_ratio': round(date_ratio, 4),
        'doc_ratio': round(doc_ratio, 4),
        'amount_ratio': round(amount_ratio, 4),
        'doc_num_ratio': round(doc_num_ratio, 4),
        'has_start_balance': has_start_balance,
        'has_end_balance': has_end_balance,
        'score': round(score, 2),
    }


def _profile_cache_file() -> Path:
    return _DATA_DIR / 'col_profiles.json'


def _load_profile_cache() -> dict:
    cache_file = _profile_cache_file()
    if cache_file.exists():
        try:
            with open(cache_file, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_profile_cache(cache: dict) -> None:
    try:
        with open(_profile_cache_file(), 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _make_parse_candidate(df: pd.DataFrame, parse_type: str, label: str, parser_id: str, bonus: float = 0.0) -> Optional[dict]:
    if not _has_structured_rows(df):
        return None
    quality = _score_parsed_dataframe(df)
    total_score = quality['score'] + bonus
    df.attrs['parser_id'] = parser_id
    df.attrs['parser_label'] = label
    df.attrs['parser_score'] = total_score
    doc_nums = set()
    amounts = set()
    if 'doc_num' in df.columns:
        for value in df['doc_num'].fillna('').astype(str):
            value = _normalize_doc_num(value.strip())
            if value:
                doc_nums.add(value)
    for col in ('debit', 'credit'):
        if col not in df.columns:
            continue
        for value in df[col]:
            parsed = _to_float(value)
            if parsed is not None:
                amounts.add(round(abs(float(parsed)), 2))
    return {
        'df': df,
        'type': parse_type,
        'label': label,
        'parser_id': parser_id,
        'quality': quality,
        'score': total_score,
        'doc_nums': doc_nums,
        'amounts': amounts,
    }


def _estimate_raw_transaction_rows(raw: Optional[pd.DataFrame]) -> int:
    if raw is None or raw.empty:
        return 0
    date_re = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$')
    skip_terms = (
        'сальдо', 'обороты', 'итого', 'дебет', 'кредит', 'дата операции',
        'документ', 'по данным', 'акт сверки', 'нижеподписав',
    )
    rows = 0
    for _, row in raw.iterrows():
        cells = [
            str(value).strip()
            for value in row.tolist()
            if pd.notna(value) and str(value).strip() and str(value).strip().lower() != 'nan'
        ]
        if not cells:
            continue
        row_text = ' '.join(cells).lower()
        if any(term in row_text for term in skip_terms):
            continue
        has_date = any(date_re.match(cell) for cell in cells)
        if not has_date:
            continue
        amount_count = sum(1 for cell in cells if _to_float(cell) is not None)
        if amount_count == 0:
            continue
        text_count = 0
        for cell in cells:
            if date_re.match(cell) or _to_float(cell) is not None:
                continue
            if re.search(r'[A-Za-zА-Яа-я]', cell):
                text_count += 1
        if text_count:
            rows += 1
    return rows


def _ai_profile_trigger_reason(candidates: list, raw: Optional[pd.DataFrame], is_sheet: bool, is_act_like: bool) -> Optional[str]:
    if not is_sheet:
        return None
    estimated_rows = _estimate_raw_transaction_rows(raw)
    if not candidates:
        return 'структурные парсеры не нашли операций' if estimated_rows else 'структурные парсеры не нашли кандидатов'
    if not is_act_like:
        return None

    best = max(candidates, key=lambda c: (c['score'], c['quality']['rows']))
    quality = best.get('quality', {})
    parsed_rows = int(quality.get('rows', 0) or 0)
    date_ratio = float(quality.get('date_ratio', 0.0) or 0.0)
    amount_ratio = float(quality.get('amount_ratio', 0.0) or 0.0)

    if estimated_rows >= 8 and parsed_rows < max(3, int(estimated_rows * 0.65)):
        return f"лучший парсер разобрал {parsed_rows} из примерно {estimated_rows} строк операций"
    if parsed_rows >= 5 and date_ratio < 0.6 and estimated_rows >= 5:
        return f"у лучшего парсера низкая доля дат ({date_ratio:.0%})"
    if parsed_rows >= 5 and amount_ratio < 0.6 and estimated_rows >= 5:
        return f"у лучшего парсера низкая доля сумм ({amount_ratio:.0%})"
    if parsed_rows < 3 and (quality.get('has_start_balance') or quality.get('has_end_balance')) and estimated_rows >= 3:
        return 'найдены сальдо, но почти нет операций'
    return None


def _collect_parse_candidates(path: str, logs: list, api_key: str = "", original_filename: str = ""):
    eff_key = _effective_key(api_key)
    display_df = parse_generic(path)
    cache_name = original_filename or Path(path).name
    ext = Path(path).suffix.lower()
    ftype = detect_file_type(path)

    header_text = ''
    raw_preview = None
    if ext == '.pdf':
        try:
            header_text = _pdf_extract_text(path, max_pages=2)
        except Exception:
            header_text = ''
    else:
        try:
            raw_preview = pd.read_excel(path, header=None, dtype=str, nrows=120)
            header_text = ' '.join(str(v) for v in raw_preview.iloc[:20].values.flatten() if pd.notna(v))
        except Exception:
            pass
    header_lower = header_text.lower()
    is_sheet = ext in ('.xls', '.xlsx')
    is_pdf = ext == '.pdf'
    is_act_like = any(marker in header_lower for marker in (
        'акт сверки', 'взаимных расчетов', 'взаиморасчетов', 'сальдо', 'по данным'
    )) or ftype in ('pdf_act', 'pdf_no_text')

    candidates = []
    seen_ids = set()

    def add_candidate(parser_id: str, label: str, parse_type: str, parser_fn, bonus: float = 0.0):
        if parser_id in seen_ids:
            return
        seen_ids.add(parser_id)
        try:
            df = parser_fn(path)
        except Exception:
            return
        candidate = _make_parse_candidate(df, parse_type, label, parser_id, bonus)
        if candidate:
            candidates.append(candidate)

    if ftype == 'proopt':
        add_candidate('proopt', 'ПРООПТ', 'proopt', parse_proopt, bonus=22)
    if ftype == 'emex':
        add_candidate('emex', 'ЭМЕКС', 'emex', parse_emex, bonus=22)
    if ftype == 'partner_ledger_act':
        add_candidate('partner_ledger_act', 'Акт сверки (реестр проводок)', 'generic_detected', parse_partner_ledger_act, bonus=20)
    if ftype == 'balance_state_act':
        add_candidate('balance_state_act', 'Акт сверки (сальдо по операциям)', 'generic_detected', parse_balance_state_act, bonus=20)
    if ftype == 'pdf_act':
        add_candidate('pdf_act', 'PDF акт сверки', 'generic_detected', parse_pdf_act_to_structured, bonus=20)
    if ftype == 'standard_act':
        add_candidate('standard_act', 'Акт сверки (односторонний)', 'generic_detected', parse_standard_act, bonus=20)
    if ftype == 'two_sided_act':
        add_candidate('two_sided_left', 'Акт сверки (двусторонний)', 'generic_detected', lambda p: parse_two_sided_act(p, side='left'), bonus=22)
        add_candidate('two_sided_right', 'Акт сверки (двусторонний, правая сторона)', 'generic_detected', lambda p: parse_two_sided_act(p, side='right'), bonus=18)
    if ftype == 'counterparty':
        add_candidate('counterparty', 'Акт сверки (контрагент)', 'generic_detected', parse_counterparty, bonus=18)
    if is_pdf and ftype in ('pdf_act', 'generic'):
        add_candidate('pdf_text_left', 'PDF акт сверки (левая сторона)', 'generic_detected',
                      lambda p: parse_pdf_act_to_structured(p, side='left'), bonus=22 if ftype == 'pdf_act' else 10)
        add_candidate('pdf_text_right', 'PDF акт сверки (правая сторона)', 'generic_detected',
                      lambda p: parse_pdf_act_to_structured(p, side='right'), bonus=18 if ftype == 'pdf_act' else 8)

    if is_sheet and is_act_like:
        add_candidate('proopt', 'ПРООПТ', 'proopt', parse_proopt, bonus=5 if ftype != 'proopt' else 0)
        add_candidate('two_sided_left', 'Акт сверки (двусторонний)', 'generic_detected', lambda p: parse_two_sided_act(p, side='left'), bonus=6 if ftype != 'two_sided_act' else 0)
        add_candidate('two_sided_right', 'Акт сверки (двусторонний, правая сторона)', 'generic_detected', lambda p: parse_two_sided_act(p, side='right'), bonus=4 if ftype != 'two_sided_act' else 0)
        add_candidate('standard_act', 'Акт сверки (односторонний)', 'generic_detected', parse_standard_act, bonus=5 if ftype != 'standard_act' else 0)
        add_candidate('counterparty', 'Акт сверки (контрагент)', 'generic_detected', parse_counterparty, bonus=5 if ftype != 'counterparty' else 0)
        add_candidate('balance_state_act', 'Акт сверки (сальдо по операциям)', 'generic_detected', parse_balance_state_act, bonus=5 if ftype != 'balance_state_act' else 0)
        add_candidate('partner_ledger_act', 'Акт сверки (реестр проводок)', 'generic_detected', parse_partner_ledger_act, bonus=5 if ftype != 'partner_ledger_act' else 0)

    if is_pdf and not candidates:
        if ftype == 'pdf_no_text':
            logs.append(f"{cache_name}: PDF не содержит текстового слоя.")
        if eff_key:
            logs.append(f"{cache_name}: пробую OCR/AI-разбор изображения.")
            add_candidate('pdf_vision', 'PDF акт сверки (AI-разбор изображения)', 'generic_detected',
                          lambda p: parse_pdf_act_with_vision(p, eff_key), bonus=8)
        elif ftype == 'pdf_no_text':
            logs.append(f"{cache_name}: для PDF без текстового слоя нужен API-ключ или исходный Excel/текстовый PDF.")

    cache = _load_profile_cache()
    cache_key = f"col_profile_{cache_name}"
    profile = cache.get(cache_key)
    profile_source = 'cache' if profile else ''
    ai_reason = _ai_profile_trigger_reason(candidates, raw_preview, is_sheet, is_act_like)
    if profile is None and ai_reason and eff_key and is_sheet:
        try:
            logs.append(f"Пробую AI-структуру для {cache_name}: {ai_reason}.")
            profile = claude_detect_columns(path, eff_key)
            profile_source = 'ai'
            if profile and profile.get('confidence') != 'low':
                cache[cache_key] = profile
                _save_profile_cache(cache)
        except Exception:
            profile = None
    elif profile is None and ai_reason and is_sheet:
        logs.append(f"AI-структура могла бы помочь для {cache_name}: {ai_reason}, но API ключ не указан.")
    if profile and profile.get('confidence') != 'low':
        try:
            df = parse_with_profile(path, profile)
            confidence = profile.get('confidence', 'medium')
            label = 'Кеш профиля' if profile_source == 'cache' else f'Автодетект AI ({confidence})'
            ai_bonus = 18 if profile_source == 'cache' else 10
            candidate = _make_parse_candidate(df, 'generic_detected', label, f'ai_profile_{confidence}', ai_bonus)
            if candidate:
                candidate['profile'] = profile
                candidates.append(candidate)
        except Exception:
            pass

    candidates.sort(key=lambda c: (c['score'], c['quality']['rows']), reverse=True)
    return display_df, candidates


def _score_reconcile_candidate(result: dict, cand1: dict, cand2: dict) -> float:
    discrepancies = result.get('discrepancies', [])
    counts = {}
    for disc in discrepancies:
        counts[disc.get('type', '')] = counts.get(disc.get('type', ''), 0) + 1

    summary = result.get('summary', {})
    exact = int(summary.get('exact_matches', 0) or 0)
    fuzzy = int(summary.get('fuzzy_count', 0) or 0)
    critical = int(summary.get('critical_count', 0) or 0)
    max_rows = max(len(cand1['df']), len(cand2['df']), 1)
    coverage_ratio = (exact + fuzzy) / max_rows

    score = 0.0
    score += cand1['score'] + cand2['score']
    score += coverage_ratio * 160
    score += exact * 0.12 + fuzzy * 0.05
    technical_mirror_count = counts.get('technical_mirror', 0)
    score -= (len(discrepancies) - technical_mirror_count) * 4.5
    score -= technical_mirror_count * 0.5
    score -= critical * 12
    score -= counts.get('amount_diff', 0) * 10
    score -= (counts.get('missing_in_counterparty', 0) + counts.get('missing_in_company', 0)) * 6
    score -= counts.get('date_diff', 0) * 1.2

    opening_diff = summary.get('opening_balance_difference')
    closing_diff = summary.get('closing_balance_difference')
    txn_diff = summary.get('transaction_net_difference')
    if opening_diff is not None:
        score += 10
    if closing_diff is not None:
        score += 18
    if opening_diff is not None and closing_diff is not None and txn_diff is not None:
        if abs(round(float(opening_diff) + float(txn_diff) - float(closing_diff), 2)) <= 0.01:
            score += 18

    return round(score, 2)


def _quick_pair_score(cand1: dict, cand2: dict) -> float:
    doc_overlap = len(cand1.get('doc_nums', set()) & cand2.get('doc_nums', set()))
    amount_overlap = len(cand1.get('amounts', set()) & cand2.get('amounts', set()))
    max_rows = max(len(cand1['df']), len(cand2['df']), 1)
    row_ratio = min(len(cand1['df']), len(cand2['df'])) / max_rows

    score = 0.0
    score += cand1['score'] + cand2['score']
    score += min(doc_overlap, 1000) * 0.45
    score += min(amount_overlap, 1000) * 0.06
    score += row_ratio * 20
    if cand1['quality']['has_end_balance'] and cand2['quality']['has_end_balance']:
        score += 14
    if cand1['quality']['has_start_balance'] and cand2['quality']['has_start_balance']:
        score += 8
    return round(score, 2)


def _detailed_pair_limit() -> int:
    try:
        return max(1, min(9, int(os.environ.get("SVERKAI_DETAILED_PAIR_LIMIT", "2"))))
    except Exception:
        return 2


def _detailed_pair_row_limit() -> int:
    try:
        return max(0, int(os.environ.get("SVERKAI_DETAILED_PAIR_ROW_LIMIT", "350")))
    except Exception:
        return 350


def _select_best_candidate_pair(candidates1: list, candidates2: list, cfg: dict, logs: list):
    if not candidates1 or not candidates2:
        raise HTTPException(status_code=422, detail="Не удалось построить структурированные кандидаты для сверки.")

    top1 = candidates1[:3]
    top2 = candidates2[:3]
    pair_queue = []
    for cand1 in top1:
        for cand2 in top2:
            pair_queue.append({
                'cand1': cand1,
                'cand2': cand2,
                'quick_score': _quick_pair_score(cand1, cand2),
            })
    pair_queue.sort(key=lambda item: item['quick_score'], reverse=True)
    limit = _detailed_pair_limit()
    top_quick = pair_queue[0]['quick_score'] if pair_queue else 0
    cutoff = top_quick - max(8.0, abs(top_quick) * 0.03)
    top_max_rows = max(len(pair_queue[0]['cand1']['df']), len(pair_queue[0]['cand2']['df'])) if pair_queue else 0
    row_limit = _detailed_pair_row_limit()
    if row_limit and top_max_rows > row_limit:
        detailed_pairs = pair_queue[:1]
    else:
        detailed_pairs = [p for p in pair_queue if p['quick_score'] >= cutoff][:limit]
    if not detailed_pairs and pair_queue:
        detailed_pairs = pair_queue[:1]
    best = None

    for pair in detailed_pairs:
        cand1 = pair['cand1']
        cand2 = pair['cand2']
        result = _reconcile_structured(
                cand1['df'], cand2['df'], cand1['type'], cand2['type'],
                None, lambda *_: None, cfg
        )
        pair_score = _score_reconcile_candidate(result, cand1, cand2)
        candidate_info = {
            'cand1': cand1,
            'cand2': cand2,
            'result': result,
            'score': pair_score,
        }
        if best is None or pair_score > best['score']:
            best = candidate_info

    logs.append(f"Быстро оценено комбинаций структур: {len(pair_queue)}")
    logs.append(f"Детально проверено комбинаций: {len(detailed_pairs)}")
    logs.append(
        f"Выбрана лучшая структура: файл 1 -> {best['cand1']['label']}, "
        f"файл 2 -> {best['cand2']['label']}"
    )
    return best


def hybrid_reconcile(df1, df2, type1, type2, client, progress_cb=None, settings=None):
    def log(msg):
        if progress_cb: progress_cb(msg)
    cfg = settings or DEFAULT_RECON_SETTINGS
    structured_types = {'proopt','emex','generic_detected'}
    def _has_str(df):
        if not {'date','document','raw_row'}.issubset(df.columns): return False
        return 'debit' in df.columns or 'credit' in df.columns
    use_structured = ((type1 in structured_types and type2 in structured_types)
                      or (_has_str(df1) and _has_str(df2)))
    return _reconcile_structured(df1, df2, type1, type2, client, log, cfg) if use_structured \
           else _reconcile_via_claude(df1, df2, client, log)


# ════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ЗАГРУЗКИ
# ════════════════════════════════════════════════════════════════════

def _load_and_parse(path: str, logs: list, api_key: str = "", original_filename: str = ""):
    """Универсальная загрузка файла.

    Порядок приоритетов:
    1. Форматы с уникальными маркерами (ПРООПТ, ЭМЕКС, PDF, стандартные акты)
       — определяются по содержимому, API-ключ не нужен.
    2. Неизвестный формат → claude_detect_columns → parse_with_profile.
       Профиль кешируется по оригинальному имени файла, чтобы повторный
       файл с таким же именем обрабатывался без API-вызова.

    Кеш строится по original_filename (имя, данное пользователем), а НЕ по
    временному пути на диске — иначе все файлы с именем file1.xlsx получали
    бы один и тот же профиль независимо от содержимого.
    """
    display_df, candidates = _collect_parse_candidates(path, logs, api_key, original_filename)
    cache_name = original_filename or Path(path).name
    if not candidates:
        eff_key = _effective_key(api_key)
        if not eff_key:
            raise HTTPException(
                status_code=422,
                detail=f"Формат файла '{cache_name}' не распознан автоматически. "
                       "Укажите API-ключ или настройте структуру вручную."
            )
        raise HTTPException(
            status_code=422,
            detail=f"Не удалось определить структуру файла '{cache_name}'. "
                   "Попробуйте другой файл или настройку вручную."
        )

    best = candidates[0]
    logs.append(
        f"Структура {cache_name} определена: {best['label']} "
        f"(качество: {best['score']:.1f}, строк: {len(best['df'])})"
    )
    return best['df'], display_df, best['type'], best['label']


def _df_to_records(df: pd.DataFrame):
    records = []
    for _, row in df.iterrows():
        rec = {}
        for col in df.columns:
            v = row[col]
            try:
                if pd.isna(v): rec[col] = ''; continue
            except: pass
            rec[col] = v.strftime('%d.%m.%Y') if hasattr(v, 'strftime') else str(v) if v is not None else ''
        records.append(rec)
    return records


# ════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ════════════════════════════════════════════════════════════════════

@app.post("/api/validate-key")
async def validate_key(payload: dict):
    """Проверяет ключ доступа SverkAI: формат -> белый список -> реальный вызов."""
    key = payload.get("api_key", "").strip()
    ok, reason = _key_access_status(key)
    if not ok:
        return JSONResponse({"valid": False, "error": _key_error_message(reason)})
    # Проверка через реальный вызов к Anthropic
    try:
        client = Anthropic(api_key=key)
        client.messages.create(
            model=MODEL_FAST, max_tokens=10,
            messages=[{"role": "user", "content": "ping"}]
        )
        return JSONResponse({"valid": True})
    except Exception as e:
        err = str(e)
        if "authentication" in err.lower() or "401" in err:
            return JSONResponse({"valid": False, "error": "Ключ недействителен"})
        return JSONResponse({"valid": False, "error": f"Ошибка проверки: {err[:120]}"})


# ── Управление белым списком (только для администратора) ─────────────

@app.post("/api/admin/add-key")
async def admin_add_key(payload: dict):
    """Добавляет ключ в белый список. Требует ADMIN_SECRET в теле."""
    if not ADMIN_SECRET or payload.get("admin_secret") != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Нет доступа")
    key  = payload.get("api_key", "").strip()
    label = payload.get("label", "").strip() or "—"
    role = payload.get("role", "user").strip().lower() if isinstance(payload.get("role", "user"), str) else "user"
    if role not in {"user", "guest"}:
        role = "user"
    if not key:
        raise HTTPException(status_code=400, detail="api_key не указан")
    h = _user_id(key)
    if role == "user" and h in _guest_key_hashes():
        raise HTTPException(status_code=400, detail="Гостевой ключ нельзя добавить как пользовательский")
    entries = _load_allowed_keys_from_file()
    all_entries = _load_allowed_keys()
    if any(e.get("hash") == h and e.get("role", "user") == role for e in all_entries):
        return JSONResponse({"ok": False, "message": "Ключ уже в списке", "hash": h})
    if any(e.get("hash") == h for e in entries):
        return JSONResponse({"ok": False, "message": "Ключ уже в списке", "hash": h})
    entries.append({
        "hash": h,
        "label": label,
        "role": role,
        "enabled": True,
        "added": datetime.now().strftime("%Y-%m-%d"),
    })
    _save_allowed_keys(entries)
    return JSONResponse({"ok": True, "hash": h, "label": label, "role": role, "total": len(entries)})


@app.post("/api/admin/remove-key")
async def admin_remove_key(payload: dict):
    """Удаляет ключ из белого списка по хешу или по api_key."""
    if not ADMIN_SECRET or payload.get("admin_secret") != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Нет доступа")
    h = payload.get("hash", "").strip()
    if not h and payload.get("api_key"):
        h = _user_id(payload["api_key"].strip())
    entries = _load_allowed_keys_from_file()
    before  = len(entries)
    entries = [e for e in entries if e.get("hash") != h]
    _save_allowed_keys(entries)
    removed = before - len(entries)
    return JSONResponse({"ok": True, "removed": removed, "total": len(entries)})


@app.get("/api/admin/list-keys")
async def admin_list_keys(request: Request):
    """Возвращает белый список (без реальных ключей — только хеши и метки)."""
    secret = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Нет доступа")
    return JSONResponse({"entries": _load_allowed_keys()})


@app.post("/api/reconcile")
async def reconcile(
    request: Request,
    file1: UploadFile = File(...),
    file2: UploadFile = File(...),
    settings: str = Form(default="{}")
):
    user_key = _authorized_user_key_or_raise(request)
    eff_key  = _effective_key(user_key)
    if not user_key:
        _guest_limit_or_raise(request)

    try:
        cfg = {**DEFAULT_RECON_SETTINGS, **json.loads(settings)}
    except Exception:
        cfg = DEFAULT_RECON_SETTINGS.copy()

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp()
        ext1 = Path(file1.filename or "").suffix.lower()
        ext2 = Path(file2.filename or "").suffix.lower()
        p1 = os.path.join(tmpdir, f"file1{ext1}")
        p2 = os.path.join(tmpdir, f"file2{ext2}")
        await _save_upload_to_path(file1, p1, user_key, "Файл 1")
        await _save_upload_to_path(file2, p2, user_key, "Файл 2")

        logs = []
        try:
            df1d, candidates1 = _collect_parse_candidates(p1, logs, user_key, file1.filename)
        except HTTPException: raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Ошибка чтения файла 1 ({file1.filename}): {e}")
        try:
            df2d, candidates2 = _collect_parse_candidates(p2, logs, user_key, file2.filename)
        except HTTPException: raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Ошибка чтения файла 2 ({file2.filename}): {e}")
        if not candidates1 or not candidates2:
            failed = []
            if not candidates1:
                failed.append(file1.filename or "файл 1")
            if not candidates2:
                failed.append(file2.filename or "файл 2")
            detail = f"Не удалось распознать структуру: {', '.join(failed)}."
            if any("PDF не содержит текстового слоя" in msg for msg in logs):
                detail += " Один из PDF не содержит текстового слоя: загрузите текстовый PDF/Excel или войдите с API-ключом для AI-разбора изображения."
            raise HTTPException(status_code=422, detail=detail)

        client = Anthropic(api_key=eff_key) if eff_key else None

        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        loop = asyncio.get_event_loop()

        def _pick_and_run():
            best_pair = _select_best_candidate_pair(candidates1, candidates2, cfg, logs)
            cand1 = best_pair['cand1']
            cand2 = best_pair['cand2']
            logs.append(f"Файл 1: {file1.filename} -> {cand1['label']} ({len(cand1['df'])} строк)")
            logs.append(f"Файл 2: {file2.filename} -> {cand2['label']} ({len(cand2['df'])} строк)")
            final_result = _reconcile_structured(
                cand1['df'], cand2['df'], cand1['type'], cand2['type'],
                client, lambda t: logs.append(t), cfg
            )
            return cand1, cand2, final_result

        df1_choice, df2_choice, result = await loop.run_in_executor(
            ThreadPoolExecutor(max_workers=1),
            _pick_and_run
        )
        df1p, ft1, lb1 = df1_choice['df'], df1_choice['type'], df1_choice['label']
        df2p, ft2, lb2 = df2_choice['df'], df2_choice['type'], df2_choice['label']

        DCOLS = ['date_str','document','doc_num','debit','credit']
        COL_RU = {'date_str':'Дата','document':'Документ','doc_num':'Номер','debit':'Дебет','credit':'Кредит'}

        def _prep(df):
            cols = [c for c in DCOLS if c in df.columns]
            d = df[cols].rename(columns=COL_RU)
            return {'columns': list(d.columns),
                    'rows': _df_to_records(d),
                    'raw_rows': list(df['raw_row']) if 'raw_row' in df.columns else list(range(len(df)))}

        # ── Сохранение истории только для авторизованных пользователей ──
        if user_key:
            history = _load_user_history(user_key)
            history.append({
                'date':       datetime.now().strftime('%d.%m.%Y %H:%M'),
                'file1':      file1.filename,
                'file2':      file2.filename,
                'total':      result['summary'].get('total_discrepancies', 0),
                'critical':   result['summary'].get('critical_count', 0),
                'debt_label': result['summary'].get('debt_label', ''),
                'result': {
                    'summary':       result['summary'],
                    'discrepancies': result['discrepancies'],
                    'highlight': {
                        'missing1':  result.get('missing_rows1',[]),
                        'missing2':  result.get('missing_rows2',[]),
                        'amt_diff1': result.get('amount_diff_rows1',[]),
                        'amt_diff2': result.get('amount_diff_rows2',[]),
                        'sign1':     result.get('sign_mismatch_rows1',[]),
                        'sign2':     result.get('sign_mismatch_rows2',[]),
                        'date1':     result.get('date_diff_rows1',[]),
                        'date2':     result.get('date_diff_rows2',[]),
                    },
                    'file1_name': file1.filename,
                    'file2_name': file2.filename,
                    'table1': _prep(df1p),
                    'table2': _prep(df2p),
                }
            })
            _save_user_history(history, user_key)
        guest_usage = None if user_key else _record_guest_reconcile(request)

        return JSONResponse({'ok': True, 'logs': logs, 'summary': result['summary'],
            'discrepancies': result['discrepancies'],
            'table1': _prep(df1p), 'table2': _prep(df2p),
            'highlight': {
                'missing1': result.get('missing_rows1',[]), 'missing2': result.get('missing_rows2',[]),
                'amt_diff1': result.get('amount_diff_rows1',[]), 'amt_diff2': result.get('amount_diff_rows2',[]),
                'sign1': result.get('sign_mismatch_rows1',[]), 'sign2': result.get('sign_mismatch_rows2',[]),
                'date1': result.get('date_diff_rows1',[]), 'date2': result.get('date_diff_rows2',[]),
            },
            'file1_name': file1.filename, 'file2_name': file2.filename,
            'history_saved': bool(user_key),
            'guest_usage': guest_usage,
        })
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/preview")
async def preview_file(request: Request, file: UploadFile = File(...)):
    """Парсит один файл и возвращает таблицу для предпросмотра + статус детекта."""
    user_key = _authorized_user_key_or_raise(request)
    eff_key  = _effective_key(user_key)

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp()
        ext = Path(file.filename or "").suffix.lower()
        path = os.path.join(tmpdir, f"preview{ext}")
        await _save_upload_to_path(file, path, user_key, "Файл")

        label = ""
        needs_manual = False
        display_candidate = None
        preview_logs = []
        _, candidates = _collect_parse_candidates(path, preview_logs, user_key, file.filename)
        if candidates:
            display_candidate = candidates[0]
            df = display_candidate['df']
            label = display_candidate['label']
            ftype = display_candidate['type']
            profile = display_candidate.get('profile')
        else:
            df = parse_generic(path)
            ftype = detect_file_type(path)
            label = "PDF без текстового слоя" if ftype == "pdf_no_text" else "Требуется настройка"
            profile = None
            needs_manual = True

        DCOLS = ["date_str", "document", "doc_num", "debit", "credit"]
        COL_RU = {"date_str": "Дата", "document": "Документ", "doc_num": "Номер", "debit": "Дебет", "credit": "Кредит"}
        if all(c in df.columns for c in ["date_str", "document"]):
            cols = [c for c in DCOLS if c in df.columns]
            display_df = df[cols].rename(columns=COL_RU)
        else:
            display_df = df.head(200)

        raw_preview = []
        try:
            raw_ext = Path(path).suffix.lower()
            engine = "openpyxl" if raw_ext == ".xlsx" else ("xlrd" if raw_ext == ".xls" else None)
            if engine:
                raw_full = pd.read_excel(path, engine=engine, header=None, dtype=str)
                raw_preview = {
                    "columns": [str(i) for i in range(len(raw_full.columns))],
                    "rows": [[str(v).strip() if pd.notna(v) else "" for v in row] for _, row in raw_full.iterrows()],
                    "n_rows": len(raw_full)
                }
        except Exception:
            pass

        return JSONResponse({
            "ok": True,
            "label": label,
            "file_type": ftype,
            "needs_manual": needs_manual,
            "profile": profile,
            "raw_rows": list(df["raw_row"]) if "raw_row" in df.columns else list(range(len(display_df))),
            "table": {
                "columns": list(display_df.columns),
                "rows": _df_to_records(display_df),
            },
            "raw_preview": raw_preview,
            "n_cols": len(pd.read_excel(path, engine=("openpyxl" if ext==".xlsx" else "xlrd") if ext in (".xlsx",".xls") else "openpyxl", header=None, dtype=str, nrows=1).columns) if ext in (".xlsx",".xls") else 0,
        })
    except HTTPException: raise
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "needs_manual": True, "table": None, "raw_preview": {}})
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/export")
async def export_report(payload: dict, request: Request):
    user_key = _authorized_user_key_or_raise(request)
    if not user_key:
        raise HTTPException(status_code=401, detail="Экспорт в Excel доступен только с выданным тестовым доступом")
    report_id = str(uuid.uuid4())[:8]
    fname = f"sverkAI_{datetime.now().strftime('%Y%m%d_%H%M')}_{report_id}.xlsx"
    fpath = _REPORT_DIR / fname
    discs     = payload.get('discrepancies', [])
    summary   = payload.get('summary', {})
    f1_name   = payload.get('file1_name', 'Файл 1')
    f2_name   = payload.get('file2_name', 'Файл 2')

    wb  = xlsxwriter.Workbook(str(fpath))
    h   = wb.add_format({'bold':True,'bg_color':'#1a2e1a','font_color':'white','border':1,'font_size':11})
    red = wb.add_format({'bg_color':'#ffcccc','border':1,'font_size':10})
    yel = wb.add_format({'bg_color':'#fff3cc','border':1,'font_size':10})
    blu = wb.add_format({'bg_color':'#cce5ff','border':1,'font_size':10})
    gry = wb.add_format({'bg_color':'#f0f0f0','border':1,'font_size':10})

    ws = wb.add_worksheet('Расхождения')
    headers = ['№','Тип','Дата','Документ',f'У организации ({f1_name})',f'У контрагента ({f2_name})','Разница','Уровень']
    widths  = [5,30,12,45,28,28,15,12]
    for col,(hdr,w) in enumerate(zip(headers,widths)):
        ws.write(0,col,hdr,h); ws.set_column(col,col,w)
    ws.set_row(0,35)
    TYPE_RU = {'missing_in_counterparty':'❌ Нет у контрагента','missing_in_company':'❌ Нет у организации',
               'amount_diff':'💰 Разница в суммах','date_diff':'📅 Разница в датах',
               'sign_mismatch':'🔀 Зеркальная корректировка'}
    SEV_RU = {'high':'Высокий','medium':'Средний','low':'Низкий'}
    for ri, d in enumerate(discs, 1):
        sev = d.get('severity','low')
        tp  = d.get('type','')
        fmt = red if sev=='high' and tp!='sign_mismatch' else blu if tp == 'sign_mismatch' else yel if sev=='medium' else gry
        ws.write(ri,0,ri,fmt); ws.write(ri,1,TYPE_RU.get(tp,tp),fmt)
        ws.write(ri,2,d.get('date',''),fmt); ws.write(ri,3,d.get('document_number',''),fmt)
        ws.write(ri,4,d.get('company_value',''),fmt); ws.write(ri,5,d.get('supplier_value',''),fmt)
        ws.write(ri,6,d.get('difference',''),fmt); ws.write(ri,7,SEV_RU.get(sev,sev),fmt)

    ws2 = wb.add_worksheet('Сводка')
    ws2.set_column(0,0,35); ws2.set_column(1,1,70)
    nf = wb.add_format({'border':1,'font_size':10,'text_wrap':True})
    for ri,(k,v) in enumerate([
        ('Дата сверки', datetime.now().strftime('%d.%m.%Y %H:%M')),
        ('Файл организации', f1_name), ('Файл контрагента', f2_name),
        ('Итог', summary.get('debt_label','')),
        ('Всего расхождений', summary.get('total_discrepancies',0)),
        ('Критических', summary.get('critical_count',0)),
        ('Нечётких совпадений', summary.get('fuzzy_count',0)),
        ('Точных совпадений', summary.get('exact_matches',0)),
        ('Период', summary.get('period','')),
        ('Комментарий AI', summary.get('ai_comment','')),
    ]):
        ws2.write(ri,0,k,h); ws2.write(ri,1,str(v),nf)
    wb.close()
    return FileResponse(path=str(fpath), filename=fname,
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.get("/api/history")
async def get_history(request: Request):
    """Возвращает историю только для авторизованных пользователей."""
    user_key = _authorized_user_key_or_raise(request)
    if not user_key:
        return JSONResponse({"detail": "История доступна только с выданным тестовым доступом"}, status_code=401)
    return JSONResponse(_load_user_history(user_key))


@app.delete("/api/history")
async def clear_history(request: Request):
    """Очищает историю текущего авторизованного пользователя."""
    user_key = _authorized_user_key_or_raise(request)
    if not user_key:
        return JSONResponse({"ok": False, "error": "История доступна только с личным API-ключом"}, status_code=401)
    _clear_user_history(user_key)
    return JSONResponse({"ok": True})


# ── Статика ──────────────────────────────────────────────────────────────────
_static = Path(__file__).parent / "static"
_static.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static)), name="static")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    ico_path = _static / "favicon.ico"
    if ico_path.exists():
        return FileResponse(str(ico_path), media_type="image/x-icon")
    return FileResponse(str(_static / "favicon.svg"), media_type="image/svg+xml")

@app.get("/")
@app.head("/")
async def root():
    return FileResponse(str(_static / "index.html"))

@app.head("/api/health")
@app.get("/api/health")
async def health():
    allowed_users = [
        e for e in _load_allowed_keys()
        if e.get("role", "user") == "user" and e.get("enabled", True)
    ]
    return {
        "status": "ok",
        "api_key_set": bool(ANTHROPIC_API_KEY),
        "auth_allow_all": AUTH_ALLOW_ALL,
        "allowed_user_keys": len(allowed_users),
        "guest_keys_blocked": len(_guest_key_hashes()),
        "guest_reconcile_limit": GUEST_RECONCILE_LIMIT,
        "guest_usage_window_days": GUEST_USAGE_WINDOW_DAYS,
        "guest_max_file_mb": round(GUEST_MAX_FILE_BYTES / 1024 / 1024, 2),
        "user_max_file_mb": round(USER_MAX_FILE_BYTES / 1024 / 1024, 2),
        "app_env": APP_ENV,
        "version": APP_VERSION,
        "admin_enabled": bool(ADMIN_SECRET),
    }
