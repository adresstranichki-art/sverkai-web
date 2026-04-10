"""sverkAI v2.1 — веб-версия (FastAPI) с поддержкой личных API-ключей"""
import os, re, json, tempfile, shutil, uuid, hashlib
from datetime import datetime
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

app = FastAPI(title="sverkAI API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_REPORT_DIR = Path(tempfile.gettempdir()) / "sverkai_reports"
_REPORT_DIR.mkdir(exist_ok=True)
_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_HISTORY_FILE      = _DATA_DIR / "history.json"         # legacy (не используется)
_ALLOWED_KEYS_FILE = _DATA_DIR / "allowed_keys.json"    # белый список (хеши ключей)
ADMIN_SECRET       = os.environ.get("ADMIN_SECRET", "") # для управления белым списком


# ════════════════════════════════════════════════════════════════════
#  АВТОРИЗАЦИЯ / ИСТОРИЯ ПО ПОЛЬЗОВАТЕЛЯМ
# ════════════════════════════════════════════════════════════════════

def _load_allowed_keys() -> list:
    """Загружает белый список. Каждая запись: {hash, label, added}."""
    if _ALLOWED_KEYS_FILE.exists():
        try:
            with open(_ALLOWED_KEYS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_allowed_keys(entries: list) -> None:
    with open(_ALLOWED_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _is_key_allowed(api_key: str) -> bool:
    """Проверяет, есть ли ключ в белом списке.
    Если файла нет или список пуст — dev-режим, всё разрешено."""
    if not _ALLOWED_KEYS_FILE.exists():
        return True
    entries = _load_allowed_keys()
    if not entries:
        return True
    h = _user_id(api_key)
    return any(e.get("hash") == h for e in entries)


def _user_id(api_key: str) -> str:
    """Хеш API-ключа — безопасный идентификатор пользователя."""
    return hashlib.sha256(api_key.strip().encode()).hexdigest()[:24]

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

def _get_user_key(request: Request) -> str:
    """Извлекает личный API-ключ пользователя из заголовка запроса."""
    return request.headers.get("X-Api-Key", "").strip()

def _effective_key(user_key: str) -> str:
    """Возвращает ключ пользователя, либо системный как fallback."""
    return user_key if user_key else ANTHROPIC_API_KEY


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
    re.compile(r'\(([A-Za-zА-Яа-я]*-?\d+[\w/]*)\s+от', re.IGNORECASE),
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
    raw = pd.read_excel(path, header=None, dtype=str)
    rows = []
    meta = _extract_balance_meta(raw)
    for idx in range(9, len(raw)):
        row = raw.iloc[idx]
        date_val = str(row[1]).strip() if pd.notna(row[1]) else ''
        doc_val  = str(row[2]).strip() if pd.notna(row[2]) else ''
        if not date_val or date_val == 'nan':
            continue
        if any(kw in date_val.lower() for kw in ['обороты', 'сальдо конечное', 'сальдо начальное']):
            continue
        debit  = str(row[4]).strip() if pd.notna(row[4]) else ''
        credit = str(row[6]).strip() if pd.notna(row[6]) else ''
        if not doc_val or doc_val == 'nan':
            continue
        m = re.search(r'\((\d+)\s+от\s', doc_val)
        doc_num = m.group(1) if m else None
        date_parsed = pd.to_datetime(date_val, dayfirst=True, errors='coerce')
        debit_val = _to_float(debit)
        credit_val = _to_float(credit)
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


def parse_two_sided_act(path: str) -> pd.DataFrame:
    """Парсит двусторонний акт сверки (формат 220): обе стороны в одном листе.
    Читает только сторону организации (левая половина):
    col 1 = дата, col 2 = документ, col 4 = дебет, col 6 = кредит (позиции авто-определяются)."""
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    date_re = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}$')
    meta = _extract_balance_meta(raw)
    # Автодетект колонок дебет/кредит: сканируем левую половину листа по нескольким строкам.
    date_col, doc_col, debit_col, credit_col = 1, 2, 4, 6
    from collections import Counter
    col_hits: Counter = Counter()
    mid = max(len(raw.columns) // 2, 8)
    scan_rows = []
    for i in range(5, min(50, len(raw))):
        if date_re.match(str(raw.iloc[i, 1]).strip()):
            scan_rows.append(i)
            if len(scan_rows) >= 30: break
    for i in scan_rows:
        for c in range(3, mid):
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
                     'signed_amount': float(credit or 0) - float(debit or 0),
                     'raw_row': idx})
    return _attach_meta(pd.DataFrame(rows), **meta)


def _parse_pdf_generic(path: str) -> pd.DataFrame:
    rows = []
    date_pattern = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}$')
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if tables:
                for table in tables:
                    for row in table:
                        if not row: continue
                        first = str(row[0] or '').strip()
                        if any(kw in first.lower() for kw in ['сальдо', 'обороты', 'генеральный', 'м.п.']):
                            continue
                        if not date_pattern.match(first): continue
                        cells = [str(c).strip() for c in row if c and str(c).strip()]
                        if len(cells) >= 2:
                            rows.append(cells)
                if rows: break
    if not rows:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    for line in text.split('\n'):
                        parts = line.strip().split()
                        if len(parts) >= 2 and date_pattern.match(parts[0]):
                            rows.append(parts)
    if not rows:
        raise Exception("PDF пустой или не содержит текста")
    max_cols = max(len(r) for r in rows)
    padded = [r + [''] * (max_cols - len(r)) for r in rows]
    return pd.DataFrame(padded, columns=[f"Col{i}" for i in range(max_cols)])


def parse_pdf_act_to_structured(path: str) -> pd.DataFrame:
    date_pattern = re.compile(r'^\d{2}\.\d{2}\.\d{2,4}$')
    rows = []
    raw_idx = 0
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            width = page.width
            right = page.crop((width * 0.5, 0, width, page.height))
            words = right.extract_words(x_tolerance=5, y_tolerance=5)
            if not words: continue
            lines: dict = {}
            for w in words:
                y = round(w['top'] / 6) * 6
                lines.setdefault(y, []).append(w)
            for y in sorted(lines):
                parts = [w['text'] for w in sorted(lines[y], key=lambda w: w['x0'])]
                if not parts or not date_pattern.match(parts[0]): continue
                if any(kw in ' '.join(parts).lower() for kw in ['сальдо', 'обороты', 'нижеподписавшиеся']): continue
                date_str = parts[0]
                date_parsed = pd.to_datetime(date_str, dayfirst=True, errors='coerce')
                amounts, doc_parts = [], []
                for p in parts[1:]:
                    clean = p.replace('\xa0', '').replace(' ', '').replace(',', '.')
                    try:
                        amounts.append(float(clean))
                    except ValueError:
                        doc_parts.append(p)
                document = ' '.join(doc_parts).strip()
                if not document: continue
                m = re.search(r'\(([A-Za-zА-Яа-я]*-?\d+[\w/]*)\s+от\s', document)
                if not m:
                    m = re.search(r'\((\d+[\w/]*)\)', document)
                doc_num = _normalize_doc_num(m.group(1)) if m else None
                debit, credit = None, None
                if amounts:
                    v = amounts[-1]
                    if v < 0: debit = v
                    elif 'оплата' in document.lower(): debit = v
                    else: credit = v
                rows.append({'date': date_parsed, 'date_str': date_str, 'document': document,
                             'doc_num': doc_num, 'debit': debit, 'credit': credit, 'raw_row': raw_idx})
                raw_idx += 1
    if not rows:
        return _parse_pdf_generic(path)
    return pd.DataFrame(rows)


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
            with pdfplumber.open(path) as pdf:
                if pdf.pages:
                    text = pdf.pages[0].extract_text() or ''
                    if 'акт сверки' in text.lower() or 'взаимных расчетов' in text.lower():
                        return 'pdf_act'
        except Exception:
            pass
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

    exact_pairs, amount_diff_pairs, seed_sign_mismatch_pairs = [], [], []
    for _, r1 in df1.iterrows():
        norm1 = _normalize_doc_num(r1['doc_num']) if r1.get('doc_num') else None
        if not norm1 or norm1 not in idx2_by_docnum: continue
        candidates = [c for c in idx2_by_docnum[norm1] if c['raw_row'] not in matched2]
        if not candidates: continue
        sum1 = _sf(r1.get('debit')) or _sf(r1.get('credit'))
        best = None
        for r2 in candidates:
            sum2 = _sf(r2.get('debit')) or _sf(r2.get('credit'))
            if sum1 is not None and sum2 is not None and abs(abs(sum1) - abs(sum2)) <= 0.01:
                best = r2; break
        if best is None and len(candidates) == 1:
            best = candidates[0]
            sum2 = _sf(best.get('debit')) or _sf(best.get('credit'))
            if sum1 is not None and sum2 is not None and abs(abs(sum1) - abs(sum2)) > 0.01:
                amount_diff_pairs.append((r1, best, sum1, sum2))
        if best is not None:
            matched1.add(r1['raw_row']); matched2.add(best['raw_row'])
            if _is_sign_mismatch_pair(r1, best):
                seed_sign_mismatch_pairs.append((r1, best))
            else:
                exact_pairs.append((r1, best))

    log("Шаг 2/3: Нечёткое сопоставление...")

    PENALTY_KW = {'штраф','санкции','пени','неустойка','контрафакт','fine','penalty','interest charge','forfeit'}
    _CAT_PAYMENT    = {'оплата','платеж','платёж','п/п','пп ','выплата','payment','pay ','transfer','wire','receipt','расходный кассов','приходный кассов'}
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

    unmatched1 = df1[~df1['raw_row'].isin(matched1)].copy()
    unmatched2 = df2[~df2['raw_row'].isin(matched2)].copy()

    for _, r1 in unmatched1.iterrows():
        d1 = _md(r1); cat1 = _cat(r1); found = False
        for s1, s2 in [('debit','credit'),('credit','debit'),('debit','debit'),('credit','credit')]:
            v1 = _sf(r1.get(s1))
            if v1 is None: continue
            for _, r2 in unmatched2.iterrows():
                if r2['raw_row'] in matched2: continue
                if any(t in str(r2.get('doc_type','')).lower()+' '+str(r2.get('document','')).lower() for t in PENALTY_KW): continue
                cat2 = _cat(r2)
                if cat1 != 'прочее' and cat2 != 'прочее' and cat1 != cat2: continue
                v2 = _sf(r2.get(s2))
                if v2 is None: continue
                if abs(abs(v1) - abs(v2)) > 0.01: continue
                if cat1 == 'корректировка' and cat2 == 'корректировка' and s1 != s2 and v1 * v2 < 0: continue
                d2 = _md(r2)
                dw = dw_payment if cat1 == 'оплата' else dw_delivery
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

    log("Шаг 3/3: Формирование отчёта...")

    def _ga(r):
        for col in ('debit','credit'):
            v = r.get(col)
            try:
                if v is not None and pd.notna(v) and float(v) != 0: return float(v)
            except: pass
        return 0.0

    discrepancies = []

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
            oc = _sf(ro.get('credit')); od = _sf(ro.get('debit'))
            if oc is not None:
                oa = abs(oc); ca = abs(_sf(rc.get('debit')) or 0.0)
                ol = f"Кредит: +{oa:,.2f} руб."; cl = f"Дебет: -{ca:,.2f} руб."
            else:
                oa = abs(od or 0.0); ca = abs(_sf(rc.get('credit')) or 0.0)
                ol = f"Дебет: -{oa:,.2f} руб."; cl = f"Кредит: +{ca:,.2f} руб."
            if min_amount and (oa + ca) / 2 < min_amount: continue
            discrepancies.append({'type':'sign_mismatch','document_number':ro.get('document',''),
                'description':'Одна операция, противоположный знак',
                'company_value':ol,'supplier_value':cl,'difference':f"{oa + ca:,.2f}",
                'severity':'high','row_company':ro.get('raw_row',0),'row_supplier':rc.get('raw_row',0),
                'date':ro.get('date_str','')})

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
                dw = dw_payment if cat == 'оплата' else dw_delivery
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

    critical = sum(1 for d in discrepancies if d['severity'] == 'high')
    ai_comment = ''
    if client and discrepancies and cfg.get('ai_comment', True):
        try:
            sample = [d for d in discrepancies if d['type'] != 'date_diff'][:30]
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
                    'transaction_net_difference': transaction_net_diff},
        'matched1': list(matched1), 'matched2': list(matched2),
        'missing_rows1': list(missing_in_2['raw_row'].tolist()),
        'missing_rows2': list(missing_in_1['raw_row'].tolist()),
        'amount_diff_rows1': [r1['raw_row'] for r1,r2,s1,s2 in amount_diff_pairs],
        'amount_diff_rows2': [r2['raw_row'] for r1,r2,s1,s2 in amount_diff_pairs],
        'fuzzy_rows1': [r1['raw_row'] for r1,_ in fuzzy_matches],
        'fuzzy_rows2': [r2['raw_row'] for _,r2 in fuzzy_matches],
        'sign_mismatch_rows1': [ro['raw_row'] for ro,_ in smm_pairs],
        'sign_mismatch_rows2': [rc['raw_row'] for _,rc in smm_pairs],
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
    eff_key = _effective_key(api_key)
    display_df = parse_generic(path)
    # Используем оригинальное имя для кеша; если не передано — имя tempfile
    cache_name = original_filename or Path(path).name

    # ── 1. Определяем тип по содержимому ───────────────────────────
    ftype = detect_file_type(path)

    if ftype == 'proopt':
        return parse_proopt(path), display_df, ftype, 'ПРООПТ'

    if ftype == 'emex':
        return parse_emex(path), display_df, ftype, 'ЭМЕКС'

    if ftype == 'partner_ledger_act':
        df = parse_partner_ledger_act(path)
        if not df.empty:
            return df, display_df, 'generic_detected', 'Акт сверки (реестр проводок)'

    if ftype == 'balance_state_act':
        df = parse_balance_state_act(path)
        if not df.empty:
            return df, display_df, 'generic_detected', 'Акт сверки (сальдо по операциям)'

    if ftype == 'pdf_act':
        return parse_pdf_act_to_structured(path), display_df, 'generic_detected', 'PDF акт сверки'

    if ftype == 'standard_act':
        df = parse_standard_act(path)
        if not df.empty:
            return df, display_df, 'generic_detected', 'Акт сверки (односторонний)'

    if ftype == 'two_sided_act':
        df = parse_two_sided_act(path)
        if not df.empty:
            return df, display_df, 'generic_detected', 'Акт сверки (двусторонний)'

    if ftype == 'counterparty':
        df = parse_counterparty(path)
        if not df.empty:
            return df, display_df, 'generic_detected', 'Акт сверки (контрагент)'

    # ── 2. Неизвестный формат — кеш профиля или Claude ─────────────
    cache_file = _DATA_DIR / 'col_profiles.json'
    cache: dict = {}
    if cache_file.exists():
        try:
            with open(cache_file, encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            pass
    cache_key = f"col_profile_{cache_name}"
    if cache_key in cache:
        logs.append(f"Использую сохранённый профиль для {cache_name}")
        profile = cache[cache_key]
        df = parse_with_profile(path, profile)
        if not df.empty:
            return df, display_df, 'generic_detected', 'Кеш профиля'

    if not eff_key:
        raise HTTPException(status_code=422,
            detail=f"Формат файла '{cache_name}' не распознан автоматически. "
                   "Укажите API-ключ для определения структуры через AI.")

    logs.append(f"Анализирую структуру {cache_name} через AI...")
    profile = claude_detect_columns(path, eff_key)

    if not profile or profile.get('confidence') == 'low':
        raise HTTPException(status_code=422,
            detail=f"Не удалось определить структуру файла '{cache_name}'. "
                   "Попробуйте другой файл или обратитесь к администратору.")

    df = parse_with_profile(path, profile)
    if df.empty:
        raise HTTPException(status_code=422,
            detail=f"Файл '{cache_name}' распознан, но не содержит транзакций. "
                   "Проверьте, что файл содержит данные сверки.")

    # Кешируем профиль по оригинальному имени файла
    cache[cache_key] = profile
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    confidence = profile.get('confidence', 'medium')
    logs.append(f"Структура {cache_name} определена (уверенность: {confidence}), строк: {len(df)}")
    return df, display_df, 'generic_detected', f'Автодетект AI ({confidence})'


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
    """Проверяет Anthropic API-ключ: формат → белый список → реальный вызов."""
    key = payload.get("api_key", "").strip()
    if not key:
        return JSONResponse({"valid": False, "error": "Ключ не указан"})
    if not key.startswith("sk-ant-"):
        return JSONResponse({"valid": False, "error": "Неверный формат ключа (должен начинаться с sk-ant-)"})
    # Проверка белого списка
    if not _is_key_allowed(key):
        return JSONResponse({"valid": False, "error": "Ключ не входит в список разрешённых. Обратитесь к администратору."})
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
    if not key:
        raise HTTPException(status_code=400, detail="api_key не указан")
    h = _user_id(key)
    entries = _load_allowed_keys()
    if any(e.get("hash") == h for e in entries):
        return JSONResponse({"ok": False, "message": "Ключ уже в списке", "hash": h})
    entries.append({"hash": h, "label": label, "added": datetime.now().strftime("%Y-%m-%d")})
    _save_allowed_keys(entries)
    return JSONResponse({"ok": True, "hash": h, "label": label, "total": len(entries)})


@app.post("/api/admin/remove-key")
async def admin_remove_key(payload: dict):
    """Удаляет ключ из белого списка по хешу или по api_key."""
    if not ADMIN_SECRET or payload.get("admin_secret") != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Нет доступа")
    h = payload.get("hash", "").strip()
    if not h and payload.get("api_key"):
        h = _user_id(payload["api_key"].strip())
    entries = _load_allowed_keys()
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
    user_key = _get_user_key(request)
    eff_key  = _effective_key(user_key)

    try:
        cfg = {**DEFAULT_RECON_SETTINGS, **json.loads(settings)}
    except Exception:
        cfg = DEFAULT_RECON_SETTINGS.copy()

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp()
        ext1 = Path(file1.filename).suffix.lower()
        ext2 = Path(file2.filename).suffix.lower()
        p1 = os.path.join(tmpdir, f"file1{ext1}")
        p2 = os.path.join(tmpdir, f"file2{ext2}")
        with open(p1, 'wb') as f: f.write(await file1.read())
        with open(p2, 'wb') as f: f.write(await file2.read())

        logs = []
        try:
            df1p, df1d, ft1, lb1 = _load_and_parse(p1, logs, user_key, file1.filename)
        except HTTPException: raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Ошибка чтения файла 1 ({file1.filename}): {e}")
        try:
            df2p, df2d, ft2, lb2 = _load_and_parse(p2, logs, user_key, file2.filename)
        except HTTPException: raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Ошибка чтения файла 2 ({file2.filename}): {e}")

        logs.append(f"Файл 1: {file1.filename} → {lb1} ({len(df1p)} строк)")
        logs.append(f"Файл 2: {file2.filename} → {lb2} ({len(df2p)} строк)")

        client = Anthropic(api_key=eff_key) if eff_key else None

        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            ThreadPoolExecutor(max_workers=1),
            lambda: hybrid_reconcile(df1p, df2p, ft1, ft2, client,
                                     progress_cb=lambda t: logs.append(t), settings=cfg)
        )

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
        })
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/preview")
async def preview_file(request: Request, file: UploadFile = File(...)):
    """Парсит один файл и возвращает таблицу для предпросмотра + статус детекта."""
    user_key = _get_user_key(request)
    eff_key  = _effective_key(user_key)

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp()
        ext = Path(file.filename).suffix.lower()
        path = os.path.join(tmpdir, f"preview{ext}")
        with open(path, "wb") as f: f.write(await file.read())

        ftype = detect_file_type(path)
        profile = None
        label = ""
        needs_manual = False

        if ftype == "proopt":
            df = parse_proopt(path); label = "ПРООПТ"
        elif ftype == "emex":
            df = parse_emex(path); label = "ЭМЕКС"
        elif ftype == "pdf_act":
            df = parse_pdf_act_to_structured(path); label = "PDF акт сверки"; ftype = "generic_detected"
        elif ftype == "balance_state_act":
            df = parse_balance_state_act(path); label = "Акт сверки (сальдо по операциям)"; ftype = "generic_detected"
        elif ftype == "standard_act":
            df = parse_standard_act(path); label = "Акт сверки (односторонний)"; ftype = "generic_detected"
        elif ftype == "two_sided_act":
            df = parse_two_sided_act(path); label = "Акт сверки (двусторонний)"; ftype = "generic_detected"
        elif ftype == "counterparty":
            df = parse_counterparty(path); label = "Акт сверки (контрагент)"; ftype = "generic_detected"
        else:
            if eff_key:
                profile = claude_detect_columns(path, eff_key)
                if profile and profile.get("confidence") != "low":
                    df = parse_with_profile(path, profile)
                    label = "Автодетект AI"; ftype = "generic_detected"
                else:
                    df = parse_generic(path); label = "Требуется настройка"; needs_manual = True
            else:
                df = parse_generic(path); label = "Требуется настройка"; needs_manual = True

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
async def export_report(payload: dict):
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
               'amount_diff':'💰 Разница в суммах','date_diff':'📅 Разница в датах','sign_mismatch':'🔀 Зеркальная корректировка'}
    SEV_RU = {'high':'Высокий','medium':'Средний','low':'Низкий'}
    for ri, d in enumerate(discs, 1):
        sev = d.get('severity','low')
        tp  = d.get('type','')
        fmt = red if sev=='high' and tp!='sign_mismatch' else blu if tp=='sign_mismatch' else yel if sev=='medium' else gry
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
    user_key = _get_user_key(request)
    return JSONResponse(_load_user_history(user_key))


# ── Статика ──────────────────────────────────────────────────────────────────
_static = Path(__file__).parent / "static"
_static.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static)), name="static")

@app.get("/")
@app.head("/")
async def root():
    return FileResponse(str(_static / "index.html"))

@app.head("/api/health")
@app.get("/api/health")
async def health():
    return {"status": "ok", "api_key_set": bool(ANTHROPIC_API_KEY)}
