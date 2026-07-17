"""Independent Claude reconciliation contract without standard-matcher conclusions."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

import pandas as pd


EXPERT_CATEGORY_ORDER = (
    'confirmed_missing',
    'sign_difference',
    'amount_difference',
    'opening_balance_bridge',
    'likely_date_pair',
    'ambiguous',
)
EXPERT_CATEGORIES = EXPERT_CATEGORY_ORDER
EXPERT_CONFIDENCE = ('high', 'medium', 'low')
DEFAULT_EXPERT_SCOPE = {
    'find_missing': True,
    'find_amount_diff': True,
    'find_sign_mismatch': True,
    'find_date_diff': True,
    'date_window_payment': 5,
    'date_window_delivery': 3,
    'min_amount': 0.0,
}

EXPERT_REPORT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'version', 'conclusion', 'confidence',
        'discrepancies',
    ],
    'properties': {
        'version': {'type': 'string', 'enum': ['1']},
        'conclusion': {'type': 'string', 'maxLength': 400},
        'confidence': {'type': 'string', 'enum': list(EXPERT_CONFIDENCE)},
        'discrepancies': {
            'type': 'array',
            'maxItems': 100,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'category', 'title', 'influence', 'reason',
                    'confidence', 'evidence',
                ],
                'properties': {
                    'category': {'type': 'string', 'enum': list(EXPERT_CATEGORIES)},
                    'title': {'type': 'string', 'maxLength': 120},
                    'influence': {'type': 'number'},
                    'reason': {'type': 'string', 'maxLength': 200},
                    'confidence': {'type': 'string', 'enum': list(EXPERT_CONFIDENCE)},
                    'evidence': {
                        'type': 'array',
                        'maxItems': 20,
                        'items': {
                            'type': 'object',
                            'additionalProperties': False,
                            'required': ['side', 'row_id'],
                            'properties': {
                                'side': {'type': 'string', 'enum': ['doc1', 'doc2']},
                                'row_id': {'type': 'string', 'maxLength': 80},
                            },
                        },
                    },
                },
            },
        },
    },
}


_UNSUPPORTED_CLAUDE_SCHEMA_CONSTRAINTS = frozenset({
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'minLength', 'maxLength', 'maxItems', 'multipleOf',
})


def _claude_output_schema(value: Any) -> Any:
    """Keep the strict shape while removing constraints Claude cannot compile."""
    if isinstance(value, dict):
        return {
            key: _claude_output_schema(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED_CLAUDE_SCHEMA_CONSTRAINTS
        }
    if isinstance(value, list):
        return [_claude_output_schema(item) for item in value]
    return value


CLAUDE_EXPERT_REPORT_SCHEMA = _claude_output_schema(EXPERT_REPORT_SCHEMA)


EXPERT_SYSTEM_PROMPT = (
    'Ты бухгалтер-эксперт. Независимо сверь два акта только по переданным строкам. '
    'balance_comparison уже рассчитан программой из исходных сальдо: используй его без пересчёта. '
    'Порядок категорий: confirmed_missing, sign_difference, amount_difference, '
    'opening_balance_bridge, likely_date_pair, ambiguous. '
    'analysis_scope — обязательные границы анализа. При false не ищи и не возвращай: '
    'find_missing→confirmed_missing, find_sign_mismatch→sign_difference, '
    'find_amount_diff→amount_difference, find_date_diff→likely_date_pair. '
    'Для likely_date_pair соблюдай date_window_payment для оплат и date_window_delivery для поставок. '
    'Не возвращай расхождения меньше min_amount; opening_balance_bridge разрешён всегда. '
    'В пользовательских conclusion, title и reason называй стороны только по documents[].display_name; '
    'не пиши doc1, doc2, d1 или d2. Технические side и row_id используй только в evidence. '
    'Одинаковая операция с '
    'другой датой — likely_date_pair. opening_balance_bridge — только операция, закрывающая '
    'начальную разницу, а не само сальдо; укажи evidence операции. '
    'likely_date_pair требует одинаковый модуль суммы и evidence из обоих документов; '
    'иначе это не пара. '
    'confirmed_missing ставь только при высокой уверенности. '
    'Приход и продажа, корректировки прихода и продажи считай зеркальными типами; '
    'сначала исключи пары по модулю суммы и смыслу документа. '
    'Группируй строго в порядке: confirmed_missing, sign_difference, amount_difference, '
    'opening_balance_bridge, likely_date_pair, ambiguous. В группе сортируй по убыванию модуля influence. '
    'Сначала перечисли все confirmed_missing; при лимите убирай ambiguous и likely_date_pair первыми. '
    'Дай не более 8 расхождений: confirmed_missing не группируй, остальные группируй. '
    'Не выдумывай row_id: evidence содержит только id из входа. Пиши кратко, без Markdown.'
)


def _number(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _date_text(row: pd.Series) -> str | None:
    value = row.get('date_str')
    if value is not None and str(value).strip() and str(value).lower() != 'nan':
        return str(value).strip()
    value = row.get('date')
    try:
        if value is not None and pd.notna(value):
            return pd.Timestamp(value).strftime('%d.%m.%Y')
    except Exception:
        pass
    return None


def _document_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r'\s+', ' ', str(value or '').replace('\n', ' ')).strip()
    return text[:limit]


def _normalized_document(value: Any) -> str:
    return re.sub(r'[^a-zа-яё0-9]+', '', str(value or '').lower())


def _balance_payload(df: pd.DataFrame, prefix: str) -> dict:
    result = {}
    for output_key, attr_prefix in (
        ('opening_balance', 'start_balance'),
        ('closing_balance', 'end_balance'),
    ):
        amount = _number(df.attrs.get(attr_prefix))
        if amount is None:
            continue
        result[output_key] = {
            'amount': amount,
            'side': str(df.attrs.get(f'{attr_prefix}_side') or 'unknown'),
        }
        raw_row = df.attrs.get(f'{attr_prefix}_raw_row')
        if raw_row is not None:
            result[output_key]['row_id'] = f'{prefix}:r{raw_row}'
    period = {}
    for output_key, attr_key in (('from', 'period_from'), ('to', 'period_to')):
        value = df.attrs.get(attr_key)
        try:
            if value is not None and pd.notna(value):
                period[output_key] = pd.Timestamp(value).strftime('%d.%m.%Y')
        except Exception:
            continue
    if period:
        result['period'] = period
    return result


def _normalized_balance(df: pd.DataFrame, attr_prefix: str, side: str) -> float | None:
    amount = _number(df.attrs.get(attr_prefix))
    balance_side = str(df.attrs.get(f'{attr_prefix}_side') or '').lower()
    if amount is None or balance_side not in ('debit', 'credit'):
        return None
    if side == 'doc1':
        return amount if balance_side == 'debit' else -amount
    return amount if balance_side == 'credit' else -amount


def _balance_number_text(value: float | None) -> str:
    if value is None:
        return '—'
    return f'{value:,.2f}'.replace(',', ' ').replace('.', ',')


def _balance_comparison(df1: pd.DataFrame, df2: pd.DataFrame) -> dict:
    opening1 = _normalized_balance(df1, 'start_balance', 'doc1')
    opening2 = _normalized_balance(df2, 'start_balance', 'doc2')
    closing1 = _normalized_balance(df1, 'end_balance', 'doc1')
    closing2 = _normalized_balance(df2, 'end_balance', 'doc2')
    opening = None if opening1 is None or opening2 is None else round(opening2 - opening1, 2)
    closing = None if closing1 is None or closing2 is None else round(closing2 - closing1, 2)
    movement = None if opening is None or closing is None else round(closing - opening, 2)
    return {
        'opening_difference': opening,
        'period_movement': movement,
        'closing_difference': closing,
        'formula': (
            f'{_balance_number_text(opening)} + {_balance_number_text(movement)} '
            f'= {_balance_number_text(closing)}'
        ),
    }


def _normalize_expert_scope(settings: dict | None = None) -> dict:
    source = settings if isinstance(settings, dict) else {}
    scope = dict(DEFAULT_EXPERT_SCOPE)
    for key in (
        'find_missing', 'find_amount_diff',
        'find_sign_mismatch', 'find_date_diff',
    ):
        if key in source:
            scope[key] = bool(source[key])
    for key in ('date_window_payment', 'date_window_delivery'):
        try:
            scope[key] = max(0, min(120, int(source.get(key, scope[key]))))
        except (TypeError, ValueError):
            pass
    amount = _number(source.get('min_amount', scope['min_amount']))
    if amount is not None:
        scope['min_amount'] = max(0.0, amount)
    return scope


def build_expert_payload(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    settings: dict | None = None,
) -> tuple[dict, dict[tuple[str, str], dict]]:
    documents = []
    row_index: dict[tuple[str, str], dict] = {}
    for side, prefix, df in (
        ('doc1', 'd1', df1),
        ('doc2', 'd2', df2),
    ):
        rows = []
        used_ids = set()
        for position, (_, row) in enumerate(df.iterrows()):
            raw_row = row.get('raw_row')
            if raw_row is None or (not isinstance(raw_row, str) and pd.isna(raw_row)):
                raw_row = position
            base_id = f'{prefix}:r{raw_row}'
            row_id = base_id
            suffix = 2
            while row_id in used_ids:
                row_id = f'{base_id}:{suffix}'
                suffix += 1
            used_ids.add(row_id)
            date = _date_text(row)
            document = _document_text(row.get('document'))
            debit = _number(row.get('debit'))
            credit = _number(row.get('credit'))
            compact = {'id': row_id}
            if date:
                compact['date'] = date
            if document:
                compact['document'] = document
            if debit is not None:
                compact['debit'] = debit
            if credit is not None:
                compact['credit'] = credit
            rows.append(compact)
            amount = debit if debit is not None else credit
            row_index[(side, row_id)] = {
                'side': side,
                'row_id': row_id,
                'raw_row': raw_row,
                'date': date,
                'document': document,
                'document_norm': _normalized_document(document),
                'amount': amount,
                'debit': debit,
                'credit': credit,
            }
        source_name = _document_text(df.attrs.get('source_name'), 120)
        display_name = (
            _document_text(df.attrs.get('display_name'), 120)
            or source_name
            or f'Документ {len(documents) + 1}'
        )
        document_payload = {
            'side': side,
            'display_name': display_name,
            'rows': rows,
            **_balance_payload(df, prefix),
        }
        if source_name and source_name != display_name:
            document_payload['source_name'] = source_name
        documents.append(document_payload)
    return {
        'version': '1',
        'analysis_scope': _normalize_expert_scope(settings),
        'balance_comparison': _balance_comparison(df1, df2),
        'documents': documents,
    }, row_index


def _fallback_evidence(
    discrepancy: dict,
    side: str,
    row_index: dict[tuple[str, str], dict],
) -> dict | None:
    expected_date = discrepancy.get(f'{side}_date')
    expected_document = _normalized_document(discrepancy.get(f'{side}_document'))
    expected_amount = _number(discrepancy.get(f'{side}_value'))
    if expected_amount is None or not (expected_date or expected_document):
        return None
    candidates = []
    for (candidate_side, _), row in row_index.items():
        if candidate_side != side:
            continue
        if expected_date and row.get('date') != expected_date:
            continue
        amount = _number(row.get('amount'))
        if amount is None or abs(abs(amount) - abs(expected_amount)) > 0.01:
            continue
        if expected_document and row.get('document_norm') != expected_document:
            continue
        candidates.append(row)
    return candidates[0] if len(candidates) == 1 else None


def resolve_expert_evidence(
    report: dict,
    row_index: dict[tuple[str, str], dict],
) -> tuple[dict, list[str]]:
    resolved_report = copy.deepcopy(report)
    warnings = []
    for discrepancy in resolved_report.get('discrepancies', []):
        resolved_rows = []
        unresolved = 0
        for evidence in discrepancy.get('evidence', []):
            side = evidence.get('side')
            row_id = evidence.get('row_id')
            row = row_index.get((side, row_id))
            if row is None and side in ('doc1', 'doc2'):
                row = _fallback_evidence(discrepancy, side, row_index)
            if row is None:
                unresolved += 1
                continue
            resolved_rows.append({
                'side': row['side'],
                'row_id': row['row_id'],
                'raw_row': row['raw_row'],
                'date': row.get('date'),
                'document': row.get('document'),
                'amount': row.get('amount'),
            })
        discrepancy['resolved_evidence'] = resolved_rows
        discrepancy['clickable'] = bool(resolved_rows)
        if unresolved:
            warnings.append('evidence_not_found')
            discrepancy['evidence_warning'] = 'Источник не найден'
    status = 'complete_with_warnings' if warnings else 'complete'
    return {
        'status': status,
        'report': resolved_report,
        'warnings': list(dict.fromkeys(warnings)),
    }, warnings


def _validate_report_shape(report: Any) -> bool:
    if not isinstance(report, dict):
        return False
    required = {
        'version', 'conclusion', 'confidence', 'balances', 'totals',
        'discrepancies',
    }
    if not required.issubset(report):
        return False
    if report.get('confidence') not in EXPERT_CONFIDENCE:
        return False
    if not isinstance(report.get('discrepancies'), list):
        return False
    return all(
        isinstance(item, dict)
        and item.get('category') in EXPERT_CATEGORIES
        and item.get('confidence') in EXPERT_CONFIDENCE
        and isinstance(item.get('evidence'), list)
        for item in report['discrepancies']
    )


def _derive_report_totals(report: dict) -> dict:
    discrepancies = report.get('discrepancies') or []
    confirmed = [
        item for item in discrepancies
        if item.get('category') == 'confirmed_missing'
    ]
    confirmed_amount = sum(
        abs(_number(item.get('influence')) or 0.0)
        for item in confirmed
    )
    return {
        'confirmed_count': len(confirmed),
        'confirmed_amount': round(confirmed_amount, 2),
        'review_count': max(0, len(discrepancies) - len(confirmed)),
    }


def _sort_report_discrepancies(report: dict) -> None:
    priority = {
        category: index
        for index, category in enumerate(EXPERT_CATEGORY_ORDER)
    }
    discrepancies = report.get('discrepancies') or []
    discrepancies.sort(key=lambda item: (
        priority.get(item.get('category'), len(priority)),
        -abs(_number(item.get('influence')) or 0.0),
    ))


def _document_family(value: Any) -> str:
    text = _normalized_document(value)
    if 'корректиров' in text and ('приход' in text or 'продаж' in text):
        return 'adjustment'
    if 'оплат' in text or 'платеж' in text:
        return 'payment'
    if 'приход' in text or 'продаж' in text or 'поставк' in text:
        return 'delivery'
    return text


def _date_distance_days(left: Any, right: Any) -> int | None:
    try:
        if not left or not right:
            return None
        return abs((pd.to_datetime(left, dayfirst=True) - pd.to_datetime(right, dayfirst=True)).days)
    except Exception:
        return None


def _mirror_row(source: dict, row_index: dict[tuple[str, str], dict]) -> dict | None:
    source_amount = _number(source.get('amount'))
    if source_amount is None:
        return None
    source_side = source.get('side')
    opposite = 'doc2' if source_side == 'doc1' else 'doc1'
    family = _document_family(source.get('document'))
    candidates = []
    for (side, _), row in row_index.items():
        if side != opposite:
            continue
        amount = _number(row.get('amount'))
        if amount is None or abs(abs(amount) - abs(source_amount)) > 0.01:
            continue
        if family and _document_family(row.get('document')) != family:
            continue
        distance = _date_distance_days(source.get('date'), row.get('date'))
        if distance is not None and distance > 120:
            continue
        candidates.append((distance if distance is not None else 10_000, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], str(item[1].get('row_id'))))
    return candidates[0][1]


def _downgrade_false_confirmed_missing(
    report: dict,
    row_index: dict[tuple[str, str], dict],
) -> None:
    for item in report.get('discrepancies') or []:
        if item.get('category') != 'confirmed_missing':
            continue
        sources = item.get('resolved_evidence') or []
        if not sources:
            continue
        mirrors = []
        for source in sources:
            mirror = _mirror_row(source, row_index)
            if mirror is None:
                mirrors = []
                break
            mirrors.append(mirror)
        if not mirrors:
            continue
        existing = {(row.get('side'), row.get('row_id')) for row in sources}
        for mirror in mirrors:
            key = (mirror.get('side'), mirror.get('row_id'))
            if key in existing:
                continue
            sources.append({
                'side': mirror['side'],
                'row_id': mirror['row_id'],
                'raw_row': mirror['raw_row'],
                'date': mirror.get('date'),
                'document': mirror.get('document'),
                'amount': mirror.get('amount'),
            })
            existing.add(key)
        item['category'] = 'likely_date_pair'
        item['influence'] = 0.0
        item['reason'] = 'Найдена зеркальная строка с той же суммой в другом документе.'
        item['resolved_evidence'] = sources
        item['clickable'] = True


def run_independent_expert_analysis(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    client: Any,
    model: str,
    settings: dict | None = None,
) -> dict:
    if client is None:
        return {'status': 'failed', 'error': 'api_key_required'}
    payload, row_index = build_expert_payload(df1, df2, settings)
    scope = payload['analysis_scope']
    try:
        message = client.messages.create(
            model=model,
            max_tokens=2800,
            temperature=0,
            system=EXPERT_SYSTEM_PROMPT,
            messages=[{
                'role': 'user',
                'content': json.dumps(
                    payload, ensure_ascii=False, separators=(',', ':'),
                ),
            }],
            output_config={
                'format': {
                    'type': 'json_schema',
                    'schema': CLAUDE_EXPERT_REPORT_SCHEMA,
                },
            },
        )
        usage = getattr(message, 'usage', None)
        usage_payload = {
            'input_tokens': int(getattr(usage, 'input_tokens', 0) or 0),
            'output_tokens': int(getattr(usage, 'output_tokens', 0) or 0),
        }
        stop_reason = getattr(message, 'stop_reason', None)
        if stop_reason in ('max_tokens', 'refusal'):
            return {
                'status': 'failed',
                'error': stop_reason,
                'usage': usage_payload,
            }
        text = ''.join(
            getattr(block, 'text', '')
            for block in getattr(message, 'content', [])
            if getattr(block, 'text', '')
        )
        report = json.loads(text)
        report['balances'] = payload['balance_comparison']
        report['totals'] = _derive_report_totals(report)
        if not _validate_report_shape(report):
            return {'status': 'failed', 'error': 'invalid_report_schema'}
        resolved, _ = resolve_expert_evidence(report, row_index)
        if scope['find_date_diff']:
            _downgrade_false_confirmed_missing(resolved['report'], row_index)
        _sort_report_discrepancies(resolved['report'])
        resolved['report']['totals'] = _derive_report_totals(resolved['report'])
        resolved['usage'] = usage_payload
        return resolved
    except Exception as exc:
        return {'status': 'failed', 'error': type(exc).__name__}
