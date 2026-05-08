import json
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import config
from broker import BrokerError, get_account, get_clock, get_latest_quote, get_open_orders, get_open_positions
from db import count_trades_today, get_conn, get_failed_trades_today, init_db, utc_now
from execution import get_runtime_state
from scanner import buy_window_open, within_morning_scan_window

ET = ZoneInfo(config.TIMEZONE_LABEL)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = (value or '').strip().split(':')
    if len(parts) != 2:
        raise ValueError(f'Invalid HH:MM format: {value}')
    hh = int(parts[0])
    mm = int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f'Invalid HH:MM range: {value}')
    return hh, mm


def _check(name: str, passed: bool, message: str, details: Dict[str, Any] | None = None, warn: bool = False) -> Dict[str, Any]:
    status = 'PASS' if passed and not warn else ('WARN' if warn else 'FAIL')
    out = {'name': name, 'status': status, 'message': message}
    if details is not None:
        out['details'] = details
    return out


def _ensure_preflight_table() -> None:
    with get_conn() as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS preflight_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                overall_status TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            '''
        )


def _record_preflight(result: Dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO preflight_checks (created_at, overall_status, result_json) VALUES (?, ?, ?)',
            (utc_now(), result.get('overall_status', 'UNKNOWN'), json.dumps(result)),
        )


def run_preflight() -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    blocking_reasons: List[str] = []
    warning_reasons: List[str] = []

    # 1) Env/config
    checks.append(_check('alpaca_api_key_present', bool(config.ALPACA_API_KEY), 'Alpaca API key present.' if config.ALPACA_API_KEY else 'Missing Alpaca API key.'))
    checks.append(_check('alpaca_api_secret_present', bool(config.ALPACA_API_SECRET), 'Alpaca API secret present.' if config.ALPACA_API_SECRET else 'Missing Alpaca API secret.'))
    paper_url = 'paper-api.alpaca.markets' in config.ALPACA_PAPER_BASE
    checks.append(_check('alpaca_base_is_paper', paper_url, f'ALPACA_PAPER_BASE={config.ALPACA_PAPER_BASE}', {'ALPACA_PAPER_BASE': config.ALPACA_PAPER_BASE}))
    checks.append(_check('paper_trading_detected', bool(config.PAPER_TRADING_DETECTED), f'PAPER_TRADING_DETECTED={config.PAPER_TRADING_DETECTED}'))
    checks.append(_check('auto_trade_enabled', bool(config.AUTO_TRADE_ENABLED), f'AUTO_TRADE_ENABLED={config.AUTO_TRADE_ENABLED}', warn=not config.AUTO_TRADE_ENABLED))

    checks.append(_check('scan_price_range_valid', config.SCAN_MIN_PRICE < config.SCAN_MAX_PRICE, 'SCAN_MIN_PRICE < SCAN_MAX_PRICE', {'SCAN_MIN_PRICE': config.SCAN_MIN_PRICE, 'SCAN_MAX_PRICE': config.SCAN_MAX_PRICE}))
    checks.append(_check('max_auto_trades_valid', config.MAX_AUTO_TRADES_PER_DAY >= 1, 'MAX_AUTO_TRADES_PER_DAY >= 1', {'MAX_AUTO_TRADES_PER_DAY': config.MAX_AUTO_TRADES_PER_DAY}))
    checks.append(_check('max_failed_trades_valid', config.MAX_FAILED_TRADES_PER_DAY >= 1, 'MAX_FAILED_TRADES_PER_DAY >= 1', {'MAX_FAILED_TRADES_PER_DAY': config.MAX_FAILED_TRADES_PER_DAY}))
    checks.append(_check('max_dollar_loss_valid', config.MAX_DOLLAR_LOSS_PER_TRADE > 0, 'MAX_DOLLAR_LOSS_PER_TRADE > 0', {'MAX_DOLLAR_LOSS_PER_TRADE': config.MAX_DOLLAR_LOSS_PER_TRADE}))
    checks.append(_check('quick_profit_pct_valid', config.QUICK_PROFIT_TAKE_PCT > 0, 'QUICK_PROFIT_TAKE_PCT > 0', {'QUICK_PROFIT_TAKE_PCT': config.QUICK_PROFIT_TAKE_PCT}))
    checks.append(_check('breakeven_trigger_pct_valid', config.BREAKEVEN_TRIGGER_PCT > 0, 'BREAKEVEN_TRIGGER_PCT > 0', {'BREAKEVEN_TRIGGER_PCT': config.BREAKEVEN_TRIGGER_PCT}))

    try:
        start = _parse_hhmm(config.MORNING_SCAN_START_ET)
        end = _parse_hhmm(config.MORNING_SCAN_END_ET)
        no_buy = _parse_hhmm(config.NO_BUY_BEFORE_ET)
        checks.append(_check('morning_scan_window_valid', start < end, 'MORNING_SCAN_START_ET is before MORNING_SCAN_END_ET.', {'MORNING_SCAN_START_ET': config.MORNING_SCAN_START_ET, 'MORNING_SCAN_END_ET': config.MORNING_SCAN_END_ET}))
        checks.append(_check('no_buy_before_before_scan_end', no_buy <= end, 'NO_BUY_BEFORE_ET is not after MORNING_SCAN_END_ET.', {'NO_BUY_BEFORE_ET': config.NO_BUY_BEFORE_ET, 'MORNING_SCAN_END_ET': config.MORNING_SCAN_END_ET}))
    except Exception as exc:
        checks.append(_check('time_config_parse', False, f'Time config parsing failed: {exc}'))

    # 2) Alpaca connectivity
    account, clock, positions, orders = {}, {}, [], []
    try:
        account = get_account()
        checks.append(_check('alpaca_account_endpoint', True, 'Account endpoint reachable.', {'status': account.get('status'), 'buying_power': account.get('buying_power'), 'trading_blocked': account.get('trading_blocked'), 'account_blocked': account.get('account_blocked')}))
    except Exception as exc:
        checks.append(_check('alpaca_account_endpoint', False, f'Account endpoint failed: {exc}'))

    try:
        clock = get_clock()
        checks.append(_check('alpaca_clock_endpoint', True, 'Clock endpoint reachable.', {'is_open': clock.get('is_open'), 'next_open': clock.get('next_open'), 'next_close': clock.get('next_close')}))
    except Exception as exc:
        checks.append(_check('alpaca_clock_endpoint', False, f'Clock endpoint failed: {exc}'))

    try:
        positions = get_open_positions()
        checks.append(_check('alpaca_positions_endpoint', True, f'Positions endpoint reachable ({len(positions)} open).', {'open_positions_count': len(positions)}))
    except Exception as exc:
        checks.append(_check('alpaca_positions_endpoint', False, f'Positions endpoint failed: {exc}'))

    try:
        orders = get_open_orders()
        checks.append(_check('alpaca_orders_endpoint', True, f'Orders endpoint reachable ({len(orders)} open).', {'open_orders_count': len(orders)}))
    except Exception as exc:
        checks.append(_check('alpaca_orders_endpoint', False, f'Orders endpoint failed: {exc}'))

    # 3) Data connectivity
    try:
        quote = get_latest_quote('SPY')
        has_quote = bool(quote and (quote.get('ap') or quote.get('bp') or quote.get('t')))
        checks.append(_check('alpaca_data_quote_spy', has_quote, 'SPY quote fetched.' if has_quote else 'SPY quote returned but missing expected fields.', {'quote': quote}, warn=not has_quote))
    except Exception as exc:
        checks.append(_check('alpaca_data_quote_spy', False, f'SPY quote check failed: {exc}'))

    # 4) DB health
    try:
        init_db()
        _ensure_preflight_table()
        with get_conn() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            scan = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scans'").fetchone()
            trades = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'").fetchone()
        checks.append(_check('db_tables_exist', bool(scan and trades), 'Core DB tables found.', {'db_path': config.DB_PATH, 'tables': tables}))
    except Exception as exc:
        checks.append(_check('db_tables_exist', False, f'DB table check failed: {exc}', {'db_path': config.DB_PATH}))

    # 5) Runtime health
    state = get_runtime_state()
    jobs = set(state.get('scheduled_jobs') or [])
    checks.append(_check('runtime_engine_started', bool(state.get('engine_started')), f"engine_started={state.get('engine_started')}"))
    checks.append(_check('runtime_scheduler_running', bool(state.get('scheduler_running')), f"scheduler_running={state.get('scheduler_running')}"))
    checks.append(_check('runtime_trade_stream_alive', bool(state.get('trade_stream_thread_alive')), f"trade_stream_thread_alive={state.get('trade_stream_thread_alive')}"))
    for job in ['flatten_book', 'position_monitor', 'auto_scan_loop']:
        checks.append(_check(f'job_{job}_registered', job in jobs, f'{job} registered={job in jobs}', {'scheduled_jobs': sorted(jobs)}))

    # 6) Auto-trade readiness
    if not config.AUTO_TRADE_ENABLED:
        blocking_reasons.append('auto_trade_disabled')
    if not config.PAPER_TRADING_DETECTED:
        blocking_reasons.append('not_paper_trading')
    if not (config.ALPACA_API_KEY and config.ALPACA_API_SECRET):
        blocking_reasons.append('missing_alpaca_credentials')
    if not state.get('scheduler_running'):
        blocking_reasons.append('scheduler_not_running')
    if 'auto_scan_loop' not in jobs:
        blocking_reasons.append('auto_scan_loop_missing')
    if not within_morning_scan_window():
        warning_reasons.append('outside_morning_scan_window')
    if not buy_window_open():
        warning_reasons.append('buy_window_closed')
    if get_failed_trades_today() >= config.MAX_FAILED_TRADES_PER_DAY:
        blocking_reasons.append('daily_failed_trade_lockout_active')
    if count_trades_today(source='auto') >= config.MAX_AUTO_TRADES_PER_DAY:
        blocking_reasons.append('max_auto_trades_reached')
    if positions:
        warning_reasons.append('open_positions_exist')
    if orders:
        warning_reasons.append('open_orders_exist')
    if not (config.SCAN_MIN_PRICE < config.SCAN_MAX_PRICE):
        blocking_reasons.append('scan_config_blocks_candidates')

    readiness = {
        'can_auto_trade_now': len(blocking_reasons) == 0,
        'blocking_reasons': blocking_reasons,
        'warning_reasons': warning_reasons,
    }
    checks.append(_check('auto_trade_readiness', readiness['can_auto_trade_now'], 'Auto-trade can execute now.' if readiness['can_auto_trade_now'] else 'Auto-trade currently blocked.', readiness, warn=(not readiness['can_auto_trade_now'] and len(blocking_reasons) == 0)))

    fail_count = sum(1 for c in checks if c['status'] == 'FAIL')
    warn_count = sum(1 for c in checks if c['status'] == 'WARN')
    overall = 'READY' if fail_count == 0 and warn_count == 0 else ('BLOCKED' if fail_count > 0 else 'WARNING')
    result = {'ok': overall != 'BLOCKED', 'overall_status': overall, 'checks': checks, 'auto_trade_readiness': readiness}

    try:
        _record_preflight(result)
    except Exception:
        pass
    return result
