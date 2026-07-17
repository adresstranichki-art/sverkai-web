"""Independent Claude reconciliation contract without standard-matcher conclusions."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

import pandas as pd


EXPERT_CATEGORIES = (
    'confirmed_missing',
    'likely_date_pair',
    'opening_balance_bridge',
    'amount_difference',
    'sign_difference',
    'ambiguous',
)
EXPERT_CONFIDENCE = ('high', 'medium', 'low')


def _nullable(kind: str) -> dict:
    return {'type': [kind, 'null']}


EXPERT_REPORT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'version', 'conclusion', 'confidence', 'balances', 'totals',
        'discrepancies', 'actions', 'limitations',
    ],
    'properties': {
        'version': {'type': 'string', 'enum': ['1']},
        'conclusion': {'type': 'string', 'maxLength': 400},
        'confidence': {'type': 'string', 'enum': list(EXPERT_CONFIDENCE)},
        'balances': {
            'type': 'object',
            'additionalProperties': False,
            'required': [
                'opening_difference', 'period_movement',
                'closing_difference', 'formula',
            ],
            'properties': {
                'opening_difference': _nullable('number'),
                'period_movement': _nullable('number'),
                'closing_difference': _nullable('number'),
                'formula': {'type': 'string', 'maxLength': 160},
            },
        },
        'totals': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['confirmed_count', 'confirmed_amount', 'review_count'],
            'properties': {
                'confirmed_count': {'type': 'integer', 'minimum': 0},
                'confirmed_amount': {'type': 'number'},
                'review_count': {'type': 'integer', 'minimum': 0},
            },
        },
        'discrepancies': {
            'type': 'array',
            'maxItems': 100,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'category', 'title', 'doc1_date', 'doc2_date',
                    'doc1_document', 'doc2_document', 'doc1_value',
                    'doc2_value', 'influence', 'reason', 'action',
                    'confidence', 'evidence',
                ],
                'properties': {
                    'category': {'type': 'string', 'enum': list(EXPERT_CATEGORIES)},
                    'title': {'type': 'string', 'maxLength': 120},
                    'doc1_date': _nullable('string'),
                    'doc2_date': _nullable('string'),
                    'doc1_document': _nullable('string'),
                    'doc2_document': _nullable('string'),
                    'doc1_value': _nullable('number'),
                    'doc2_value': _nullable('number'),
                    'influence': {'type': 'number'},
                    'reason': {'type': 'string', 'maxLength': 200},
                    'action': {'type': 'string', 'maxLength': 160},
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
        'actions': {
            'type': 'array',
            'maxItems': 5,
            'items': {'type': 'string', 'maxLength': 160},
        },
        'limitations': {
            'type': 'array',
            'maxItems': 3,
            'items': {'type': 'string', 'maxLength': 160},
        },
    },
}


EXPERT_SYSTEM_PROMPT = (
    'Ты бухгалтер-эксперт. Независимо сверь два акта только по переданным строкам. '
    'Учитывай сторону дебет/кредит начального и конечного сальдо и проверь формулу: '
    'начальная разница + движение периода = конечная разница. Одинаковая операция с '
    'другой датой — likely_date_pair; операция на сумму входящей разницы — '
    'opening_balance_bridge; confirmed_missing ставь только при высокой уверенности. '
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


def build_expert_payload(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
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
        document_payload = {
            'side': side,
            'name': _document_text(df.attrs.get('source_name'), 120),
            'rows': rows,
            **_balance_payload(df, prefix),
        }
        if not document_payload['name']:
            document_payload.pop('name')
        documents.append(document_payload)
    return {'version': '1', 'documents': documents}, row_index


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
        'discrepancies', 'actions', 'limitations',
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


def run_independent_expert_analysis(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    client: Any,
    model: str,
) -> dict:
    if client is None:
        return {'status': 'failed', 'error': 'api_key_required'}
    payload, row_index = build_expert_payload(df1, df2)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=2200,
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
                    'schema': EXPERT_REPORT_SCHEMA,
                },
            },
        )
        text = ''.join(
            getattr(block, 'text', '')
            for block in getattr(message, 'content', [])
            if getattr(block, 'text', '')
        )
        report = json.loads(text)
        if not _validate_report_shape(report):
            return {'status': 'failed', 'error': 'invalid_report_schema'}
        resolved, _ = resolve_expert_evidence(report, row_index)
        usage = getattr(message, 'usage', None)
        resolved['usage'] = {
            'input_tokens': int(getattr(usage, 'input_tokens', 0) or 0),
            'output_tokens': int(getattr(usage, 'output_tokens', 0) or 0),
        }
        return resolved
    except Exception as exc:
        return {'status': 'failed', 'error': type(exc).__name__}
