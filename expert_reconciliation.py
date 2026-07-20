"""Expert reconciliation v2: deterministic matching + Claude verdicts.

Программа считает — Claude судит. Сервер сам сопоставляет строки по суммам,
считает влияния и окна дат; Claude получает только спорные места (пары с разными
датами и строки без пары) и выносит вердикты по выданным сервером идентификаторам.
"""

from __future__ import annotations

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

PAIR_VERDICTS = ('date_pair', 'unrelated', 'needs_review')
UNMATCHED_VERDICTS = ('missing', 'opening_balance_bridge', 'needs_review')
CROSS_MATCH_CATEGORIES = ('amount_difference', 'sign_difference', 'date_pair')

EXPERT_REVIEW_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'version', 'conclusion', 'confidence',
        'pair_verdicts', 'unmatched_verdicts', 'cross_matches',
    ],
    'properties': {
        'version': {'type': 'string', 'enum': ['2']},
        'conclusion': {'type': 'string', 'maxLength': 500},
        'confidence': {'type': 'string', 'enum': list(EXPERT_CONFIDENCE)},
        'pair_verdicts': {
            'type': 'array',
            'maxItems': 200,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['id', 'verdict', 'note'],
                'properties': {
                    'id': {'type': 'string', 'maxLength': 20},
                    'verdict': {'type': 'string', 'enum': list(PAIR_VERDICTS)},
                    'note': {'type': 'string', 'maxLength': 180},
                },
            },
        },
        'unmatched_verdicts': {
            'type': 'array',
            'maxItems': 200,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['id', 'verdict', 'note'],
                'properties': {
                    'id': {'type': 'string', 'maxLength': 20},
                    'verdict': {'type': 'string', 'enum': list(UNMATCHED_VERDICTS)},
                    'note': {'type': 'string', 'maxLength': 180},
                },
            },
        },
        'cross_matches': {
            'type': 'array',
            'maxItems': 100,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['doc1_id', 'doc2_id', 'category', 'note'],
                'properties': {
                    'doc1_id': {'type': 'string', 'maxLength': 20},
                    'doc2_id': {'type': 'string', 'maxLength': 20},
                    'category': {'type': 'string', 'enum': list(CROSS_MATCH_CATEGORIES)},
                    'note': {'type': 'string', 'maxLength': 180},
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


CLAUDE_EXPERT_REVIEW_SCHEMA = _claude_output_schema(EXPERT_REVIEW_SCHEMA)


EXPERT_SYSTEM_PROMPT = (
    'Ты бухгалтер-эксперт по актам сверки. Программа уже сопоставила строки двух актов '
    'по суммам и рассчитала сальдо (balance_comparison); числа не пересчитывай. '
    'Твоя задача — вынести суждения по спорным местам и дать краткий общий вывод. '
    'date_pairs — пары строк с равной суммой, но разными датами: verdict date_pair '
    '(одна и та же операция, отражена разными датами), unrelated (разные операции), '
    'needs_review (по данным не определить). '
    'unmatched — строки без пары: verdict missing (операции действительно нет во втором акте), '
    'opening_balance_bridge (операция закрывает разницу начального сальдо), needs_review. '
    'cross_matches — укажи, если две строки из unmatched (по одной с каждой стороны) '
    'на самом деле одна операция: category amount_difference (суммы близки, но различаются), '
    'sign_difference (зеркальный КСФ: модуль суммы тот же, перепутана сторона), '
    'date_pair (модули сумм равны). Такие строки не отмечай ещё и как missing. '
    'Используй только выданные id; влияния и итоги считает программа. '
    'В note и conclusion называй стороны только по display_name; не пиши doc1, doc2 или id. '
    'conclusion — общий вывод: сошлись ли сальдо и каков характер расхождений, '
    'без перечисления отдельных строк, до 450 знаков. Без Markdown. '
    'note — кратко и по-русски, до 160 знаков.'
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
    """Collect rows of both documents with stable ids plus balance context."""
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
        'version': '2',
        'analysis_scope': _normalize_expert_scope(settings),
        'balance_comparison': _balance_comparison(df1, df2),
        'documents': documents,
    }, row_index


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


def _pair_window_days(scope: dict, family: str) -> int:
    key = 'date_window_payment' if family == 'payment' else 'date_window_delivery'
    try:
        return max(0, int(scope.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def _row_influence(row: dict) -> float:
    """Вклад строки в разницу конечного сальдо (контрагент − организация).

    Формула одинакова для обеих сторон: кредит − дебет. Зеркально совпадающие
    операции (дебет у одной стороны, кредит у другой) в сумме дают ноль.
    """
    credit = _number(row.get('credit')) or 0.0
    debit = _number(row.get('debit')) or 0.0
    return round(credit - debit, 2)


def _match_rows(
    row_index: dict[tuple[str, str], dict],
) -> tuple[list[dict], list[dict]]:
    """Greedy 1:1 matching by absolute amount; prefers same document family,
    then minimal date distance. Returns (pairs, unmatched_rows)."""
    rows: dict[str, list[dict]] = {'doc1': [], 'doc2': []}
    for (side, _), row in row_index.items():
        if _number(row.get('amount')) is not None:
            rows[side].append(row)
    for side in rows:
        rows[side].sort(key=lambda row: (str(row.get('date') or ''), str(row['row_id'])))
    buckets: dict[float, list[dict]] = {}
    for row in rows['doc2']:
        key = round(abs(_number(row['amount']) or 0.0), 2)
        buckets.setdefault(key, []).append(row)
    pairs: list[dict] = []
    unmatched: list[dict] = []
    used_doc2: set[str] = set()
    for row1 in rows['doc1']:
        key = round(abs(_number(row1['amount']) or 0.0), 2)
        candidates = [
            row for row in buckets.get(key, [])
            if row['row_id'] not in used_doc2
        ]
        if not candidates:
            unmatched.append(row1)
            continue
        family1 = _document_family(row1.get('document'))

        def _rank(row2: dict) -> tuple:
            distance = _date_distance_days(row1.get('date'), row2.get('date'))
            return (
                0 if _document_family(row2.get('document')) == family1 else 1,
                distance if distance is not None else 10_000,
                str(row2['row_id']),
            )

        best = min(candidates, key=_rank)
        used_doc2.add(best['row_id'])
        pairs.append({
            'doc1': row1,
            'doc2': best,
            'days': _date_distance_days(row1.get('date'), best.get('date')),
        })
    for row2 in rows['doc2']:
        if row2['row_id'] not in used_doc2:
            unmatched.append(row2)
    return pairs, unmatched


def build_expert_review_state(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    settings: dict | None = None,
) -> dict:
    """Deterministic matching stage: everything the review request and the final
    report assembly need."""
    payload, row_index = build_expert_payload(df1, df2, settings)
    scope = payload['analysis_scope']
    pairs, unmatched = _match_rows(row_index)
    date_pairs = []
    exact_count = 0
    for pair in pairs:
        if pair['days'] and pair['days'] > 0 and scope['find_date_diff']:
            date_pairs.append(pair)
        elif pair['days'] and pair['days'] > 0:
            exact_count += 1  # поиск по датам выключен: пара считается совпавшей
        else:
            exact_count += 1
    min_amount = scope['min_amount']
    skipped_small = [
        row for row in unmatched
        if abs(_number(row.get('amount')) or 0.0) < min_amount
    ]
    unmatched = [
        row for row in unmatched
        if abs(_number(row.get('amount')) or 0.0) >= min_amount
    ]
    pair_items = {}
    for index, pair in enumerate(date_pairs, start=1):
        family = _document_family(pair['doc1'].get('document'))
        window = _pair_window_days(scope, family)
        pair_items[f'p{index}'] = {
            **pair,
            'id': f'p{index}',
            'family': family,
            'window': window,
            'within_window': bool(
                pair['days'] is not None and pair['days'] <= window
            ),
        }
    unmatched_items = {}
    for index, row in enumerate(unmatched, start=1):
        unmatched_items[f'u{index}'] = {**row, 'id': f'u{index}'}
    return {
        'payload': payload,
        'row_index': row_index,
        'scope': scope,
        'pair_items': pair_items,
        'unmatched_items': unmatched_items,
        'matched_row_count': 2 * (exact_count + len(date_pairs)),
        'skipped_small_count': len(skipped_small),
    }


def _compact_row(row: dict) -> dict:
    compact = {}
    if row.get('date'):
        compact['date'] = row['date']
    if row.get('document'):
        compact['document'] = row['document']
    if row.get('debit') is not None:
        compact['debit'] = row['debit']
    if row.get('credit') is not None:
        compact['credit'] = row['credit']
    return compact


def build_expert_review_request(state: dict) -> dict:
    """Payload for the single Claude call: only questionable items."""
    payload = state['payload']
    return {
        'version': '2',
        'analysis_scope': state['scope'],
        'balance_comparison': payload['balance_comparison'],
        'documents': [
            {
                'side': document['side'],
                'display_name': document['display_name'],
                **{
                    key: document[key]
                    for key in ('opening_balance', 'closing_balance', 'period')
                    if key in document
                },
            }
            for document in payload['documents']
        ],
        'matched_rows': state['matched_row_count'],
        'date_pairs': [
            {
                'id': item['id'],
                'amount': round(abs(_number(item['doc1'].get('amount')) or 0.0), 2),
                'days': item['days'],
                'window': item['window'],
                'within_window': item['within_window'],
                'doc1': _compact_row(item['doc1']),
                'doc2': _compact_row(item['doc2']),
            }
            for item in state['pair_items'].values()
        ],
        'unmatched': [
            {'id': item['id'], 'side': item['side'], **_compact_row(item)}
            for item in state['unmatched_items'].values()
        ],
    }


def _validate_review(review: Any) -> bool:
    if not isinstance(review, dict):
        return False
    if review.get('confidence') not in EXPERT_CONFIDENCE:
        return False
    if not isinstance(review.get('conclusion'), str):
        return False
    for key, allowed, id_keys in (
        ('pair_verdicts', PAIR_VERDICTS, ('id',)),
        ('unmatched_verdicts', UNMATCHED_VERDICTS, ('id',)),
        ('cross_matches', CROSS_MATCH_CATEGORIES, ('doc1_id', 'doc2_id')),
    ):
        items = review.get(key)
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                return False
            value = item.get('verdict') if key != 'cross_matches' else item.get('category')
            if value not in allowed:
                return False
            if not all(isinstance(item.get(id_key), str) for id_key in id_keys):
                return False
    return True


def _resolved_row(row: dict) -> dict:
    return {
        'side': row['side'],
        'row_id': row['row_id'],
        'raw_row': row['raw_row'],
        'date': row.get('date'),
        'document': row.get('document'),
        'amount': row.get('amount'),
    }


def _row_title(row: dict) -> str:
    parts = [part for part in (row.get('document'), row.get('date')) if part]
    return (' '.join(str(part) for part in parts) or 'Операция')[:120]


def _pair_title(pair: dict) -> str:
    left = _row_title(pair['doc1'])
    right = _row_title(pair['doc2'])
    return f'{left} ↔ {right}'[:160]


def _make_discrepancy(
    category: str,
    title: str,
    influence: float,
    reason: str,
    confidence: str,
    rows: list[dict],
) -> dict:
    return {
        'category': category,
        'title': title,
        'influence': round(influence, 2),
        'reason': (reason or '')[:200],
        'confidence': confidence if confidence in EXPERT_CONFIDENCE else 'medium',
        'evidence': [
            {'side': row['side'], 'row_id': row['row_id']} for row in rows
        ],
        'resolved_evidence': [_resolved_row(row) for row in rows],
        'clickable': bool(rows),
    }


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


def _category_enabled(category: str, scope: dict) -> bool:
    flags = {
        'confirmed_missing': 'find_missing',
        'amount_difference': 'find_amount_diff',
        'sign_difference': 'find_sign_mismatch',
        'likely_date_pair': 'find_date_diff',
    }
    flag = flags.get(category)
    return True if flag is None else bool(scope.get(flag))


def _assemble_expert_report(state: dict, review: dict) -> dict:
    scope = state['scope']
    payload = state['payload']
    guard_log: list[str] = []
    pair_verdicts = {}
    for item in review.get('pair_verdicts') or []:
        if item['id'] in state['pair_items']:
            pair_verdicts[item['id']] = item
        else:
            guard_log.append(f"unknown_pair_id:{item['id']}")
    unmatched_verdicts = {}
    for item in review.get('unmatched_verdicts') or []:
        if item['id'] in state['unmatched_items']:
            unmatched_verdicts[item['id']] = item
        else:
            guard_log.append(f"unknown_unmatched_id:{item['id']}")

    discrepancies: list[dict] = []
    consumed_unmatched: set[str] = set()

    # 1. Cross-matches: две несопоставленные строки — одна операция.
    for match in review.get('cross_matches') or []:
        doc1_item = state['unmatched_items'].get(match['doc1_id'])
        doc2_item = state['unmatched_items'].get(match['doc2_id'])
        if (
            doc1_item is None or doc2_item is None
            or doc1_item['side'] != 'doc1' or doc2_item['side'] != 'doc2'
            or match['doc1_id'] in consumed_unmatched
            or match['doc2_id'] in consumed_unmatched
        ):
            guard_log.append(
                f"cross_match_ignored:{match.get('doc1_id')}+{match.get('doc2_id')}"
            )
            continue
        category = match['category']
        amount1 = abs(_number(doc1_item.get('amount')) or 0.0)
        amount2 = abs(_number(doc2_item.get('amount')) or 0.0)
        if category == 'date_pair':
            if abs(amount1 - amount2) > 0.01:
                guard_log.append(
                    f"cross_match_ignored:{match['doc1_id']}+{match['doc2_id']}"
                )
                continue
            days = _date_distance_days(doc1_item.get('date'), doc2_item.get('date'))
            window = _pair_window_days(
                scope, _document_family(doc1_item.get('document')),
            )
            if days is not None and days > window:
                category_final = 'ambiguous'
                reason = (
                    f'Суммы равны, но разница дат {days} дн. '
                    f'превышает допуск {window} дн.'
                )
            else:
                category_final = 'likely_date_pair'
                reason = match.get('note') or (
                    f'Одна операция, отражена разными датами '
                    f'(разница {days if days is not None else "?"} дн., допуск {window} дн.).'
                )
            influence = 0.0
        else:
            category_final = category
            influence = _row_influence(doc1_item) + _row_influence(doc2_item)
            reason = match.get('note') or 'Стороны отразили одну операцию по-разному.'
        consumed_unmatched.add(match['doc1_id'])
        consumed_unmatched.add(match['doc2_id'])
        if not _category_enabled(category_final, scope):
            continue
        discrepancies.append(_make_discrepancy(
            category_final,
            _pair_title({'doc1': doc1_item, 'doc2': doc2_item}),
            influence,
            reason,
            'high',
            [doc1_item, doc2_item],
        ))

    # 2. Пары с разными датами.
    for pair_id, item in state['pair_items'].items():
        verdict = (pair_verdicts.get(pair_id) or {}).get('verdict')
        note = (pair_verdicts.get(pair_id) or {}).get('note') or ''
        days = item['days']
        window = item['window']
        if verdict == 'unrelated':
            for row in (item['doc1'], item['doc2']):
                if not _category_enabled('confirmed_missing', scope):
                    continue
                discrepancies.append(_make_discrepancy(
                    'confirmed_missing',
                    _row_title(row),
                    _row_influence(row),
                    note or 'Суммы совпали случайно: это разные операции.',
                    'medium',
                    [row],
                ))
            continue
        if not item['within_window']:
            category = 'ambiguous'
            reason = (
                f'Суммы равны, но разница дат {days} дн. '
                f'превышает допуск {window} дн.'
            )
            confidence = 'medium'
        elif verdict == 'needs_review':
            category = 'ambiguous'
            reason = note or 'Требуется проверка пары по первичным документам.'
            confidence = 'low'
        else:
            category = 'likely_date_pair'
            reason = note or (
                f'Одна операция, отражена разными датами '
                f'(разница {days} дн., допуск {window} дн.).'
            )
            confidence = 'high' if verdict == 'date_pair' else 'medium'
        if not _category_enabled(category, scope):
            continue
        discrepancies.append(_make_discrepancy(
            category, _pair_title(item), 0.0, reason, confidence,
            [item['doc1'], item['doc2']],
        ))

    # 3. Несопоставленные строки.
    for item_id, row in state['unmatched_items'].items():
        if item_id in consumed_unmatched:
            continue
        verdict_item = unmatched_verdicts.get(item_id) or {}
        verdict = verdict_item.get('verdict')
        note = verdict_item.get('note') or ''
        if verdict == 'opening_balance_bridge':
            category = 'opening_balance_bridge'
            reason = note or 'Операция закрывает разницу начального сальдо.'
            confidence = 'high'
        elif verdict == 'needs_review':
            category = 'ambiguous'
            reason = note or 'Строку не удалось однозначно классифицировать.'
            confidence = 'low'
        else:
            category = 'confirmed_missing'
            reason = note or 'Операция отражена только в одном акте.'
            confidence = 'high' if verdict == 'missing' else 'medium'
        if not _category_enabled(category, scope):
            continue
        discrepancies.append(_make_discrepancy(
            category, _row_title(row), _row_influence(row), reason, confidence,
            [row],
        ))

    report = {
        'version': '2',
        'conclusion': (review.get('conclusion') or '')[:500],
        'confidence': review.get('confidence') or 'medium',
        'balances': payload['balance_comparison'],
        'discrepancies': discrepancies,
        'guard_log': guard_log,
    }
    _sort_report_discrepancies(report)
    report['totals'] = _derive_report_totals(report)

    referenced = {
        (row['side'], row['row_id'])
        for item in discrepancies
        for row in item['resolved_evidence']
    }
    report['completeness'] = {
        'rows_total': len(state['row_index']),
        'rows_matched': state['matched_row_count'],
        'rows_in_findings': len(referenced),
    }

    balances = payload['balance_comparison']
    opening = balances.get('opening_difference')
    closing = balances.get('closing_difference')
    if opening is not None and closing is not None:
        influence_sum = sum(
            _number(item.get('influence')) or 0.0 for item in discrepancies
        )
        if abs(round(opening + influence_sum - closing, 2)) > 0.01:
            guard_log.append(
                'balance_mismatch:'
                f'{round(opening + influence_sum - closing, 2)}'
            )
    return report


def _auto_conclusion(state: dict) -> dict:
    balances = state['payload']['balance_comparison']
    closing = balances.get('closing_difference')
    if closing is not None and abs(closing) <= 0.01:
        text = (
            'Все операции сопоставлены по суммам, конечные сальдо сторон совпадают. '
            'Спорных строк не найдено.'
        )
    else:
        text = (
            'Все операции сопоставлены по суммам; спорных строк не найдено. '
            'Разница сальдо объясняется начальным сальдо и составом операций.'
        )
    return {
        'version': '2',
        'conclusion': text,
        'confidence': 'high',
        'pair_verdicts': [],
        'unmatched_verdicts': [],
        'cross_matches': [],
    }


def run_independent_expert_analysis(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    client: Any,
    model: str,
    settings: dict | None = None,
) -> dict:
    if client is None:
        return {'status': 'failed', 'error': 'api_key_required'}
    state = build_expert_review_state(df1, df2, settings)
    usage_payload = {'input_tokens': 0, 'output_tokens': 0}
    try:
        if not state['pair_items'] and not state['unmatched_items']:
            review = _auto_conclusion(state)
        else:
            message = client.messages.create(
                model=model,
                max_tokens=4000,
                temperature=0,
                system=EXPERT_SYSTEM_PROMPT,
                messages=[{
                    'role': 'user',
                    'content': json.dumps(
                        build_expert_review_request(state),
                        ensure_ascii=False, separators=(',', ':'),
                    ),
                }],
                output_config={
                    'format': {
                        'type': 'json_schema',
                        'schema': CLAUDE_EXPERT_REVIEW_SCHEMA,
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
            review = json.loads(text)
            if not _validate_review(review):
                return {'status': 'failed', 'error': 'invalid_report_schema'}
        report = _assemble_expert_report(state, review)
        return {
            'status': 'complete',
            'report': report,
            'warnings': [],
            'usage': usage_payload,
        }
    except Exception as exc:
        return {'status': 'failed', 'error': type(exc).__name__}
