from __future__ import annotations

import config
from broker import place_managed_entry_order
from db import count_trades_today, get_failed_trades_today, get_trade_by_symbol_today, insert_trade
from execution import get_runtime_trade_blocks
from scanner import buy_window_open, within_morning_scan_window

TRIGGER_MAP = {
    'ORB_BREAKOUT': lambda d: bool((d.get('opening_range_confirmation') or {}).get('breakout_confirmed')),
    'VWAP_RECLAIM': lambda d: bool((d.get('vwap_hold_reclaim') or {}).get('reclaimed_vwap')),
    'VWAP_PULLBACK_BOUNCE': lambda d: bool((d.get('vwap_hold_reclaim') or {}).get('held_vwap')),
    'MOMENTUM_CONTINUATION': lambda d: bool(d.get('momentum_continuation', False)),
}


def detect_entry_trigger(candidate):
    details = candidate.get('details') or {}
    scanner_trigger = (details.get('entry_trigger') or '').upper().strip()
    if scanner_trigger in {'ORB_BREAKOUT', 'VWAP_RECLAIM', 'VWAP_PULLBACK_BOUNCE', 'MOMENTUM_CONTINUATION', 'NO_TRIGGER'}:
        return scanner_trigger
    for name, fn in TRIGGER_MAP.items():
        if fn(details):
            return name
    return 'NO_TRIGGER'


def validate_trade_candidate(candidate, auto=False):
    skip = []
    decision = (candidate.get('decision') or '').upper()
    if auto and not config.AUTO_TRADE_ENABLED:
        skip.append('auto_trade_disabled')
    if auto:
        skip.extend(get_runtime_trade_blocks())
    if auto and not within_morning_scan_window():
        skip.append('outside_morning_scan_window')
    if get_failed_trades_today() >= config.MAX_FAILED_TRADES_PER_DAY:
        skip.append('failed_trade_lockout')
    if candidate.get('setup_grade') not in {'A', 'A+'}:
        skip.append('setup_grade_not_allowed')
    if int(candidate.get('score_total', 0)) < config.MIN_SCORE_TO_EXECUTE:
        skip.append('score_too_low')
    catalyst = int((candidate.get('scores') or {}).get('catalyst', 0))
    if catalyst < config.MIN_CATALYST_SCORE:
        skip.append('catalyst_too_low')
    details = candidate.get('details') or {}
    if float(details.get('spread_pct', 0) or 0) > config.MAX_SPREAD_PCT:
        skip.append('wide_spread')
    if float(candidate.get('current_price', 0)) > float(candidate.get('buy_upper', 0)):
        skip.append('price_extended')
    if not buy_window_open():
        skip.append('buy_window_closed')
    if int(candidate.get('qty', 0) or 0) < 1:
        skip.append('qty_zero')

    trigger = detect_entry_trigger(candidate)
    if trigger == 'NO_TRIGGER':
        skip.append('no_valid_entry_trigger')
    risk = (float(candidate.get('entry_price', 0)) - float(candidate.get('stop_price', 0))) * int(candidate.get('qty', 0) or 0)
    if risk > config.MAX_DOLLAR_LOSS_PER_TRADE + 0.01:
        skip.append('oversized_risk')

    symbol = candidate.get('symbol')
    if auto and count_trades_today(source='auto') >= config.MAX_AUTO_TRADES_PER_DAY:
        skip.append('max_auto_trades_reached')
    if symbol and (not config.ALLOW_DUPLICATE_SYMBOL_TRADES_PER_DAY) and get_trade_by_symbol_today(symbol):
        skip.append('duplicate_symbol_trade_blocked')

    if auto and decision != 'BUY NOW':
        skip.append('auto_decision_not_actionable')
    if not auto and decision == 'WAIT':
        skip.append('manual_wait_decision')

    return {'ok': not skip, 'entry_trigger': trigger, 'skip_reasons': skip}


def execute_trade_candidate(candidate, source='manual'):
    order = place_managed_entry_order(symbol=candidate['symbol'], qty=int(candidate['qty']), entry_price=float(candidate['entry_price']), stop_price=float(candidate['stop_price']), target_1_price=float(candidate['target_1']), target_2_price=float(candidate['target_2']))
    payload = {
        'scan_id': candidate.get('scan_id'), 'symbol': candidate['symbol'], 'side': 'buy', 'decision': candidate.get('decision', 'BUY NOW'),
        'score_total': int(candidate.get('score_total', 0)), 'current_price': float(candidate['current_price']), 'entry_price': float(candidate['entry_price']),
        'buy_lower': float(candidate.get('buy_lower', candidate['entry_price'])), 'buy_upper': float(candidate['buy_upper']), 'stop_price': float(candidate['stop_price']),
        'target_1': float(candidate['target_1']), 'target_2': float(candidate['target_2']), 'qty': int(candidate['qty']),
        'order_id': order.get('id'), 'order_status': order.get('status'), 'filled_avg_price': order.get('filled_avg_price'), 'filled_qty': order.get('filled_qty'),
        'outcome': 'open', 'notes': f'Executed via {source}', 'raw_json': {'order_bundle': order, 'execution_request': candidate, 'source': source}
    }
    trade_id = insert_trade(payload)
    return {'trade_id': trade_id, 'order': order}
