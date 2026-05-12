import json
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import config
from broker_facade import BrokerError, get_account, get_asset, get_clock, get_latest_quote, get_open_orders, get_open_positions
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
    sim_mode = bool(config.SIMULATION_MODE)
    checks.append(_check('alpaca_api_key_present', bool(config.ALPACA_API_KEY) or sim_mode, 'Alpaca API key present.' if config.ALPACA_API_KEY else ('Missing Alpaca API key (allowed in simulation mode).' if sim_mode else 'Missing Alpaca API key.'), warn=(sim_mode and not config.ALPACA_API_KEY)))
    checks.append(_check('alpaca_api_secret_present', bool(config.ALPACA_API_SECRET) or sim_mode, 'Alpaca API secret present.' if config.ALPACA_API_SECRET else ('Missing Alpaca API secret (allowed in simulation mode).' if sim_mode else 'Missing Alpaca API secret.'), warn=(sim_mode and not config.ALPACA_API_SECRET)))
    paper_url = 'paper-api.alpaca.markets' in config.ALPACA_PAPER_BASE
    checks.append(_check('alpaca_base_is_paper', paper_url or sim_mode, f'ALPACA_PAPER_BASE={config.ALPACA_PAPER_BASE}', {'ALPACA_PAPER_BASE': config.ALPACA_PAPER_BASE}, warn=(sim_mode and not paper_url)))
    checks.append(_check('simulation_mode', bool(config.SIMULATION_MODE), 'Simulation Mode is ON — no Alpaca orders will be placed.' if config.SIMULATION_MODE else 'Simulation Mode is OFF.'))
    checks.append(_check('paper_trading_detected', bool(config.PAPER_TRADING_DETECTED) or sim_mode, f'PAPER_TRADING_DETECTED={config.PAPER_TRADING_DETECTED}' if not sim_mode else 'Paper trading detection not required in simulation mode.', warn=(sim_mode and not config.PAPER_TRADING_DETECTED)))
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
        checks.append(_check('alpaca_account_endpoint', True, ('Simulated account endpoint reachable.' if sim_mode else 'Account endpoint reachable.'), {'status': account.get('status'), 'buying_power': account.get('buying_power'), 'trading_blocked': account.get('trading_blocked'), 'account_blocked': account.get('account_blocked')}))
    except Exception as exc:
        checks.append(_check('alpaca_account_endpoint', sim_mode, f'Account endpoint failed: {exc}', warn=sim_mode))

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
    checks.append(_check('runtime_trade_stream_alive', bool(state.get('trade_stream_thread_alive')) or sim_mode, f"trade_stream_thread_alive={state.get('trade_stream_thread_alive')}", warn=(sim_mode and not state.get('trade_stream_thread_alive'))))
    for job in ['flatten_book', 'position_monitor', 'auto_scan_loop']:
        checks.append(_check(f'job_{job}_registered', job in jobs, f'{job} registered={job in jobs}', {'scheduled_jobs': sorted(jobs)}))

    # 6) Auto-trade readiness
    if not config.AUTO_TRADE_ENABLED:
        blocking_reasons.append('auto_trade_disabled')
    if not config.SIMULATION_MODE and not config.PAPER_TRADING_DETECTED:
        blocking_reasons.append('not_paper_trading')
    if not config.SIMULATION_MODE and not (config.ALPACA_API_KEY and config.ALPACA_API_SECRET):
        blocking_reasons.append('missing_alpaca_credentials')
    if not state.get('scheduler_running'):
        blocking_reasons.append('scheduler_not_running')
    if state.get('emergency_stop_active'):
        blocking_reasons.append('emergency_stop_active')
    if state.get('operator_auto_trade_paused'):
        blocking_reasons.append('operator_auto_trade_paused')
    if 'auto_scan_loop' not in jobs:
        blocking_reasons.append('auto_scan_loop_missing')
    if not within_morning_scan_window():
        blocking_reasons.append('outside_morning_scan_window')
        warning_reasons.append('outside_morning_scan_window')
    if not buy_window_open():
        blocking_reasons.append('buy_window_closed')
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
        'simulation_mode': bool(config.SIMULATION_MODE),
        'broker_backend': 'simulation' if config.SIMULATION_MODE else 'alpaca_paper',
        'can_auto_trade_now': len(blocking_reasons) == 0,
        'blocking_reasons': blocking_reasons,
        'warning_reasons': warning_reasons,
    }
    checks.append(_check('auto_trade_readiness', readiness['can_auto_trade_now'], 'Auto-trade can execute now.' if readiness['can_auto_trade_now'] else 'Auto-trade currently blocked.', readiness, warn=(not readiness['can_auto_trade_now'] and len(blocking_reasons) == 0)))

    fail_count = sum(1 for c in checks if c['status'] == 'FAIL')
    warn_count = sum(1 for c in checks if c['status'] == 'WARN')
    overall = 'READY' if fail_count == 0 and warn_count == 0 else ('BLOCKED' if fail_count > 0 else 'WARNING')
    result = {'ok': overall != 'BLOCKED', 'overall_status': overall, 'checks': checks, 'auto_trade_readiness': readiness, 'simulation_mode': bool(config.SIMULATION_MODE), 'broker_backend': 'simulation' if config.SIMULATION_MODE else 'alpaca_paper'}

    try:
        _record_preflight(result)
    except Exception:
        pass
    return result


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    for secret in (config.ALPACA_API_KEY, config.ALPACA_API_SECRET):
        if secret:
            text = text.replace(secret, '[redacted]')
    return text[:220]


def run_paper_trade_readiness_preflight(symbol: str | None = None) -> dict:
    symbol = (symbol or config.PREFLIGHT_SYMBOL or 'SPY').strip().upper()
    checks, blocking, warnings = [], [], []

    def add(name, status, message, details=None):
        checks.append({'name': name, 'status': status, 'message': message, 'details': details or {}})
        if status == 'FAIL':
            blocking.append(name)
        elif status == 'WARN':
            warnings.append(name)

    sim = bool(config.SIMULATION_MODE)
    paper_detected = bool(config.PAPER_TRADING_DETECTED)
    live_override = bool(config.LIVE_TRADING_OVERRIDE)

    add('paper_or_sim_guard', 'PASS' if (sim or paper_detected) else 'FAIL', 'Simulation or paper mode required.', {'simulation_mode': sim, 'paper_trading_detected': paper_detected})

    if sim:
        add('paper_base_url', 'PASS', 'Simulation mode bypasses paper base URL check.', {'alpaca_paper_base': config.ALPACA_PAPER_BASE})
    else:
        is_paper = 'paper-api.alpaca.markets' in (config.ALPACA_PAPER_BASE or '')
        add('paper_base_url', 'PASS' if (is_paper or live_override) else 'FAIL', 'Paper base URL validated.' if (is_paper or live_override) else 'Live/non-paper base URL blocked without LIVE_TRADING_OVERRIDE.', {'alpaca_paper_base': config.ALPACA_PAPER_BASE, 'live_trading_override': live_override})

    creds_present = bool(config.ALPACA_API_KEY and config.ALPACA_API_SECRET)
    add('credentials_present', 'PASS' if (sim or creds_present) else 'FAIL', 'Credentials present.' if creds_present else ('Simulation mode allows missing credentials.' if sim else 'Missing Alpaca API key/secret.'), {'has_key': bool(config.ALPACA_API_KEY), 'has_secret': bool(config.ALPACA_API_SECRET)})

    account = {}
    if sim:
        add('account_accessible', 'PASS', 'Simulation account path available.')
    else:
        try:
            account = get_account()
            add('account_accessible', 'PASS' if isinstance(account, dict) else 'FAIL', 'Account endpoint reachable.' if isinstance(account, dict) else 'Account endpoint returned invalid payload.')
        except Exception as exc:
            add('account_accessible', 'FAIL', f'Account endpoint unavailable: {_safe_error(exc)}')

    blocked_flags = []
    if account:
        for f in ('trading_blocked', 'account_blocked', 'trade_suspended_by_user'):
            if account.get(f) is True:
                blocked_flags.append(f)
        status = str(account.get('status') or '').lower()
        if status and status not in {'active'}:
            blocked_flags.append(f'status:{status}')
        add('account_tradeable', 'FAIL' if blocked_flags else 'PASS', 'Account tradeability check complete.' if not blocked_flags else 'Account blocked/restricted.', {'blocked_flags': blocked_flags, 'status': account.get('status')})
    elif sim:
        add('account_tradeable', 'PASS', 'Simulation mode tradeability assumed.')
    else:
        add('account_tradeable', 'WARN', 'Account payload missing; unable to fully evaluate tradeability.')

    quote = {}
    ask = 0.0
    try:
        quote = get_latest_quote(symbol) or {}
        ask = float(quote.get('ap') or quote.get('ask_price') or quote.get('price') or 0)
        bid = float(quote.get('bp') or quote.get('bid_price') or 0)
        has_quote = ask > 0 or bid > 0
        add('quote_accessible', 'PASS' if has_quote else 'FAIL', 'Quote accessible.' if has_quote else 'Quote missing/zero.', {'symbol': symbol, 'has_ask': ask > 0, 'has_bid': bid > 0})
    except Exception as exc:
        add('quote_accessible', 'FAIL', f'Quote unavailable: {_safe_error(exc)}', {'symbol': symbol})

    if quote:
        bid = float(quote.get('bp') or quote.get('bid_price') or 0)
        spread_status = 'WARN'
        spread_msg = 'Spread unknown.'
        spread_pct = None
        if ask > 0 and bid > 0:
            mid = (ask + bid) / 2
            spread_pct = ((ask - bid) / mid) if mid > 0 else None
            if spread_pct is not None and spread_pct <= float(config.PROBE_MAX_SPREAD_PCT):
                spread_status, spread_msg = 'PASS', 'Spread within probe threshold.'
            elif spread_pct is not None:
                spread_status, spread_msg = 'FAIL', 'Spread too wide for probe safety.'
        add('spread_reasonable_for_probe', spread_status, spread_msg, {'symbol': symbol, 'spread_pct': spread_pct, 'max_spread_pct': float(config.PROBE_MAX_SPREAD_PCT)})
    else:
        add('spread_reasonable_for_probe', 'WARN', 'Spread unavailable because quote is unavailable.', {'symbol': symbol})

    acct_buying = 0.0
    if sim:
        add('buying_power_probe_capacity', 'PASS', 'Simulation mode buying-power probe bypassed.')
    else:
        raw_bp = account.get('buying_power') if isinstance(account, dict) else None
        raw_cash = account.get('cash') if isinstance(account, dict) else None
        try:
            acct_buying = float(raw_bp or raw_cash or 0)
        except Exception:
            acct_buying = 0.0
        min_needed = max(float(config.PREFLIGHT_MIN_BUYING_POWER), float(ask or 0))
        if min_needed <= 0:
            add('buying_power_probe_capacity', 'WARN', 'Buying power unknown due to missing quote/account values.', {'buying_power': acct_buying, 'ask': ask, 'min_buying_power': float(config.PREFLIGHT_MIN_BUYING_POWER)})
        else:
            ok = acct_buying >= min_needed
            add('buying_power_probe_capacity', 'PASS' if ok else 'FAIL', 'Buying power can support 1-share preflight probe.' if ok else 'Insufficient buying power for 1-share probe.', {'buying_power': acct_buying, 'required': min_needed, 'symbol': symbol})

    try:
        clock = get_clock() or {}
        is_open = clock.get('is_open')
        if isinstance(clock, dict):
            add('clock_accessible', 'PASS' if is_open is not False else 'WARN', 'Clock endpoint reachable.' if is_open is not False else 'Clock reachable but market currently closed.', {'is_open': is_open, 'timestamp': clock.get('timestamp'), 'next_open': clock.get('next_open')})
        else:
            add('clock_accessible', 'FAIL', 'Clock endpoint returned invalid payload.')
    except Exception as exc:
        add('clock_accessible', 'FAIL', f'Clock endpoint unavailable: {_safe_error(exc)}')

    if config.PREFLIGHT_REQUIRE_ASSET_TRADABLE:
        try:
            asset = get_asset(symbol)
            tradable = bool((asset or {}).get('tradable'))
            add('symbol_tradability', 'PASS' if tradable else 'FAIL', 'Asset tradability verified.' if tradable else 'Asset is not tradable.', {'symbol': symbol, 'tradable': tradable, 'status': (asset or {}).get('status')})
        except Exception as exc:
            add('symbol_tradability', 'WARN', f'Asset lookup unavailable: {_safe_error(exc)}', {'symbol': symbol})
    else:
        add('symbol_tradability', 'WARN', 'Asset tradability check disabled by config.', {'symbol': symbol})

    state = get_runtime_state() or {}
    scheduler_ok = bool(state.get('scheduler_running')) and bool(state.get('auto_scan_job_registered'))
    add('scheduler_ready', 'PASS' if scheduler_ok else 'WARN', 'Scheduler and auto-scan job ready.' if scheduler_ok else 'Scheduler not running or auto-scan job missing.', {'scheduler_running': bool(state.get('scheduler_running')), 'auto_scan_job_registered': bool(state.get('auto_scan_job_registered'))})

    plan = state.get('last_auto_cycle_plan') or {}
    cand = int(plan.get('candidate_count') or 0)
    execs = int(plan.get('executable_count') or 0)
    if not plan:
        add('candidate_plan_available', 'WARN', 'No candidate plan available yet.')
    elif cand > 0 and execs == 0:
        add('candidate_plan_available', 'FAIL', 'Plan has candidates but zero executable trades.', {'candidate_count': cand, 'executable_count': execs})
    else:
        add('candidate_plan_available', 'PASS', 'Candidate plan available.', {'candidate_count': cand, 'executable_count': execs})

    gov_ok = bool(config.FIRST_TRADE_GOVERNOR_ENABLED) and int(config.FIRST_TRADE_MAX_QTY) >= 1 and float(config.FIRST_TRADE_MAX_DOLLAR_RISK) > 0
    add('first_trade_governor_ready', 'PASS' if gov_ok else 'FAIL', 'First-trade governor config valid.' if gov_ok else 'First-trade governor config invalid.', {'enabled': bool(config.FIRST_TRADE_GOVERNOR_ENABLED), 'max_qty': int(config.FIRST_TRADE_MAX_QTY), 'max_dollar_risk': float(config.FIRST_TRADE_MAX_DOLLAR_RISK)})

    overall = 'FAIL' if blocking else ('WARN' if warnings else 'PASS')
    hint = 'ready_for_open'
    if 'credentials_present' in blocking: hint = 'set_paper_credentials'
    elif 'paper_base_url' in blocking: hint = 'set_paper_base_url'
    elif 'scheduler_ready' in warnings: hint = 'start_scheduler'
    elif 'candidate_plan_available' in warnings: hint = 'run_auto_cycle_plan'

    return {'ok': overall == 'PASS', 'overall_status': overall, 'checks': checks, 'blocking_reasons': blocking, 'warning_reasons': warnings, 'next_action_hint': hint, 'symbol': symbol}
