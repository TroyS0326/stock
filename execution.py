import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import websockets
from apscheduler.schedulers.background import BackgroundScheduler

import config
from broker_facade import (
    BrokerError,
    cancel_open_orders_for_symbol,
    get_latest_quote,
    get_open_orders,
    get_open_positions,
    get_order,
    maybe_activate_runner_trailing,
    replace_order,
    replace_order_qty,
    submit_market_sell,
    submit_stop_sell,
    submit_trailing_stop_sell,
)
from db import get_active_trades, get_trade_by_target1_id, update_trade_status

logger = logging.getLogger(__name__)
ALPACA_WSS_URL = config.ALPACA_PAPER_BASE.replace('https', 'wss') + '/stream'
RUNTIME_STATE = {
    'engine_started': False,
    'scheduler_running': False,
    'trade_stream_thread_alive': False,
    'trade_stream_required': True,
    'trade_stream_skipped_reason': None,
    'last_scan_at': None,
    'last_scan_error': None,
    'last_scan_skipped_reason': None,
    'last_auto_trade_at': None,
    'last_auto_trade_error': None,
    'last_auto_trade_skip_reasons': [],
    'last_auto_trade_candidate_symbol': None,
    'last_auto_trade_verdict': None,
    'last_position_monitor_at': None,
    'last_position_monitor_error': None,
    'auto_trade_enabled': config.AUTO_TRADE_ENABLED,
    'operator_auto_trade_paused': False,
    'operator_pause_reason': None,
    'emergency_stop_active': False,
    'emergency_stop_reason': None,
    'last_operator_action_at': None,
    'last_operator_action': None,
    'last_operator_action_error': None,
    'operator_action_audit_log': [],
    'engine_start_attempted': False,
    'engine_start_at': None,
    'engine_start_error': None,
    'last_auto_cycle_plan': None,
    'last_auto_cycle_plan_at': None,
    'last_auto_cycle_plan_error': None,
}
_scheduler = None
_ws_thread = None


def _append_operator_audit(action: str, details: dict | None = None, error: str | None = None):
    stamp = datetime.utcnow().isoformat()
    entry = {'at': stamp, 'action': action, 'details': details or {}, 'error': error}
    audit = RUNTIME_STATE.setdefault('operator_action_audit_log', [])
    audit.append(entry)
    RUNTIME_STATE['operator_action_audit_log'] = audit[-100:]
    RUNTIME_STATE['last_operator_action_at'] = stamp
    RUNTIME_STATE['last_operator_action'] = action
    RUNTIME_STATE['last_operator_action_error'] = error


def get_runtime_trade_blocks() -> list[str]:
    blocks = []
    if RUNTIME_STATE.get('operator_auto_trade_paused'):
        blocks.append('operator_auto_trade_paused')
    if RUNTIME_STATE.get('emergency_stop_active'):
        blocks.append('emergency_stop_active')
    return blocks


def set_operator_pause(paused: bool, reason: str | None = None):
    RUNTIME_STATE['operator_auto_trade_paused'] = bool(paused)
    RUNTIME_STATE['operator_pause_reason'] = reason if paused else None
    _append_operator_audit('pause_auto_trading' if paused else 'resume_auto_trading', {'reason': reason} if reason else {})


def set_emergency_stop(active: bool, reason: str | None = None):
    RUNTIME_STATE['emergency_stop_active'] = bool(active)
    RUNTIME_STATE['emergency_stop_reason'] = reason if active else None
    _append_operator_audit('emergency_stop_activate' if active else 'emergency_stop_clear', {'reason': reason} if reason else {})


def emergency_cancel_and_flatten(close_positions: bool = False, reason: str | None = None):
    errors = []
    canceled_symbols = []
    closed_positions = []
    try:
        for order in get_open_orders() or []:
            symbol = order.get('symbol')
            if not symbol:
                continue
            try:
                cancel_open_orders_for_symbol(symbol)
                canceled_symbols.append(symbol)
            except Exception as exc:
                errors.append(f'cancel:{symbol}:{exc}')
    except Exception as exc:
        errors.append(f'cancel_scan:{exc}')

    if close_positions:
        try:
            for pos in get_open_positions() or []:
                symbol = pos.get('symbol')
                qty = int(float(pos.get('qty') or 0))
                if not symbol or qty <= 0:
                    continue
                try:
                    submit_market_sell(symbol, qty)
                    closed_positions.append({'symbol': symbol, 'qty': qty})
                except Exception as exc:
                    errors.append(f'close:{symbol}:{exc}')
        except Exception as exc:
            errors.append(f'close_scan:{exc}')

    set_emergency_stop(True, reason=reason)
    error_text = '; '.join(errors) if errors else None
    _append_operator_audit('emergency_cancel_and_flatten', {'close_positions': close_positions, 'reason': reason, 'canceled_symbols': sorted(set(canceled_symbols)), 'closed_positions': closed_positions}, error=error_text)
    return {'ok': not bool(errors), 'errors': errors, 'canceled_symbols': sorted(set(canceled_symbols)), 'closed_positions': closed_positions}

def _alpaca_headers():
    return {'accept': 'application/json', 'APCA-API-KEY-ID': config.ALPACA_API_KEY, 'APCA-API-SECRET-KEY': config.ALPACA_API_SECRET}


def flatten_book():
    if config.SIMULATION_MODE:
        for order in get_open_orders() or []:
            symbol = order.get('symbol')
            if symbol:
                cancel_open_orders_for_symbol(symbol)
        for pos in get_open_positions() or []:
            symbol = pos.get('symbol')
            qty = int(float(pos.get('qty') or 0))
            if symbol and qty > 0:
                submit_market_sell(symbol, qty)
        return
    try:
        requests.delete(f'{config.ALPACA_PAPER_BASE}/v2/orders', headers=_alpaca_headers(), timeout=10)
        requests.delete(f'{config.ALPACA_PAPER_BASE}/v2/positions', headers=_alpaca_headers(), timeout=10)
    except Exception as exc:
        logger.error('Kill switch error: %s', exc)


async def handle_fill_event(order):
    trade = get_trade_by_target1_id(order.get('id'))
    if not trade:
        return
    raw = trade.get('raw_json') or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    bundle = raw.get('order_bundle', {})
    updated = maybe_activate_runner_trailing(bundle, breakeven_price=float(trade.get('entry_price') or 0))
    raw['order_bundle'] = updated
    update_trade_status(trade['order_id'], {'raw_json': raw})


async def alpaca_trade_listener():
    async for websocket in websockets.connect(ALPACA_WSS_URL):
        try:
            await websocket.send(json.dumps({'action': 'auth', 'key': config.ALPACA_API_KEY, 'secret': config.ALPACA_API_SECRET}))
            await websocket.recv()
            await websocket.send(json.dumps({'action': 'listen', 'data': {'streams': ['trade_updates']}}))
            await websocket.recv()
            async for message in websocket:
                data = json.loads(message)
                if data.get('stream') == 'trade_updates' and data.get('data', {}).get('event') in ('fill', 'partial_fill'):
                    await handle_fill_event(data.get('data', {}).get('order', {}))
        except Exception:
            await asyncio.sleep(1)


def run_async_loop_in_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(alpaca_trade_listener())


def _register_scheduler_jobs(auto_scan_callback=None):
    hh, mm = [int(x) for x in config.HARD_EXIT_TIME_ET.split(':', 1)]
    _scheduler.add_job(flatten_book, 'cron', day_of_week='mon-fri', hour=hh, minute=mm, id='flatten_book', replace_existing=True)
    _scheduler.add_job(monitor_positions_job, 'interval', seconds=config.POSITION_MONITOR_INTERVAL_SECONDS, id='position_monitor', replace_existing=True)
    if auto_scan_callback is not None:
        _scheduler.add_job(auto_scan_callback, 'interval', seconds=config.AUTO_SCAN_INTERVAL_SECONDS, id='auto_scan_loop', replace_existing=True)


def monitor_positions_job():
    RUNTIME_STATE['last_position_monitor_at'] = datetime.utcnow().isoformat()
    had_monitor_issue = False
    try:
        positions = {p.get('symbol'): p for p in get_open_positions()}
        for trade in get_active_trades(200):
            symbol = trade.get('symbol')
            order_id = trade.get('order_id')
            if not symbol or not order_id:
                continue
            pos = positions.get(symbol)
            if not pos:
                continue
            raw = trade.get('raw_json') or {}
            if isinstance(raw, str):
                raw = json.loads(raw or '{}')
            order_bundle = raw.get('order_bundle') or {}
            entry_price = float(trade.get('filled_avg_price') or trade.get('entry_price') or pos.get('avg_entry_price') or 0)
            if entry_price <= 0:
                continue
            created_at = trade.get('created_at')
            age_exit = False
            try:
                if created_at:
                    created_dt = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    age_minutes = (datetime.now(timezone.utc) - created_dt.astimezone(timezone.utc)).total_seconds() / 60.0
                    age_exit = age_minutes >= config.MAX_INTRADAY_POSITION_MINUTES
            except Exception:
                age_exit = False
            et_now = datetime.now(ZoneInfo(config.TIMEZONE_LABEL))
            hh, mm = [int(x) for x in config.HARD_EXIT_TIME_ET.split(':', 1)]
            hard_exit = (et_now.hour, et_now.minute) >= (hh, mm)
            if age_exit or hard_exit:
                reason = 'max_intraday_minutes_exceeded' if age_exit else 'hard_exit_time_reached'
                qty_all = max(1, int(float(pos.get('qty') or 0)))
                try:
                    cancel_open_orders_for_symbol(symbol)
                    time_exit_order = submit_market_sell(symbol, qty_all)
                    raw['time_exit_action_taken'] = True
                    raw['time_exit_reason'] = reason
                    raw['time_exit_order_id'] = time_exit_order.get('id')
                    update_trade_status(order_id, {'raw_json': raw, 'notes': f'time_exit:{reason}'})
                except Exception as exc:
                    had_monitor_issue = True
                    RUNTIME_STATE['last_position_monitor_error'] = str(exc)
                    raw['time_exit_action_taken'] = False
                    raw['time_exit_reason'] = f'{reason}_failed:{exc}'
                    update_trade_status(order_id, {'raw_json': raw, 'notes': f'time_exit_failed:{reason}'})
                continue
            latest = get_latest_quote(symbol)
            current_price = float(latest.get('ap') or latest.get('bp') or pos.get('current_price') or 0)
            if current_price <= 0:
                continue
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            changed = False
            if pnl_pct >= config.BREAKEVEN_TRIGGER_PCT and not raw.get('breakeven_protected'):
                runner_stop_id = order_bundle.get('runner_stop_order_id')
                if runner_stop_id:
                    try:
                        runner_order = get_order(runner_stop_id)
                        runner_status = (runner_order.get('status') or '').lower()
                        if runner_status == 'open':
                            replace_order(runner_stop_id, {'stop_price': round(entry_price, 2)})
                            raw['breakeven_protected'] = True
                            raw['breakeven_protect_order_id'] = runner_stop_id
                            logger.info('Breakeven protected for %s with order %s', symbol, runner_stop_id)
                        else:
                            raw['breakeven_blocked_reason'] = f'runner_stop_not_open:{runner_status}'
                            had_monitor_issue = True
                            logger.warning('Breakeven blocked for %s: %s', symbol, raw['breakeven_blocked_reason'])
                        changed = True
                    except BrokerError as exc:
                        raw['breakeven_blocked_reason'] = str(exc)
                        had_monitor_issue = True
                        changed = True
                        logger.warning('Breakeven blocked for %s: %s', symbol, exc)
            if pnl_pct >= config.QUICK_PROFIT_TAKE_PCT and not raw.get('quick_profit_action_taken'):
                target_1_id = order_bundle.get('target_1_order_id')
                t1_filled = False
                if target_1_id:
                    t1 = get_order(target_1_id)
                    t1_filled = (t1.get('status') or '').lower() == 'filled'
                if t1_filled:
                    order_bundle = maybe_activate_runner_trailing(order_bundle, breakeven_price=entry_price)
                    raw['runner_trailing_activated'] = bool(order_bundle.get('runner_trailing_activated'))
                    raw['order_bundle'] = order_bundle
                    raw['quick_profit_orders_reconciled'] = True
                else:
                    qty = max(1, int(float(pos.get('qty') or 0)) // 2)
                    open_sell_orders = [o for o in get_open_orders(symbol) if (o.get('side') or '').lower() == 'sell']
                    reconciled = True
                    blocked_reason = None
                    if any(not o.get('id') for o in open_sell_orders):
                        reconciled = False
                        blocked_reason = 'open_sell_order_missing_id'
                    elif open_sell_orders:
                        try:
                            canceled_ids = cancel_open_orders_for_symbol(symbol, side='sell')
                            logger.info('Reconciled quick-profit orders for %s canceled=%s', symbol, canceled_ids)
                        except BrokerError as exc:
                            reconciled = False
                            blocked_reason = f'cancel_failed:{exc}'
                    if not reconciled:
                        raw['quick_profit_blocked_reason'] = blocked_reason or 'orders_not_reconciled'
                        raw['quick_profit_orders_reconciled'] = False
                        raw.setdefault('notes', '')
                        had_monitor_issue = True
                        RUNTIME_STATE['last_position_monitor_error'] = raw['quick_profit_blocked_reason']
                        logger.warning('Quick profit blocked for %s: %s', symbol, raw['quick_profit_blocked_reason'])
                    else:
                        original_qty = int(float(pos.get('qty') or 0))
                        restore_stop_price = float(trade.get('stop_price') or 0)
                        if restore_stop_price <= 0:
                            restore_stop_price = float(order_bundle.get('runner_breakeven_price') or 0)
                        if restore_stop_price <= 0:
                            restore_stop_price = entry_price
                        if restore_stop_price >= current_price:
                            restore_stop_price = min(entry_price, round(current_price * 0.995, 4))
                        try:
                            sell_order = submit_market_sell(symbol, qty)
                        except BrokerError as partial_sell_exc:
                            partial_sell_reason = str(partial_sell_exc)
                            raw['quick_profit_partial_sell_failed_reason'] = partial_sell_reason
                            had_monitor_issue = True
                            RUNTIME_STATE['last_position_monitor_error'] = partial_sell_reason
                            try:
                                restore_order = submit_stop_sell(symbol, original_qty, restore_stop_price)
                            except BrokerError as restore_exc:
                                reprotect_reason = str(restore_exc)
                                raw['quick_profit_reprotect_failed_reason'] = reprotect_reason
                                try:
                                    flatten_order = submit_market_sell(symbol, original_qty)
                                except BrokerError as flatten_exc:
                                    raw['quick_profit_forced_flatten_failed_reason'] = str(flatten_exc)
                                    raw['quick_profit_protection_type'] = 'failed'
                                    RUNTIME_STATE['last_position_monitor_error'] = str(flatten_exc)
                                else:
                                    raw['quick_profit_forced_flatten_order_id'] = flatten_order.get('id')
                                    raw['quick_profit_forced_flatten_reason'] = 'partial_sell_failed_and_reprotect_failed'
                                    raw['quick_profit_protection_type'] = 'forced_flatten'
                            else:
                                raw['quick_profit_reprotected_after_partial_sell_failure'] = True
                                raw['quick_profit_reprotect_order_id'] = restore_order.get('id')
                                raw['quick_profit_protection_type'] = 'stop_restore'
                            raw['quick_profit_action_taken'] = False
                            changed = True
                            update_trade_status(order_id, {'raw_json': raw, 'notes': f'position_monitor pnl={pnl_pct:.2f}% quick_profit_partial_failed'})
                            continue
                        remaining_qty = max(0, original_qty - qty)
                        protection_order = None
                        protection_type = None
                        protection_failed_reason = None
                        forced_flatten_order = None
                        if remaining_qty > 0:
                            try:
                                trail_pct = float(order_bundle.get('runner_trailing_pct') or config.TARGET2_TRAILING_STOP_PCT)
                                protection_order = submit_trailing_stop_sell(symbol, remaining_qty, trail_pct)
                                protection_type = 'trailing_stop'
                            except BrokerError as trail_exc:
                                try:
                                    protection_order = submit_stop_sell(symbol, remaining_qty, entry_price)
                                    protection_type = 'stop'
                                except BrokerError as stop_exc:
                                    protection_failed_reason = f'trailing:{trail_exc};stop:{stop_exc}'
                                    try:
                                        forced_flatten_order = submit_market_sell(symbol, remaining_qty)
                                        protection_type = 'forced_flatten'
                                        logger.warning('Quick profit protection failed for %s; forced flatten submitted order_id=%s', symbol, forced_flatten_order.get('id'))
                                    except BrokerError as flatten_exc:
                                        protection_type = 'failed'
                                        raw['quick_profit_forced_flatten_failed_reason'] = str(flatten_exc)
                                        had_monitor_issue = True
                                        RUNTIME_STATE['last_position_monitor_error'] = str(flatten_exc)
                                        logger.warning('Quick profit forced flatten failed for %s: %s', symbol, flatten_exc)
                                    else:
                                        raw['quick_profit_forced_flatten_order_id'] = forced_flatten_order.get('id')
                                        raw['quick_profit_forced_flatten_reason'] = 'protection_order_failed'
                                        raw['quick_profit_remaining_qty_after_forced_flatten'] = 0
                                        had_monitor_issue = True
                                        RUNTIME_STATE['last_position_monitor_error'] = protection_failed_reason
                        raw['quick_profit_orders_reconciled'] = True
                        raw['quick_profit_sell_order_id'] = sell_order.get('id')
                        raw['quick_profit_original_qty'] = original_qty
                        raw['quick_profit_sell_qty'] = qty
                        raw['quick_profit_remaining_qty'] = remaining_qty
                        raw['quick_profit_protection_order_id'] = (protection_order or {}).get('id')
                        raw['quick_profit_protection_type'] = protection_type
                        raw['quick_profit_blocked_reason'] = None
                        raw['quick_profit_protection_failed_reason'] = protection_failed_reason
                        raw['quick_profit_action_taken'] = True
                        logger.info('Quick profit sell sent for %s qty=%s order_id=%s', symbol, qty, sell_order.get('id'))
                if t1_filled:
                    raw['quick_profit_action_taken'] = True
                changed = True
            if changed:
                update_trade_status(order_id, {'raw_json': raw, 'notes': f'position_monitor pnl={pnl_pct:.2f}%'})
        if not had_monitor_issue:
            RUNTIME_STATE['last_position_monitor_error'] = None
    except Exception as exc:
        RUNTIME_STATE['last_position_monitor_error'] = str(exc)
        logger.exception('position monitor failed')


def start_execution_engine(auto_scan_callback=None):
    global _scheduler, _ws_thread
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=ZoneInfo(config.TIMEZONE_LABEL))
    _register_scheduler_jobs(auto_scan_callback=auto_scan_callback)
    if not _scheduler.running:
        _scheduler.start()
        logger.info('Scheduler started.')

    if config.SIMULATION_MODE:
        RUNTIME_STATE['trade_stream_required'] = False
        RUNTIME_STATE['trade_stream_skipped_reason'] = 'simulation_mode'
        RUNTIME_STATE['trade_stream_thread_alive'] = False
    elif _ws_thread is None or not _ws_thread.is_alive():
        _ws_thread = threading.Thread(target=run_async_loop_in_thread, daemon=True, name='alpaca-trade-stream')
        _ws_thread.start()
        logger.info('Trade stream thread started.')
        RUNTIME_STATE['trade_stream_required'] = True
        RUNTIME_STATE['trade_stream_skipped_reason'] = None

    RUNTIME_STATE.update({'engine_started': True, 'scheduler_running': True, 'trade_stream_thread_alive': bool(_ws_thread.is_alive()) if _ws_thread else False})
    return get_runtime_state()


def get_runtime_state():
    state = dict(RUNTIME_STATE)
    state['scheduler_running'] = bool(_scheduler and _scheduler.running)
    state['trade_stream_thread_alive'] = bool(_ws_thread and _ws_thread.is_alive())
    scheduled_jobs = [j.id for j in (_scheduler.get_jobs() if _scheduler else [])]
    state['scheduled_jobs'] = scheduled_jobs
    state['auto_scan_job_registered'] = 'auto_scan_loop' in scheduled_jobs
    state['position_monitor_job_registered'] = 'position_monitor' in scheduled_jobs
    state['flatten_job_registered'] = 'flatten_book' in scheduled_jobs
    return state
