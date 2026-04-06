"""sverkAI v2.0 — веб-версия (FastAPI)"""
import os, re, json, tempfile, shutil, uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import pdfplumber
import xlsxwriter
from anthropic import Anthropic

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
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
_HISTORY_FILE = _DATA_DIR / "history.json"


# ════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════════════════════════════

def _to_float(s: str) -> Optional[float]:
    if not s or s == 'nan':
        return None
    try:
        cleaned = str(s).replace('\u2212', '-').replace(',', '.').replace(' ', '').replace('\xa0', '')
        v = float(cleaned)
        return None if v == 0 else v
    except Exception:
        return None


def _normalize_doc_num(num: str) -> str:
    if not num:
        return num
    cleaned = re.sub(r'^[А-ЯA-Zа-яa-z]+-', '', str(num).strip())
    cleaned = cleaned.lstrip('0') or cleaned
    return cleaned.lower()


# ════════════════════════════════════════════════════════════════════
#  ПАРСЕРЫ
# ════════════════════════════════════════════════════════════════════

def parse_proopt(path: str) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None, dtype=str)
    rows = []
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
        rows.append({'date': date_parsed, 'date_str': date_val, 'document': doc_val,
                     'doc_num': doc_num, 'debit': _to_float(debit), 'credit': _to_float(credit),
                     'raw_row': idx})
    return pd.DataFrame(rows)


def parse_emex(path: str) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None, dtype=str)
    rows = []
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
        rows.append({'date': date_parsed, 'date_str': date_val, 'document': doc_val,
                     'doc_num': doc_num, 'doc_type': doc_type,
                     'debit': _to_float(debit), 'credit': _to_float(credit), 'raw_row': idx})
    return pd.DataFrame(rows)


def parse_counterparty(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, engine=engine, header=None, dtype=str)
    date_pattern = re.compile(r'\((\d{2}\.\d{2}\.\d{4})')
    rows = []
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
        m_num = re.search(r'[№#](\d+)', doc_val)
        if not m_num:
            m_num = re.search(r',(\d{4,})\)', doc_val)
        doc_num = _normalize_doc_num(m_num.group(1)) if m_num else None
        rows.append({'date': date_parsed, 'date_str': date_str, 'document': doc_val,
                     'doc_num': doc_num, 'debit': _to_float(debit), 'credit': _to_float(credit), 'raw_row': idx})
    return pd.DataFrame(rows)


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
    _DOC_NUM_PATTERNS = [
        re.compile(r'[No№#]\s*(\d[\w/-]*)'),
        re.compile(r'\(([A-Za-zА-Яа-я]*-?\d+[\w/]*)\s+от'),
        re.compile(r'[-/](\d{3,})\b'),
        re.compile(r'\b(\d{4,})\b'),
    ]
    def _extract_doc_num_from_text(doc_val: str) -> Optional[str]:
        for pattern in _DOC_NUM_PATTERNS:
            m = pattern.search(doc_val)
            if m: return _normalize_doc_num(m.group(1))
        return None
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
                   else _extract_doc_num_from_text(doc_val))
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
        if 'Номер документа' in text and 'Эмекс' in text: return 'emex'
        if 'Дата операции' in text and 'Тип документа' in text: return 'emex'
        is_act = ('акт сверки' in text.lower() or 'взаимных расчетов' in text.lower() or 'По данным ООО' in text)
        if is_act:
            proopt_pos = text.find('По данным ООО "ПРООПТ"')
            other_pos = -1
            for match in re.finditer(r'По данным [А-Яа-я]+ "(?!ПРООПТ)', text):
                other_pos = match.start(); break
            if proopt_pos != -1 and (other_pos == -1 or proopt_pos < other_pos): return 'proopt'
            if other_pos != -1 and (proopt_pos == -1 or other_pos < proopt_pos): return 'counterparty'
            if 'ПРООПТ' in text: return 'proopt'
            return 'counterparty'
    except Exception:
        pass
    return 'generic'


# ════════════════════════════════════════════════════════════════════
#  АВТОДЕТЕКТ ЧЕРЕЗ CLAUDE
# ════════════════════════════════════════════════════════════════════

def claude_detect_columns(path: str, api_key: str) -> Optional[dict]:
    cache_key = f"col_profile_{Path(path).name}"
    cache_file = _DATA_DIR / 'col_profiles.json'
    cache = {}
    if cache_file.exists():
        try:
            with open(cache_file, encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            pass
    if cache_key in cache:
        return cache[cache_key]
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

Верни ТОЛЬКО корректный JSON без markdown, без комментариев:
{{
  "header_row": <номер строки с заголовками (0-based), или null>,
  "data_start_row": <номер первой строки с данными транзакций (0-based)>,
  "date_col": <индекс колонки с датой операции (0-based)>,
  "doc_col": <индекс колонки с наименованием документа (0-based)>,
  "doc_num_col": <индекс колонки с ТОЛЬКО номером документа (0-based), или null>,
  "doc_type_col": <индекс колонки с типом операции (0-based), или null>,
  "debit_col": <индекс колонки Дебет (0-based), или null>,
  "credit_col": <индекс колонки Кредит (0-based), или null>,
  "amount_col": <индекс единственной колонки суммы (0-based), или null>,
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
            cache[cache_key] = profile
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
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

    exact_pairs, amount_diff_pairs = [], []
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
        d1 = r1['date']; cat1 = _cat(r1); found = False
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
                d2 = r2['date']
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

    _sign_pat = re.compile(
        r'корректировк|ксф|возврат|сторно|исправлени|аннулирован|зачет|зачёт|'
        r'adjustment|correction|credit.?note|reversal|refund|write.?off|reverse', re.IGNORECASE)

    def _find_smm(side_a, side_b, col_a, col_b):
        pairs, rem_a, used_b = [], set(), set()
        sa = side_a[side_a['document'].str.contains(_sign_pat, na=False) & side_a[col_a].notna()]
        sb = side_b[side_b['document'].str.contains(_sign_pat, na=False) & side_b[col_b].notna()]
        for _, ra in sa.iterrows():
            va = _sf(ra.get(col_a))
            if va is None: continue
            da = ra['date']
            for _, rb in sb.iterrows():
                if rb['raw_row'] in used_b: continue
                vb = _sf(rb.get(col_b))
                if vb is None: continue
                if abs(abs(va) - abs(vb)) > 0.01: continue
                db = rb['date']
                if ((pd.notna(da) and pd.notna(db) and abs((da - db).days) <= 5) or pd.isna(da) or pd.isna(db)):
                    pairs.append((ra, rb)); rem_a.add(ra['raw_row']); used_b.add(rb['raw_row']); break
        return pairs, rem_a, used_b

    p1, ra1, rb1 = _find_smm(missing_in_2, missing_in_1, 'credit', 'debit')
    m2r = missing_in_2[~missing_in_2['raw_row'].isin(ra1)]
    m1r = missing_in_1[~missing_in_1['raw_row'].isin(rb1)]
    p2, ra2, rb2 = _find_smm(m2r, m1r, 'debit', 'credit')
    smm_pairs = p1 + p2
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

    net_period = 0.0
    for _, r in missing_in_2.iterrows(): net_period += _sfz(r.get('credit')) - _sfz(r.get('debit'))
    for _, r in missing_in_1.iterrows(): net_period += abs(_sfz(r.get('debit'))) + _sfz(r.get('credit'))
    for ro, rc in smm_pairs:
        net_period += abs(_sfz(ro.get('credit')) or _sfz(ro.get('debit'))) * 2
    for r1, r2, s1, s2 in amount_diff_pairs: net_period += (abs(s2) - abs(s1))

    all_dates = []
    for df in (df1, df2):
        if 'date' in df.columns:
            all_dates += [d for d in df['date'] if pd.notna(d)]
    period_str = (f"{min(all_dates).strftime('%d.%m.%Y')} - {max(all_dates).strftime('%d.%m.%Y')}"
                  if all_dates else '')

    if abs(net_period) < 0.01:
        debt_label = f'Взаиморасчёты совпадают (за период {period_str})' if period_str else 'Взаиморасчёты совпадают'
    elif net_period > 0:
        debt_label = f'Организация должна контрагенту: {net_period:,.2f} руб. (за период {period_str})'
    else:
        debt_label = f'Контрагент должен организации: {abs(net_period):,.2f} руб. (за период {period_str})'

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
                    'ai_comment': ai_comment, 'period': period_str},
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
#  ИСТОРИЯ
# ════════════════════════════════════════════════════════════════════

def _load_history():
    if _HISTORY_FILE.exists():
        try:
            with open(_HISTORY_FILE, encoding='utf-8') as f: return json.load(f)
        except: pass
    return []

def _save_history(h):
    with open(_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(h[-50:], f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ЗАГРУЗКИ
# ════════════════════════════════════════════════════════════════════

def _load_and_parse(path: str, logs: list):
    ftype = detect_file_type(path)
    display_df = parse_generic(path)
    if ftype == 'proopt':
        return parse_proopt(path), display_df, ftype, 'ПРООПТ'
    elif ftype == 'emex':
        return parse_emex(path), display_df, ftype, 'ЭМЕКС'
    elif ftype == 'counterparty':
        return parse_counterparty(path), display_df, 'generic_detected', 'Акт сверки (контрагент)'
    elif ftype == 'pdf_act':
        return parse_pdf_act_to_structured(path), display_df, 'generic_detected', 'PDF акт сверки'
    else:
        if not ANTHROPIC_API_KEY:
            raise HTTPException(status_code=422,
                detail=f"Формат файла '{Path(path).name}' не распознан. "
                       "Необходим ANTHROPIC_API_KEY для автодетекта.")
        logs.append(f"Анализирую структуру {Path(path).name} через AI...")
        profile = claude_detect_columns(path, ANTHROPIC_API_KEY)
        if not profile or profile.get('confidence') == 'low':
            raise HTTPException(status_code=422,
                detail=f"Не удалось определить структуру файла '{Path(path).name}'. "
                       "Обратитесь к администратору.")
        return parse_with_profile(path, profile), display_df, 'generic_detected', 'Автодетект AI'


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

@app.post("/api/reconcile")
async def reconcile(
    file1: UploadFile = File(...),
    file2: UploadFile = File(...),
    settings: str = Form(default="{}")
):
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
            df1p, df1d, ft1, lb1 = _load_and_parse(p1, logs)
        except HTTPException: raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Ошибка чтения файла 1 ({file1.filename}): {e}")
        try:
            df2p, df2d, ft2, lb2 = _load_and_parse(p2, logs)
        except HTTPException: raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Ошибка чтения файла 2 ({file2.filename}): {e}")

        logs.append(f"Файл 1: {file1.filename} → {lb1} ({len(df1p)} строк)")
        logs.append(f"Файл 2: {file2.filename} → {lb2} ({len(df2p)} строк)")

        client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

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

        history = _load_history()
        history.append({'date': datetime.now().strftime('%d.%m.%Y %H:%M'),
                        'file1': file1.filename, 'file2': file2.filename,
                        'total': result['summary'].get('total_discrepancies', 0),
                        'critical': result['summary'].get('critical_count', 0),
                        'debt_label': result['summary'].get('debt_label', '')})
        _save_history(history)

        return JSONResponse({'ok': True, 'logs': logs, 'summary': result['summary'],
            'discrepancies': result['discrepancies'],
            'table1': _prep(df1p), 'table2': _prep(df2p),
            'highlight': {
                'missing1': result.get('missing_rows1',[]), 'missing2': result.get('missing_rows2',[]),
                'amt_diff1': result.get('amount_diff_rows1',[]), 'amt_diff2': result.get('amount_diff_rows2',[]),
                'sign1': result.get('sign_mismatch_rows1',[]), 'sign2': result.get('sign_mismatch_rows2',[]),
                'date1': result.get('date_diff_rows1',[]), 'date2': result.get('date_diff_rows2',[]),
            },
            'file1_name': file1.filename, 'file2_name': file2.filename})
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
async def get_history():
    return JSONResponse(_load_history())


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
