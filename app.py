import json
import logging
import os
import sqlite3

from flask import Flask, jsonify, render_template, request
from flask_sock import Sock

import config
from broker_facade import BrokerError, get_order, maybe_activate_runner_trailing, get_open_orders, get_open_positions, get_account
import db
from db import get_failed_trades_today, get_recent_operator_actions, get_recent_scans, get_recent_trades, get_trade_by_order_id, init_db, insert_operator_action, insert_scan, update_trade_status
from execution import (
    RUNTIME_STATE,
    emergency_cancel_and_flatten,
    get_runtime_state,
    set_emergency_stop,
    set_operator_pause,
    start_execution_engine,
)
from scanner import ScanError, buy_window_open, get_stock_chart_pack, now_et, run_scan, within_auto_scan_window, within_morning_scan_window
from watchlist import watchlist_manager
from execution_service import validate_trade_candidate, execute_trade_candidate
from preflight import run_preflight

app = Flask(__name__)
app.config['SECRET_KEY'] = config.SECRET_KEY
sock = Sock(app)
logger = logging.getLogger(__name__)


def ensure_db_initialized() -> None:
    try:
        init_db()
        return
    except (sqlite3.OperationalError, PermissionError) as exc:
        fallback_dir = os.getenv('DB_FALLBACK_DIR', '/tmp')
        fallback_path = os.path.join(fallback_dir, 'veteran_trades.db')
        logger.warning('Primary DB path failed (%s). Falling back to %s. Error: %s', config.DB_PATH, fallback_path, exc)
        config.DB_PATH = fallback_path
        db.config.DB_PATH = fallback_path
        init_db()


ensure_db_initialized()

LATEST_SCAN = None

def run_scan_and_maybe_auto_trade():
    global LATEST_SCAN
    if not within_auto_scan_window():
        RUNTIME_STATE['last_scan_skipped_reason'] = 'outside_auto_scan_window'
        RUNTIME_STATE['last_auto_trade_error'] = 'outside_auto_scan_window'
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['outside_auto_scan_window']
        RUNTIME_STATE['last_auto_trade_attempts'] = []
        RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': ['outside_auto_scan_window']}
        logger.info('Auto scan skipped: outside morning window.')
        return
    try:
        result = run_scan()
        scan_id = insert_scan(result)
        result['scan_id'] = scan_id
        LATEST_SCAN = result
        watchlist_manager.set_items(result.get('watchlist', []))
        RUNTIME_STATE['last_scan_at'] = now_et().isoformat()
        RUNTIME_STATE['last_scan_error'] = None
        RUNTIME_STATE['last_scan_skipped_reason'] = None
        ranked = []
        if result.get('best_pick'): ranked.append(result['best_pick'])
        ranked.extend(result.get('watchlist', []))
        seen, candidates = set(), []
        for c in ranked:
            sym = (c or {}).get('symbol')
            if sym and sym not in seen:
                seen.add(sym); candidates.append(c)
        attempts, all_reasons = [], set()
        RUNTIME_STATE['last_auto_trade_error'] = None
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = []
        RUNTIME_STATE['last_auto_trade_verdict'] = None
        executed = False
        if not candidates:
            RUNTIME_STATE['last_auto_trade_error'] = 'no_candidates'
            RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['no_candidates']
            RUNTIME_STATE['last_auto_trade_attempts'] = []
            RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': ['no_candidates']}
            return
        for candidate in candidates[:max(1, config.AUTO_TRADE_CANDIDATE_LIMIT)]:
            candidate['scan_id'] = scan_id
            verdict = validate_trade_candidate(candidate, auto=True)
            attempts.append({
                'symbol': candidate.get('symbol'),
                'ok': verdict.get('ok'),
                'entry_trigger': verdict.get('entry_trigger'),
                'fallback_used': verdict.get('fallback_used'),
                'risk_dollars': candidate.get('risk_dollars') or candidate.get('max_dollar_loss'),
                'skip_reasons': verdict.get('skip_reasons', []),
                'fallback_reasons': verdict.get('fallback_reasons', []),
                'probe_trade': verdict.get('probe_trade', False),
                'probe_trade_ok': verdict.get('probe_trade_ok', False),
                'probe_reasons': verdict.get('probe_reasons', []),
                'probe_qty': verdict.get('probe_qty'),
                'probe_risk_dollars': verdict.get('probe_risk_dollars'),
                'soft_blockers_overridden': verdict.get('soft_blockers_overridden', []),
                'score_total': candidate.get('score_total'),
                'setup_grade': candidate.get('setup_grade'),
                'error': None,
            })
            all_reasons.update(verdict.get('skip_reasons', []))
            if verdict.get('ok'):
                try:
                    executed_trade = execute_trade_candidate(candidate, source='auto')
                    RUNTIME_STATE['last_auto_trade_at'] = now_et().isoformat()
                    RUNTIME_STATE['last_auto_trade_error'] = None
                    RUNTIME_STATE['last_auto_trade_skip_reasons'] = []
                    RUNTIME_STATE['last_auto_trade_candidate_symbol'] = candidate.get('symbol')
                    RUNTIME_STATE['last_auto_trade_verdict'] = verdict
                    trade_order = (executed_trade or {}).get('order') or {}
                    attempts[-1]['trade_id'] = (executed_trade or {}).get('trade_id')
                    attempts[-1]['order_id'] = trade_order.get('id')
                    attempts[-1]['order_status'] = trade_order.get('status')
                    executed = True
                    break
                except Exception as exc:
                    attempts[-1]['error'] = str(exc)
                    RUNTIME_STATE['last_auto_trade_error'] = str(exc)
                    RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['execution_failed']
        RUNTIME_STATE['last_auto_trade_attempts'] = attempts
        if not executed and RUNTIME_STATE.get('last_auto_trade_skip_reasons') != ['execution_failed']:
            RUNTIME_STATE['last_auto_trade_error'] = 'no_executable_candidate'
            RUNTIME_STATE['last_auto_trade_skip_reasons'] = sorted(all_reasons)
            RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': sorted(all_reasons)}
    except Exception as exc:
        RUNTIME_STATE['last_scan_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['auto_cycle_exception']
        RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': ['auto_cycle_exception']}


@app.route('/api/auto-cycle', methods=['POST'])
@app.route('/api/run-auto-cycle', methods=['POST'])
def api_auto_cycle():
    if not (bool(config.PAPER_TRADING_DETECTED) or bool(config.SIMULATION_MODE)):
        return fail('auto_cycle_blocked_not_paper', 409)
    try:
        run_scan_and_maybe_auto_trade()
    except Exception as exc:
        RUNTIME_STATE['last_scan_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['auto_cycle_exception']
        RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': ['auto_cycle_exception']}
        return fail('auto_cycle_failed', 500, runtime_state=get_runtime_state())
    state = get_runtime_state()

    return ok({
        'runtime_state': state,
        'latest_scan': LATEST_SCAN,
        'last_auto_trade_attempts': state.get('last_auto_trade_attempts', []),
        'last_auto_trade_error': state.get('last_auto_trade_error'),
        'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons', []),
        'last_auto_trade_verdict': state.get('last_auto_trade_verdict'),
    })



def ok(data=None, **kwargs):
    payload = {'ok': True}
    if data is not None:
        payload['data'] = data
    payload.update(kwargs)
    return jsonify(payload)


def fail(message: str, status: int = 400, **extras):
    payload = {'ok': False, 'error': message}
    payload.update(extras)
    return jsonify(payload), status


def order_outcome_from_payload(order: dict) -> str:
    status = (order.get('status') or '').lower()
    if order.get('strategy') == 'target1_then_trailing_runner':
        t1 = order.get('target_1_order') or {}
        runner = order.get('runner_order') or {}
        runner_trailing = order.get('runner_trailing_order') or {}
        if (runner_trailing.get('status') or '').lower() == 'filled':
            return 'win'
        if (runner.get('status') or '').lower() == 'filled':
            return 'breakeven_or_small_win'
        if (t1.get('status') or '').lower() == 'filled':
            return 'partial_win'
        if status in {'rejected'}:
            return 'rejected'
        if status in {'canceled', 'expired'}:
            return 'failed'
        return 'open'
    legs = order.get('legs') or []
    for leg in legs:
        leg_type = (leg.get('order_type') or '').lower()
        leg_status = (leg.get('status') or '').lower()
        if leg_type == 'limit' and leg_status == 'filled':
            return 'win'
        if leg_type == 'stop' and leg_status == 'filled':
            return 'loss'
    if status in {'rejected'}:
        return 'rejected'
    if status in {'canceled', 'expired'}:
        return 'failed'
    if status == 'filled':
        return 'working_or_filled'
    return 'open'


@app.route('/')
def index():
    return render_template('index.html', app_title="Veteran Pro")




@app.route('/api/bot-status')
def api_bot_status():
    state = get_runtime_state()

    sim_orders = get_open_orders() if config.SIMULATION_MODE else []
    sim_positions = get_open_positions() if config.SIMULATION_MODE else []
    sim_account = get_account() if config.SIMULATION_MODE else {}

    control_state = {
        'config_auto_trade_enabled': bool(config.AUTO_TRADE_ENABLED),
        'operator_auto_trade_paused': bool(state.get('operator_auto_trade_paused')),
        'operator_pause_reason': state.get('operator_pause_reason'),
        'emergency_stop_active': bool(state.get('emergency_stop_active')),
        'emergency_stop_reason': state.get('emergency_stop_reason'),
        'last_operator_action_at': state.get('last_operator_action_at'),
        'last_operator_action': state.get('last_operator_action'),
        'last_operator_action_error': state.get('last_operator_action_error'),
        'automation_blockers': [
            b
            for b in [
                None if config.AUTO_TRADE_ENABLED else 'auto_trade_disabled',
                'operator_auto_trade_paused' if state.get('operator_auto_trade_paused') else None,
                'emergency_stop_active' if state.get('emergency_stop_active') else None,
            ]
            if b
        ],
        'recent_operator_actions': get_recent_operator_actions(),
    }

    auto_cycle_blockers = list(control_state.get('automation_blockers') or [])
    if not (bool(config.PAPER_TRADING_DETECTED) or bool(config.SIMULATION_MODE)):
        auto_cycle_blockers.append('auto_cycle_blocked_not_paper')
    if config.AUTO_CYCLE_REQUIRE_MARKET_OPEN:
        if not within_morning_scan_window():
            auto_cycle_blockers.append('outside_morning_scan_window')
        if not within_auto_scan_window():
            auto_cycle_blockers.append('outside_auto_scan_window')
    auto_cycle_blockers = sorted(set(auto_cycle_blockers))
    auto_cycle_ready = len(auto_cycle_blockers) == 0
    next_action_hint = 'run_auto_cycle' if auto_cycle_ready else f"blocked:{','.join(auto_cycle_blockers)}"

    return ok({
        **state,
        'last_auto_trade_attempts': state.get('last_auto_trade_attempts', []),
        'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons', []),
        'last_auto_trade_verdict': state.get('last_auto_trade_verdict'),
        'control_state': control_state,
        'auto_cycle_ready': auto_cycle_ready,
        'auto_cycle_blockers': auto_cycle_blockers,
        'next_action_hint': next_action_hint,
        'paper_trading_detected': config.PAPER_TRADING_DETECTED,
        'simulation_mode': bool(config.SIMULATION_MODE),
        'broker_backend': 'simulation' if config.SIMULATION_MODE else 'alpaca_paper',
        'simulated_open_orders_count': len(sim_orders),
        'simulated_open_positions_count': len(sim_positions),
        'simulated_account_cash': sim_account.get('cash') if config.SIMULATION_MODE else None,
        'simulated_account_equity': sim_account.get('equity') if config.SIMULATION_MODE else None,
        'db_path': config.DB_PATH,
        'risk_controls': {'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY, 'max_auto_trades_per_day': config.MAX_AUTO_TRADES_PER_DAY},
        'recent_scans': get_recent_scans(),
        'recent_trades': get_recent_trades(),
        'latest_best_pick': (LATEST_SCAN or {}).get('best_pick'),
        'latest_scan_id': (LATEST_SCAN or {}).get('scan_id'),
        'latest_scan_at': state.get('last_scan_at'),
        'attempt_debug': {
            'last_auto_trade_attempts': state.get('last_auto_trade_attempts', []),
            'last_auto_trade_error': state.get('last_auto_trade_error'),
            'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons', []),
            'last_auto_trade_verdict': state.get('last_auto_trade_verdict'),
            'last_scan_skipped_reason': state.get('last_scan_skipped_reason'),
            'scheduler_running': state.get('scheduler_running'),
            'scheduled_jobs': state.get('scheduled_jobs'),
        },
        'config_summary': {
            'AUTO_TRADE_ENABLED': config.AUTO_TRADE_ENABLED,
            'AUTO_SCAN_INTERVAL_SECONDS': config.AUTO_SCAN_INTERVAL_SECONDS,
            'POSITION_MONITOR_INTERVAL_SECONDS': config.POSITION_MONITOR_INTERVAL_SECONDS,
            'MORNING_SCAN_START_ET': config.MORNING_SCAN_START_ET,
            'MORNING_SCAN_END_ET': config.MORNING_SCAN_END_ET,
            'AUTO_SCAN_END_ET': config.AUTO_SCAN_END_ET,
            'NO_BUY_BEFORE_ET': config.NO_BUY_BEFORE_ET,
            'MAX_AUTO_TRADES_PER_DAY': config.MAX_AUTO_TRADES_PER_DAY,
            'MAX_FAILED_TRADES_PER_DAY': config.MAX_FAILED_TRADES_PER_DAY,
            'SCAN_MIN_PRICE': config.SCAN_MIN_PRICE,
            'SCAN_MAX_PRICE': config.SCAN_MAX_PRICE,
            'QUICK_PROFIT_TAKE_PCT': config.QUICK_PROFIT_TAKE_PCT,
            'BREAKEVEN_TRIGGER_PCT': config.BREAKEVEN_TRIGGER_PCT,
            'ACTIVE_PAPER_TRADING_MODE': config.ACTIVE_PAPER_TRADING_MODE,
            'MIN_AUTO_SETUP_GRADE': config.MIN_AUTO_SETUP_GRADE,
            'ALLOW_WATCH_GRADE_AUTO_TRADES': config.ALLOW_WATCH_GRADE_AUTO_TRADES,
            'MIN_MOMENTUM_SCORE_TO_AUTOTRADE': config.MIN_MOMENTUM_SCORE_TO_AUTOTRADE,
            'FALLBACK_ENTRY_ENABLED': config.FALLBACK_ENTRY_ENABLED,
            'FALLBACK_ENTRY_MAX_SPREAD_PCT': config.FALLBACK_ENTRY_MAX_SPREAD_PCT,
            'MAX_DOLLAR_LOSS_PER_TRADE': config.MAX_DOLLAR_LOSS_PER_TRADE,
            'MAX_TRADE_RISK_PCT': config.MAX_TRADE_RISK_PCT,
        },
    })


@app.route('/api/control/pause-auto-trading', methods=['POST'])
def api_control_pause_auto_trading():
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip() or None
    set_operator_pause(True, reason=reason)
    insert_operator_action('pause_auto_trading', reason=reason, success=True, details={'source': 'api_control_pause_auto_trading'})
    return ok({'runtime_state': get_runtime_state()})


@app.route('/api/control/resume-auto-trading', methods=['POST'])
def api_control_resume_auto_trading():
    preflight = run_preflight()
    readiness = preflight.get('auto_trade_readiness') or {}
    blocking = list(readiness.get('blocking_reasons') or [])
    time_window_blockers = {'outside_morning_scan_window', 'buy_window_closed'}
    dangerous_blockers = sorted(set(blocking) - time_window_blockers)
    if not config.AUTO_TRADE_ENABLED:
        dangerous_blockers.append('auto_trade_disabled')
    if (not config.SIMULATION_MODE) and (not config.PAPER_TRADING_DETECTED):
        dangerous_blockers.append('paper_trading_not_detected')
    if RUNTIME_STATE.get('emergency_stop_active'):
        dangerous_blockers.append('emergency_stop_active')
    if dangerous_blockers:
        deduped = sorted(set(dangerous_blockers))
        RUNTIME_STATE['last_operator_action_error'] = ','.join(deduped)
        insert_operator_action('resume_auto_trading', reason=None, success=False, details={'blocking_reasons': deduped, 'preflight': preflight})
        return fail('Resume blocked by safety checks.', 409, details={'blocking_reasons': deduped, 'preflight': preflight})
    set_operator_pause(False)
    insert_operator_action('resume_auto_trading', reason=None, success=True, details={'preflight': preflight})
    return ok({'runtime_state': get_runtime_state(), 'preflight': preflight})


@app.route('/api/control/emergency-stop', methods=['POST'])
def api_control_emergency_stop():
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip() or None
    close_positions = bool(data.get('close_positions', False))
    if (not config.SIMULATION_MODE) and (not config.PAPER_TRADING_DETECTED):
        RUNTIME_STATE['last_operator_action_error'] = 'not_paper_trading'
        result = {'ok': False, 'error': 'Emergency stop cancel/flatten blocked: paper trading not detected.'}
        insert_operator_action('emergency_stop', reason=reason, success=False, details={'close_positions': close_positions, 'result': result})
        return jsonify({'ok': False, 'error': result['error'], 'data': {'result': result, 'runtime_state': get_runtime_state()}}), 409
    result = emergency_cancel_and_flatten(close_positions=close_positions, reason=reason)
    insert_operator_action('emergency_stop', reason=reason, success=bool(result.get('ok')), details={'close_positions': close_positions, 'result': result})
    status = 200 if result.get('ok') else 207
    return jsonify({'ok': bool(result.get('ok')), 'data': {'result': result, 'runtime_state': get_runtime_state()}}), status


@app.route('/api/control/clear-emergency-stop', methods=['POST'])
def api_control_clear_emergency_stop():
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip() or None
    preflight = run_preflight()
    readiness = preflight.get('auto_trade_readiness') or {}
    if not RUNTIME_STATE.get('emergency_stop_active'):
        blocking = ['emergency_stop_not_active']
    else:
        ignored_blockers = {'outside_morning_scan_window', 'buy_window_closed'}
        blocking = sorted(set(readiness.get('blocking_reasons') or []) - ignored_blockers)
    if (not config.SIMULATION_MODE) and (not config.PAPER_TRADING_DETECTED):
        blocking.append('paper_trading_not_detected')
    if blocking:
        insert_operator_action('clear_emergency_stop', reason=reason, success=False, details={'blocking_reasons': sorted(set(blocking)), 'preflight': preflight})
        return fail('Clear emergency stop blocked by safety checks.', 409, details={'blocking_reasons': sorted(set(blocking)), 'preflight': preflight})
    set_emergency_stop(False, reason=reason)
    set_operator_pause(True, reason='manual_pause_after_clear_emergency_stop')
    insert_operator_action('clear_emergency_stop', reason=reason, success=True, details={'source': 'api_control_clear_emergency_stop', 'preflight': preflight, 'operator_paused_after_clear': True})
    return ok({'runtime_state': get_runtime_state(), 'preflight': preflight})



@app.route('/api/control/state', methods=['GET'])
def api_control_state():
    state = get_runtime_state()
    return ok({
        'config_auto_trade_enabled': bool(config.AUTO_TRADE_ENABLED),
        'paper_trading_detected': bool(config.PAPER_TRADING_DETECTED),
        'simulation_mode': bool(config.SIMULATION_MODE),
        'broker_backend': 'simulation' if config.SIMULATION_MODE else 'alpaca_paper',
        'simulated_open_orders_count': len(get_open_orders()) if config.SIMULATION_MODE else 0,
        'simulated_open_positions_count': len(get_open_positions()) if config.SIMULATION_MODE else 0,
        'simulated_account_cash': (get_account().get('cash') if config.SIMULATION_MODE else None),
        'simulated_account_equity': (get_account().get('equity') if config.SIMULATION_MODE else None),
        'trade_stream_required': state.get('trade_stream_required'),
        'trade_stream_skipped_reason': state.get('trade_stream_skipped_reason'),
        'operator_auto_trade_paused': bool(state.get('operator_auto_trade_paused')),
        'operator_pause_reason': state.get('operator_pause_reason'),
        'emergency_stop_active': bool(state.get('emergency_stop_active')),
        'emergency_stop_reason': state.get('emergency_stop_reason'),
        'automation_blockers': [
            b
            for b in [
                None if config.AUTO_TRADE_ENABLED else 'auto_trade_disabled',
                'operator_auto_trade_paused' if state.get('operator_auto_trade_paused') else None,
                'emergency_stop_active' if state.get('emergency_stop_active') else None,
            ]
            if b
        ],
        'recent_operator_actions': get_recent_operator_actions(),
    })

@app.route('/api/runtime-health')
def api_runtime_health():
    websocket_upgrade_header = (request.headers.get('Upgrade') or '').lower()
    return ok(
        {
            'db_path': config.DB_PATH,
            'ws_proxy_hint': 'Ensure proxy forwards Upgrade/Connection headers for /ws/watchlist when using Nginx/Gunicorn.',
            'ws_upgrade_header_seen': websocket_upgrade_header,
        }
    )

@app.route('/api/scan', methods=['POST', 'GET'])
def api_scan():
    global LATEST_SCAN
    try:
        result = run_scan()
        risk_controls = {
            'failed_trades_today': get_failed_trades_today(),
            'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
            'can_trade_today': get_failed_trades_today() < config.MAX_FAILED_TRADES_PER_DAY,
            'buy_window_open': buy_window_open(),
            'no_buy_before_et': config.NO_BUY_BEFORE_ET,
        }
        result['risk_controls'] = risk_controls
        scan_id = insert_scan(result)
        result['scan_id'] = scan_id
        LATEST_SCAN = result
        watchlist_manager.set_items(result.get('watchlist', []))
        return ok(
            result,
            history={'scans': get_recent_scans(), 'trades': get_recent_trades()},
        )
    except ScanError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f'Scan failed: {exc}', 500)




@app.route('/api/preflight', methods=['GET'])
def api_preflight():
    try:
        result = run_preflight()
    except Exception as exc:
        result = {
            'ok': False,
            'overall_status': 'BLOCKED',
            'checks': [
                {
                    'name': 'preflight_exception',
                    'status': 'FAIL',
                    'message': f'Preflight crashed: {exc}',
                }
            ],
            'auto_trade_readiness': {
                'can_auto_trade_now': False,
                'blocking_reasons': ['preflight_exception'],
                'warning_reasons': [],
            },
        }
    return ok({
        'ok': result.get('ok'),
        'overall_status': result.get('overall_status'),
        'checks': result.get('checks', []),
        'auto_trade_readiness': result.get('auto_trade_readiness', {}),
        'simulation_mode': result.get('simulation_mode', bool(config.SIMULATION_MODE)),
        'broker_backend': result.get('broker_backend', 'simulation' if config.SIMULATION_MODE else 'alpaca_paper'),
    })

@app.route('/api/history')
def api_history():
    return ok({'scans': get_recent_scans(), 'trades': get_recent_trades(), 'failed_trades_today': get_failed_trades_today()})


@app.route('/api/chart/<symbol>')
def api_chart(symbol: str):
    try:
        return ok(get_stock_chart_pack(symbol.upper()))
    except Exception as exc:
        return fail(str(exc), 500)


@app.route('/api/execute', methods=['POST'])
def api_execute():
    data = request.get_json(silent=True) or {}
    required = ['symbol', 'entry_price', 'stop_price', 'target_1', 'target_2', 'qty', 'current_price', 'buy_upper', 'score_total', 'decision']
    missing = [k for k in required if k not in data]
    if missing:
        return fail(f'Missing fields: {", ".join(missing)}')
    try:
        verdict = validate_trade_candidate(data, auto=False)
        if not verdict['ok']:
            return fail('Execution blocked.', 403, details={'skip_reasons': verdict['skip_reasons'], 'entry_trigger': verdict['entry_trigger']})
        result = execute_trade_candidate(data, source='manual')
        order = result['order']
        return ok({'trade_id': result['trade_id'], 'order_id': order.get('id'), 'status': order.get('status')}, history={'trades': get_recent_trades()})
    except BrokerError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f'Execution failed: {exc}', 500)


@app.route('/api/order-status/<order_id>')
def api_order_status(order_id: str):
    try:
        trade = get_trade_by_order_id(order_id)
        if not trade:
            return fail('Trade not found for order id.', 404)
        raw = trade.get('raw_json') or '{}'
        if isinstance(raw, str):
            raw = json.loads(raw or '{}')
        bundle = raw.get('order_bundle') if isinstance(raw, dict) else None
        if not isinstance(bundle, dict):
            order = get_order(order_id)
        else:
            order = dict(bundle)
            if bundle.get('strategy') == 'target1_then_trailing_runner':
                bundle = maybe_activate_runner_trailing(bundle, breakeven_price=float(trade.get('entry_price') or 0))
                order['target_1_order'] = get_order(bundle.get('target_1_order_id')) if bundle.get('target_1_order_id') else {}
                if bundle.get('runner_trailing_order_id'):
                    order['runner_trailing_order'] = get_order(bundle.get('runner_trailing_order_id'))
                elif bundle.get('runner_stop_order_id'):
                    order['runner_order'] = get_order(bundle.get('runner_stop_order_id'))
                raw['order_bundle'] = bundle
        updates = {
            'order_status': order.get('status'),
            'filled_avg_price': order.get('filled_avg_price'),
            'filled_qty': order.get('filled_qty'),
            'outcome': order_outcome_from_payload(order),
            'raw_json': raw if isinstance(raw, dict) else order,
        }
        update_trade_status(order_id, updates)
        order['risk_controls'] = {
            'failed_trades_today': get_failed_trades_today(),
            'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
            'can_trade_today': get_failed_trades_today() < config.MAX_FAILED_TRADES_PER_DAY,
            'buy_window_open': buy_window_open(),
            'no_buy_before_et': config.NO_BUY_BEFORE_ET,
        }
        return ok(order, history={'trades': get_recent_trades(), 'failed_trades_today': get_failed_trades_today()})
    except BrokerError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f'Order lookup failed: {exc}', 500)


@sock.route('/ws/watchlist')
def ws_watchlist(ws):
    try:
        watchlist_manager.stream(ws)
    except Exception:
        return


if config.AUTO_START_EXECUTION_ENGINE:
    start_execution_engine(auto_scan_callback=run_scan_and_maybe_auto_trade)


if __name__ == '__main__':
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)
