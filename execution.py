import asyncio
import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import websockets
from apscheduler.schedulers.background import BackgroundScheduler

import config
from broker import (
    BrokerError,
    close_position,
    get_latest_quote,
    get_open_positions,
    get_order,
    maybe_activate_runner_trailing,
    submit_market_sell,
)
from db import get_active_trades, get_trade_by_target1_id, update_trade_status

logger = logging.getLogger(__name__)
ALPACA_WSS_URL = config.ALPACA_PAPER_BASE.replace('https', 'wss') + '/stream'
RUNTIME_STATE = {
    'engine_started': False,
    'scheduler_running': False,
    'trade_stream_thread_alive': False,
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
}
_scheduler = None
_ws_thread = None

def _alpaca_headers():
    return {'accept': 'application/json', 'APCA-API-KEY-ID': config.ALPACA_API_KEY, 'APCA-API-SECRET-KEY': config.ALPACA_API_SECRET}


def flatten_book():
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
                        from broker import replace_order

                        replace_order(runner_stop_id, {'stop_price': round(entry_price, 2)})
                        raw['breakeven_protected'] = True
                        changed = True
                    except BrokerError:
                        pass
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
                else:
                    qty = max(1, int(float(pos.get('qty') or 0)) // 2)
                    submit_market_sell(symbol, qty)
                raw['quick_profit_action_taken'] = True
                changed = True
            if changed:
                update_trade_status(order_id, {'raw_json': raw, 'notes': f'position_monitor pnl={pnl_pct:.2f}%'})
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

    if _ws_thread is None or not _ws_thread.is_alive():
        _ws_thread = threading.Thread(target=run_async_loop_in_thread, daemon=True, name='alpaca-trade-stream')
        _ws_thread.start()
        logger.info('Trade stream thread started.')

    RUNTIME_STATE.update({'engine_started': True, 'scheduler_running': True, 'trade_stream_thread_alive': bool(_ws_thread.is_alive())})
    return get_runtime_state()


def get_runtime_state():
    state = dict(RUNTIME_STATE)
    state['scheduler_running'] = bool(_scheduler and _scheduler.running)
    state['trade_stream_thread_alive'] = bool(_ws_thread and _ws_thread.is_alive())
    state['scheduled_jobs'] = [j.id for j in (_scheduler.get_jobs() if _scheduler else [])]
    return state
