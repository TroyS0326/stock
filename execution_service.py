from __future__ import annotations

import config
from broker_facade import place_managed_entry_order
from db import count_trades_today, estimated_daily_loss_risk_used_today, get_failed_trades_today, get_trade_by_symbol_today, insert_trade
from execution import get_runtime_trade_blocks
from scanner import buy_window_open, within_auto_scan_window, within_morning_scan_window

TRIGGER_MAP = {
    'ORB_BREAKOUT': lambda d: bool((d.get('opening_range_confirmation') or {}).get('breakout_confirmed')),
    'VWAP_RECLAIM': lambda d: bool((d.get('vwap_hold_reclaim') or {}).get('reclaimed_vwap')),
    'VWAP_PULLBACK_BOUNCE': lambda d: bool((d.get('vwap_hold_reclaim') or {}).get('held_vwap')),
    'MOMENTUM_CONTINUATION': lambda d: bool(d.get('momentum_continuation', False)),
}
GRADE_ORDER = {'NO TRADE': 0, 'WATCH': 1, 'A': 2, 'A+': 3}

HARD_AUTO_BLOCKERS = {
    'auto_trade_disabled',
    'operator_auto_trade_paused',
    'emergency_stop_active',
    'outside_auto_scan_window',
    'failed_trade_lockout',
    'daily_loss_limit_reached',
    'max_auto_trades_reached',
    'duplicate_symbol_trade_blocked',
    'hard_reject_reasons_present',
    'qty_zero',
    'zero_qty_risk',
    'invalid_entry_price',
    'invalid_stop_price',
    'invalid_current_price',
    'invalid_buy_upper',
    'invalid_targets',
    'invalid_risk',
    'oversized_risk',
    'wide_spread',
    'buy_window_closed',
}


def effective_probe_hard_blockers(skip_reasons: list[str], candidate: dict, probe_payload: dict) -> set[str]:
    blockers = {r for r in (skip_reasons or []) if r in HARD_AUTO_BLOCKERS or str(r).startswith('hard_reject_reason_')}
    if 'invalid_risk' in blockers:
        return blockers
    qty = int(probe_payload.get('qty') or 0)
    probe_risk = float(probe_payload.get('risk_dollars') or 0.0)
    probe_qty_valid = qty >= 1 and qty <= int(config.PROBE_MAX_QTY)
    probe_risk_valid = probe_risk > 0 and probe_risk <= (config.PROBE_MAX_DOLLAR_RISK + 0.01)

    if 'oversized_risk' in blockers:
        qty = int(probe_payload.get('qty') or 0)
        entry = float(candidate.get('entry_price', 0) or 0)
        stop = float(candidate.get('stop_price', 0) or 0)
        probe_risk = max(0.0, (entry - stop) * qty)
        if qty >= 1 and qty <= int(config.PROBE_MAX_QTY) and probe_risk > 0 and probe_risk <= (config.PROBE_MAX_DOLLAR_RISK + 0.01):
            blockers.discard('oversized_risk')
    if 'wide_spread' in blockers:
        spread = float(((candidate.get('details') or {}).get('spread_pct', 0)) or 0)
        if spread <= config.PROBE_MAX_SPREAD_PCT:
            blockers.discard('wide_spread')
    if probe_qty_valid and probe_risk_valid:
        blockers.discard('qty_zero')
        blockers.discard('zero_qty_risk')
    return blockers


def trade_risk_limit() -> float:
    pct_limit = config.CURRENT_BANKROLL * config.MAX_TRADE_RISK_PCT
    return max(config.MAX_DOLLAR_LOSS_PER_TRADE, pct_limit) if config.ACTIVE_PAPER_TRADING_MODE else min(config.MAX_DOLLAR_LOSS_PER_TRADE, pct_limit)

def validate_price_risk_fields(candidate) -> tuple[bool, list[str], float]:
    skip = []
    qty = int(candidate.get('qty', 0) or 0)
    entry = float(candidate.get('entry_price', 0) or 0)
    stop = float(candidate.get('stop_price', 0) or 0)
    current = float(candidate.get('current_price', 0) or 0)
    buy_upper = float(candidate.get('buy_upper', 0) or 0)
    t1_raw = candidate.get('target_1')
    t2_raw = candidate.get('target_2')
    t1 = float(t1_raw or 0)
    t2 = float(t2_raw or 0)
    if qty < 1: skip.append('qty_zero')
    if entry <= 0: skip.append('invalid_entry_price')
    if stop <= 0: skip.append('invalid_stop_price')
    if current <= 0: skip.append('invalid_current_price')
    if buy_upper <= 0: skip.append('invalid_buy_upper')
    if (t1_raw is not None or t2_raw is not None) and (t1 <= entry or t2 < t1):
        skip.append('invalid_targets')
    risk_per_share = entry - stop
    risk = risk_per_share * max(0, qty)
    if stop >= entry:
        skip.append('invalid_risk')
    elif qty < 1 and risk_per_share > 0:
        skip.append('zero_qty_risk')
    elif risk_per_share <= 0:
        skip.append('invalid_risk')
    if risk > trade_risk_limit() + 0.01: skip.append('oversized_risk')
    return (not skip, skip, risk)

def detect_entry_trigger(candidate):
    details = candidate.get('details') or {}
    scanner_trigger = (details.get('entry_trigger') or '').upper().strip()
    if scanner_trigger in {'ORB_BREAKOUT', 'VWAP_RECLAIM', 'VWAP_PULLBACK_BOUNCE', 'MOMENTUM_CONTINUATION', 'NO_TRIGGER'}:
        return scanner_trigger
    for name, fn in TRIGGER_MAP.items():
        if fn(details):
            return name
    return 'NO_TRIGGER'

def candidate_hard_reject_reasons(candidate) -> list[str]:
    details = candidate.get('details') or {}
    reasons = []
    for key in ('hard_reject_reasons',):
        val = candidate.get(key) or details.get(key) or []
        if isinstance(val, list):
            reasons.extend([str(x) for x in val])
    why_not = details.get('why_not_buying') or candidate.get('why_not_buying') or []
    if isinstance(why_not, list):
        reasons.extend([str(x) for x in why_not if 'hard_gatekeeper' in str(x) or 'reject' in str(x)])
    return sorted(set([r for r in reasons if r]))


def classify_hard_reject_reasons(candidate) -> tuple[list[str], list[str]]:
    raw_reasons = candidate_hard_reject_reasons(candidate)
    true_hard_reject_tokens = {
        'dilution_blacklist',
        'price_out_of_range',
        'below_min_price',
        'above_max_price',
        'insufficient_liquidity',
        'low_daily_dollar_volume',
        'halted',
        'not_tradable',
        'no_quote',
        'data_unavailable',
        'market_data_unavailable',
        'hard_gatekeeper_failed',
        'outside_scan_price_range',
        'hard_gatekeeper',
        'heavy_red_candle_trap',
        'market_internals_block',
        'vix_circuit_breaker',
    }
    overridable_tokens = {
        'missing_catalyst',
        'low_premarket_dollar_volume',
        'weak_premarket_volume',
        'no_premarket_gap',
        'sector_sympathy_too_low',
        'setup_grade_not_allowed',
        'setup_grade_below_min_auto_grade',
        'auto_decision_not_actionable',
        'no_valid_entry_trigger',
        'price_extended',
        'extended_above_buy_zone',
        'qty_zero',
        'zero_qty_risk',
        'risk_qty_below_1',
        'oversized_risk',
        'fallback_score_too_low',
        'fallback_no_momentum_signal',
        'fallback_entry_too_extended',
        'score_too_low',
    }
    true_hard_rejects, overridable_rejects = [], []
    for reason in raw_reasons:
        normalized = ''.join(ch.lower() if ch.isalnum() else '_' for ch in str(reason)).strip('_')
        normalized = '_'.join([p for p in normalized.split('_') if p])
        if not normalized:
            continue
        if normalized == 'spread_too_wide':
            spread_pct = float((candidate.get('details') or {}).get('spread_pct', 0) or 0)
            if spread_pct > config.PROBE_MAX_SPREAD_PCT:
                true_hard_rejects.append(f'hard_reject_reason_{normalized}')
            else:
                overridable_rejects.append(normalized)
            continue
        if normalized in {'price_extended', 'extended_above_buy_zone', 'fallback_entry_too_extended'}:
            current = float(candidate.get('current_price', 0) or 0)
            entry = float(candidate.get('entry_price', 0) or 0)
            max_ext = float(config.PROBE_MAX_ENTRY_EXTENSION_PCT)
            if entry > 0 and current > 0:
                if (current / entry) <= (1 + max_ext):
                    overridable_rejects.append(normalized)
                else:
                    true_hard_rejects.append(f'hard_reject_reason_{normalized}')
            else:
                true_hard_rejects.append(f'hard_reject_reason_{normalized}')
            continue
        if normalized in overridable_tokens:
            overridable_rejects.append(normalized)
            continue
        if (normalized in true_hard_reject_tokens) or normalized.startswith('hard_gatekeeper'):
            true_hard_rejects.append(f'hard_reject_reason_{normalized}')
            continue
        true_hard_rejects.append(f'hard_reject_reason_{normalized}')
    return sorted(set(true_hard_rejects)), sorted(set(overridable_rejects))

def fallback_entry_ok(candidate) -> tuple[bool, list[str]]:
    reasons = []
    details = candidate.get('details') or {}
    spread = float(details.get('spread_pct', 0) or 0)
    score = int(candidate.get('score_total', 0) or 0)
    entry = float(candidate.get('entry_price', 0) or 0)
    stop = float(candidate.get('stop_price', 0) or 0)
    buy_upper = float(candidate.get('buy_upper', 0) or 0)
    decision = (candidate.get('decision') or '').upper()
    momentum = bool(details.get('momentum_continuation')) or bool((details.get('vwap_hold_reclaim') or {}).get('reclaimed_vwap')) or bool((details.get('vwap_hold_reclaim') or {}).get('held_vwap')) or int(((details.get('opening_range_confirmation') or {}).get('bars_above_breakout') or 0)) >= 1 or ('BUY' in decision or 'BREAKOUT' in decision)
    if score < config.MIN_MOMENTUM_SCORE_TO_AUTOTRADE: reasons.append('fallback_score_too_low')
    if spread > config.FALLBACK_ENTRY_MAX_SPREAD_PCT: reasons.append('fallback_wide_spread')
    if entry <= 0 or stop <= 0 or stop >= entry: reasons.append('fallback_invalid_stop_or_entry')
    if entry > buy_upper * (1 + config.MAX_ENTRY_EXTENSION_PCT): reasons.append('fallback_entry_too_extended')
    if not momentum: reasons.append('fallback_no_momentum_signal')
    return (not reasons, reasons)


def probe_trade_ok(candidate, skip_reasons: list[str]) -> tuple[bool, list[str], dict]:
    reasons = []
    details = candidate.get('details') or {}
    score = int(candidate.get('score_total', 0) or 0)
    spread = float(details.get('spread_pct', 0) or 0)
    entry = float(candidate.get('entry_price', 0) or 0)
    current = float(candidate.get('current_price', 0) or 0)
    stop = float(candidate.get('stop_price', 0) or 0)
    buy_upper = float(candidate.get('buy_upper', 0) or 0)
    target_1 = float(candidate.get('target_1', 0) or 0)
    target_2 = float(candidate.get('target_2', 0) or 0)
    original_qty = int(candidate.get('qty', 0) or 0)
    probe_max_qty = int(config.PROBE_MAX_QTY)
    probe_qty_from_zero = False
    if probe_max_qty >= 1:
        if original_qty >= 1:
            qty = min(original_qty, probe_max_qty)
        else:
            qty = min(1, probe_max_qty)
            probe_qty_from_zero = True
    else:
        qty = 0

    if not config.ACTIVE_PAPER_TRADING_MODE: reasons.append('probe_requires_active_paper_mode')
    if not config.AGGRESSIVE_DAY_FLIPPER_MODE: reasons.append('aggressive_mode_disabled')
    if not config.PAPER_PROBE_TRADES_ENABLED: reasons.append('paper_probe_disabled')
    if not (bool(config.PAPER_TRADING_DETECTED) or bool(config.SIMULATION_MODE)): reasons.append('probe_not_paper_or_simulation')
    if score < config.PROBE_MIN_SCORE: reasons.append('probe_score_too_low')
    if spread > config.PROBE_MAX_SPREAD_PCT: reasons.append('probe_spread_too_wide')
    if entry <= 0 or current <= 0 or stop <= 0 or stop >= entry: reasons.append('probe_invalid_price_fields')
    if target_1 <= entry or target_2 < target_1: reasons.append('probe_invalid_targets')
    if buy_upper > 0 and current > buy_upper * (1 + config.PROBE_MAX_ENTRY_EXTENSION_PCT): reasons.append('probe_entry_too_extended')
    if buy_upper > 0 and entry > buy_upper * (1 + config.PROBE_MAX_ENTRY_EXTENSION_PCT): reasons.append('probe_entry_too_extended')
    if qty < 1: reasons.append('probe_invalid_qty')

    risk_dollars = max(0.0, (entry - stop) * qty)
    if risk_dollars <= 0: reasons.append('probe_invalid_risk')
    if risk_dollars > config.PROBE_MAX_DOLLAR_RISK + 0.01: reasons.append('probe_risk_too_high')
    original_hard_blockers = {r for r in (skip_reasons or []) if r in HARD_AUTO_BLOCKERS}
    hard_blockers = effective_probe_hard_blockers(skip_reasons, candidate, {'qty': qty, 'risk_dollars': risk_dollars, 'probe_qty_from_zero': probe_qty_from_zero})
    overridden_hard_blockers = sorted(original_hard_blockers - hard_blockers)
    probe_overridable_hard = {'oversized_risk', 'wide_spread', 'qty_zero', 'zero_qty_risk'}
    overridable_hard_blockers = sorted([r for r in overridden_hard_blockers if r in probe_overridable_hard])
    if hard_blockers:
        reasons.append('probe_hard_blockers_present')

    soft_only = [r for r in (skip_reasons or []) if r not in HARD_AUTO_BLOCKERS]
    technical_override_reasons = {'no_valid_entry_trigger', 'auto_decision_not_actionable', 'setup_grade_not_allowed'}
    if any(r in (skip_reasons or []) for r in technical_override_reasons):
        if score < config.PROBE_MIN_SCORE or entry <= 0 or current <= 0 or stop <= 0 or stop >= entry:
            reasons.append('probe_no_trigger_unsafe_context')
        if target_1 <= entry or target_2 < target_1:
            reasons.append('probe_no_trigger_unsafe_context')
        if spread > config.PROBE_MAX_SPREAD_PCT:
            reasons.append('probe_no_trigger_unsafe_context')
        if risk_dollars <= 0 or risk_dollars > config.PROBE_MAX_DOLLAR_RISK + 0.01:
            reasons.append('probe_no_trigger_unsafe_context')
        if buy_upper > 0 and (current > buy_upper * (1 + config.PROBE_MAX_ENTRY_EXTENSION_PCT) or entry > buy_upper * (1 + config.PROBE_MAX_ENTRY_EXTENSION_PCT)):
            reasons.append('probe_no_trigger_unsafe_context')

    if not soft_only and not overridable_hard_blockers:
        reasons.append('no_soft_gate_blockers_to_override')

    probe_payload = {
        'qty': qty,
        'risk_dollars': round(risk_dollars, 2),
        'probe_qty_from_zero': probe_qty_from_zero,
        'soft_blockers_overridden': sorted(set(soft_only)),
        'hard_blockers_overridden': overridable_hard_blockers,
        'effective_hard_blockers': sorted(hard_blockers),
    }
    ok = not reasons
    success_reasons = ['aggressive_paper_probe'] if ok else []
    if ok and any(r in (skip_reasons or []) for r in technical_override_reasons):
        success_reasons.append('probe_override_no_trigger_safe_context')
    out_reasons = sorted(set(reasons + success_reasons))
    return (ok, out_reasons, probe_payload)

def validate_trade_candidate(candidate, auto=False):
    skip = []
    decision = (candidate.get('decision') or '').upper()
    if auto and not config.AUTO_TRADE_ENABLED: skip.append('auto_trade_disabled')
    if auto: skip.extend(get_runtime_trade_blocks())
    if auto and not within_auto_scan_window(): skip.append('outside_auto_scan_window')
    if auto and estimated_daily_loss_risk_used_today() >= (config.CURRENT_BANKROLL * config.MAX_DAILY_REALIZED_LOSS_PCT):
        skip.append('daily_loss_limit_reached')
    if get_failed_trades_today() >= config.MAX_FAILED_TRADES_PER_DAY: skip.append('failed_trade_lockout')
    details = candidate.get('details') or {}
    spread = float(details.get('spread_pct', 0) or 0)
    valid_risk, risk_reasons, risk = validate_price_risk_fields(candidate)
    if not valid_risk:
        skip.extend(risk_reasons)
    trigger = detect_entry_trigger(candidate)
    true_hard_rejects, overridable_rejects = classify_hard_reject_reasons(candidate)
    if true_hard_rejects:
        skip.extend(true_hard_rejects or ['hard_reject_reasons_present'])
    skip.extend([f'overridable_reject_{r}' for r in overridable_rejects])
    symbol = candidate.get('symbol')
    if auto and count_trades_today(source='auto') >= config.MAX_AUTO_TRADES_PER_DAY: skip.append('max_auto_trades_reached')
    if symbol and (not config.ALLOW_DUPLICATE_SYMBOL_TRADES_PER_DAY) and get_trade_by_symbol_today(symbol): skip.append('duplicate_symbol_trade_blocked')

    fallback_used = False
    fallback_reasons = []
    if auto and config.ACTIVE_PAPER_TRADING_MODE:
        setup_grade = (candidate.get('setup_grade') or '').upper()
        min_grade = (config.MIN_AUTO_SETUP_GRADE or 'WATCH').upper()
        if GRADE_ORDER.get(setup_grade, -1) < GRADE_ORDER.get(min_grade, 1):
            skip.append('setup_grade_below_min_auto_grade')
        allow_watch = config.ALLOW_WATCH_GRADE_AUTO_TRADES and setup_grade == 'WATCH'
        if spread > config.FALLBACK_ENTRY_MAX_SPREAD_PCT: skip.append('wide_spread')
        fallback_ok, fallback_reasons = fallback_entry_ok(candidate)
        if allow_watch and config.FALLBACK_ENTRY_ENABLED and fallback_ok:
            fallback_used = True
        else:
            if trigger == 'NO_TRIGGER':
                skip.append('no_valid_entry_trigger')
            if setup_grade not in {'A', 'A+'}:
                skip.append('setup_grade_not_allowed')
            if decision != 'BUY NOW':
                skip.append('auto_decision_not_actionable')
            if allow_watch and fallback_reasons:
                skip.extend(fallback_reasons)
    else:
        if candidate.get('setup_grade') not in {'A', 'A+'}: skip.append('setup_grade_not_allowed')
        if int(candidate.get('score_total', 0)) < config.MIN_SCORE_TO_EXECUTE: skip.append('score_too_low')
        catalyst = int((candidate.get('scores') or {}).get('catalyst', 0))
        if catalyst < config.MIN_CATALYST_SCORE: skip.append('catalyst_too_low')
        if spread > config.MAX_SPREAD_PCT: skip.append('wide_spread')
        if trigger == 'NO_TRIGGER': skip.append('no_valid_entry_trigger')
        if auto and decision != 'BUY NOW': skip.append('auto_decision_not_actionable')

    if float(candidate.get('current_price', 0)) > float(candidate.get('buy_upper', 0)) * (1 + config.MAX_ENTRY_EXTENSION_PCT): skip.append('price_extended')
    if not buy_window_open(): skip.append('buy_window_closed')
    if not auto and decision == 'WAIT': skip.append('manual_wait_decision')
    skip = sorted(set(skip))
    probe_ok, probe_reasons, probe_payload = (False, [], {})
    if auto:
        probe_ok, probe_reasons, probe_payload = probe_trade_ok(candidate, skip)
    hard_blocked = bool(effective_probe_hard_blockers(skip, candidate, probe_payload))
    ok = (not skip) or (auto and (not hard_blocked) and probe_ok)
    if auto and skip and probe_ok and not hard_blocked:
        original_qty = int(candidate.get('qty', 0) or 0)
        probe_qty = int(probe_payload.get('qty') or 0)
        probe_risk = float(probe_payload.get('risk_dollars') or 0.0)
        soft_blockers_overridden = probe_payload.get('soft_blockers_overridden', [])
        hard_blockers_overridden = probe_payload.get('hard_blockers_overridden', [])
        candidate['probe_trade'] = True
        candidate['probe_reason'] = 'aggressive_paper_probe'
        candidate['original_qty'] = original_qty
        candidate['qty'] = probe_qty
        candidate['probe_risk_dollars'] = probe_risk
        candidate['probe_qty_from_zero'] = bool(probe_payload.get('probe_qty_from_zero'))
        candidate['soft_blockers_overridden'] = soft_blockers_overridden
        candidate['hard_blockers_overridden'] = hard_blockers_overridden
        return {
            'ok': True,
            'entry_trigger': trigger,
            'skip_reasons': [],
            'fallback_used': True,
            'fallback_reasons': fallback_reasons,
            'probe_trade': True,
            'probe_trade_ok': True,
            'probe_reasons': ['aggressive_paper_probe'],
            'probe_qty': probe_qty,
            'probe_risk_dollars': probe_risk,
            'probe_qty_from_zero': bool(probe_payload.get('probe_qty_from_zero')),
            'soft_blockers_overridden': soft_blockers_overridden,
            'hard_blockers_overridden': hard_blockers_overridden,
            'risk_dollars': round(probe_risk, 2),
        }
    return {
        'ok': ok,
        'entry_trigger': trigger,
        'skip_reasons': skip,
        'fallback_used': fallback_used,
        'fallback_reasons': fallback_reasons,
        'risk_dollars': round(risk, 2),
        'probe_trade': bool(ok and skip and probe_ok),
        'probe_trade_ok': probe_ok,
        'probe_reasons': probe_reasons,
        'probe_qty': probe_payload.get('qty'),
        'probe_risk_dollars': probe_payload.get('risk_dollars'),
        'probe_qty_from_zero': bool(probe_payload.get('probe_qty_from_zero')),
        'soft_blockers_overridden': probe_payload.get('soft_blockers_overridden', []),
        'hard_blockers_overridden': probe_payload.get('hard_blockers_overridden', []),
    }


def execute_trade_candidate(candidate, source='manual'):
    qty = int(candidate.get('qty') or 0)
    if candidate.get('probe_trade'):
        qty = min(int(candidate.get('qty') or 0), int(config.PROBE_MAX_QTY))
        if qty < 1:
            raise ValueError('probe_qty_invalid')
        probe_risk = (float(candidate.get('entry_price') or 0) - float(candidate.get('stop_price') or 0)) * qty
        if probe_risk > config.PROBE_MAX_DOLLAR_RISK + 0.01:
            raise ValueError('probe_risk_too_high')
    order = place_managed_entry_order(symbol=candidate['symbol'], qty=qty, entry_price=float(candidate['entry_price']), stop_price=float(candidate['stop_price']), target_1_price=float(candidate['target_1']), target_2_price=float(candidate['target_2']))
    payload = {
        'scan_id': candidate.get('scan_id'), 'symbol': candidate['symbol'], 'side': 'buy', 'decision': candidate.get('decision', 'BUY NOW'),
        'score_total': int(candidate.get('score_total', 0)), 'current_price': float(candidate['current_price']), 'entry_price': float(candidate['entry_price']),
        'buy_lower': float(candidate.get('buy_lower', candidate['entry_price'])), 'buy_upper': float(candidate['buy_upper']), 'stop_price': float(candidate['stop_price']),
        'target_1': float(candidate['target_1']), 'target_2': float(candidate['target_2']), 'qty': qty,
        'order_id': order.get('id'), 'order_status': order.get('status'), 'filled_avg_price': order.get('filled_avg_price'), 'filled_qty': order.get('filled_qty'),
        'outcome': 'open', 'notes': f'Executed via {source}', 'raw_json': {'order_bundle': order, 'execution_request': {
            **candidate,
            'probe_trade': bool(candidate.get('probe_trade')),
            'probe_reason': candidate.get('probe_reason'),
            'original_qty': candidate.get('original_qty'),
            'probe_risk_dollars': candidate.get('probe_risk_dollars'),
            'probe_qty_from_zero': bool(candidate.get('probe_qty_from_zero')),
            'soft_blockers_overridden': candidate.get('soft_blockers_overridden', []),
            'hard_blockers_overridden': candidate.get('hard_blockers_overridden', []),
            'qty': qty,
        }, 'source': source}
    }
    trade_id = insert_trade(payload)
    return {'trade_id': trade_id, 'order': order}
