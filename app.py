import json
import logging
import os
import sqlite3
from collections import Counter
from copy import deepcopy

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from flask_sock import Sock

import config
from broker_facade import BrokerError, get_order, maybe_activate_runner_trailing, get_open_orders, get_open_positions, get_account, get_clock
import db
from db import count_trades_today, estimated_daily_loss_risk_used_today, get_failed_trades_today, get_recent_operator_actions, get_recent_scans, get_recent_trades, get_trade_by_order_id, init_db, insert_operator_action, insert_scan, update_trade_status
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
from preflight import run_paper_trade_readiness_preflight, run_preflight

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


@app.errorhandler(Exception)
def handle_api_exceptions(exc):
    if not request.path.startswith('/api/'):
        raise exc
    status = 500
    error_text = str(exc) or 'internal_server_error'
    error_type = exc.__class__.__name__
    if isinstance(exc, HTTPException):
        status = exc.code or 500
        error_text = exc.description or error_text
    return jsonify({
        'ok': False,
        'error': error_text,
        'error_type': error_type,
        'path': request.path,
    }), status


def compact_auto_cycle_payload(state: dict) -> dict:
    attempts = []
    for attempt in (state.get('last_auto_trade_attempts') or [])[:3]:
        attempts.append({
            'symbol': attempt.get('symbol'),
            'ok': bool(attempt.get('ok')),
            'probe_trade': bool(attempt.get('probe_trade')),
            'skip_reasons': (attempt.get('skip_reasons') or [])[:5],
            'error': attempt.get('error'),
            'score_total': attempt.get('score_total'),
            'setup_grade': attempt.get('setup_grade'),
            'qty': attempt.get('qty') or attempt.get('probe_qty'),
        })

    latest_scan = LATEST_SCAN or {}
    best_pick = latest_scan.get('best_pick') or {}
    scan_diag = (latest_scan.get('scan_diagnostics') or {})
    scan_preview = {
        'scan_id': latest_scan.get('scan_id'),
        'scan_diagnostics': {
            'broad_universe_count': scan_diag.get('broad_universe_count'),
            'deep_analysis_count': scan_diag.get('deep_analysis_count'),
            'symbols_analyzed_count': scan_diag.get('symbols_analyzed_count'),
        },
        'best_pick': {
            'symbol': best_pick.get('symbol'),
            'decision': best_pick.get('decision'),
            'score_total': best_pick.get('score_total'),
            'entry_price': best_pick.get('entry_price'),
            'stop_price': best_pick.get('stop_price'),
            'qty': best_pick.get('qty'),
            'skip_reasons': best_pick.get('skip_reasons') or [],
        } if best_pick else {},
    }

    return {
        'runtime_state': {
            'last_scan_at': state.get('last_scan_at'),
            'last_scan_error': state.get('last_scan_error'),
            'last_auto_trade_at': state.get('last_auto_trade_at'),
            'last_auto_trade_error': state.get('last_auto_trade_error'),
            'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons') or [],
            'last_scan_skipped_reason': state.get('last_scan_skipped_reason'),
            'attempted_candidate_count': len(state.get('last_auto_trade_attempts') or []),
            'blocker_counts': state.get('last_auto_trade_blocker_counts') or {},
        },
        'latest_scan': scan_preview,
        'last_auto_trade_attempts': attempts,
        'last_auto_trade_error': state.get('last_auto_trade_error'),
        'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons') or [],
        'last_auto_trade_verdict': state.get('last_auto_trade_verdict') or {},
    }

def market_open_for_auto_cycle() -> tuple[bool, str]:
    if config.SIMULATION_MODE or not config.AUTO_CYCLE_REQUIRE_MARKET_OPEN:
        return True, 'market_open_not_required'
    try:
        clock = get_clock() or {}
        if not bool(clock.get('is_open')):
            return False, 'market_closed'
    except Exception as exc:
        return False, f'market_clock_unavailable:{exc}'
    if not within_morning_scan_window():
        return False, 'outside_morning_scan_window'
    if not within_auto_scan_window():
        return False, 'outside_auto_scan_window'
    return True, 'market_open'

def run_scan_and_maybe_auto_trade():
    global LATEST_SCAN
    market_open, market_reason = market_open_for_auto_cycle()
    if not market_open:
        RUNTIME_STATE['last_scan_skipped_reason'] = market_reason
        RUNTIME_STATE['last_auto_trade_error'] = market_reason
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = [market_reason]
        RUNTIME_STATE['last_auto_trade_attempts'] = []
        RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': [market_reason]}
        logger.info('Auto scan skipped: %s.', market_reason)
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
        plan = build_auto_trade_candidate_plan(result, scan_id=scan_id)
        RUNTIME_STATE['last_auto_cycle_plan'] = {
            'candidate_count': plan.get('candidate_count', 0),
            'executable_count': plan.get('executable_count', 0),
            'probe_eligible_count': plan.get('probe_eligible_count', 0),
            'blocked_count': plan.get('blocked_count', 0),
            'candidate_symbols': (plan.get('candidate_symbols') or [])[:8],
            'top_blockers': plan.get('top_blockers') or {},
        }
        RUNTIME_STATE['last_auto_cycle_plan_at'] = now_et().isoformat()
        RUNTIME_STATE['last_auto_cycle_plan_error'] = None
        attempts, all_reasons = [], set()
        blocker_counts = {}
        RUNTIME_STATE['last_auto_trade_error'] = None
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = []
        RUNTIME_STATE['last_auto_trade_verdict'] = None
        executed = False
        if not plan.get('attempt_plan'):
            RUNTIME_STATE['last_auto_trade_error'] = 'no_candidates'
            RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['no_candidates']
            RUNTIME_STATE['last_auto_trade_attempts'] = []
            RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': ['no_candidates']}
            return
        for item in plan.get('attempt_plan', []):
            candidate = dict(item.get('candidate') or {})
            verdict = dict(item.get('verdict') or {})
            attempts.append({
                'symbol': candidate.get('symbol'),
                'ok': verdict.get('ok'),
                'entry_trigger': verdict.get('entry_trigger'),
                'fallback_used': verdict.get('fallback_used'),
                'risk_dollars': verdict.get('risk_dollars', candidate.get('risk_dollars') or candidate.get('max_dollar_loss')),
                'skip_reasons': verdict.get('skip_reasons', []),
                'fallback_reasons': verdict.get('fallback_reasons', []),
                'probe_trade': verdict.get('probe_trade', False),
                'probe_trade_ok': verdict.get('probe_trade_ok', False),
                'probe_reasons': sorted(set((verdict.get('probe_reasons', []) or []) + [f"soft_overridden:{r}" for r in (verdict.get('soft_blockers_overridden', []) or [])] + [f"hard_overridden:{r}" for r in (verdict.get('hard_blockers_overridden', []) or [])])),
                'probe_qty': verdict.get('probe_qty'),
                'probe_risk_dollars': verdict.get('probe_risk_dollars'),
                'probe_qty_from_zero': verdict.get('probe_qty_from_zero', False),
                'soft_blockers_overridden': verdict.get('soft_blockers_overridden', []),
                'hard_blockers_overridden': verdict.get('hard_blockers_overridden', []),
                'overridden_blockers': sorted(set((verdict.get('soft_blockers_overridden', []) or []) + (verdict.get('hard_blockers_overridden', []) or []))),
                'true_hard_rejects': sorted([r for r in (verdict.get('skip_reasons', []) or []) if str(r).startswith('hard_reject_reason_')]),
                'overridable_rejects': sorted([str(r).replace('overridable_reject_', '') for r in (verdict.get('skip_reasons', []) or []) if str(r).startswith('overridable_reject_')]),
                'score_total': candidate.get('score_total'),
                'setup_grade': candidate.get('setup_grade'),
                'error': None,
            })
            all_reasons.update(verdict.get('skip_reasons', []))
            for r in (verdict.get('skip_reasons', []) or []): blocker_counts[r] = blocker_counts.get(r,0)+1
            all_reasons.update(verdict.get('probe_reasons', []))
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
                    filled_qty = trade_order.get('filled_qty')
                    governed_qty = verdict.get('first_trade_final_qty') or item.get('final_qty') or candidate.get('qty')
                    qty_value = filled_qty if filled_qty is not None else governed_qty
                    attempts[-1]['qty'] = int(qty_value or 0)
                    executed = True
                    break
                except Exception as exc:
                    attempts[-1]['error'] = str(exc)
                    RUNTIME_STATE['last_auto_trade_error'] = str(exc)
                    RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['execution_failed']
        RUNTIME_STATE['last_auto_trade_attempts'] = attempts
        RUNTIME_STATE['last_auto_trade_blocker_counts'] = blocker_counts
        if not executed:
            execution_errors = sorted(set([a.get('error') for a in attempts if a.get('error')]))
            combined_reasons = sorted(set(list(all_reasons) + (['execution_failed'] if execution_errors else [])))
            RUNTIME_STATE['last_auto_trade_error'] = ';'.join(execution_errors) if execution_errors else 'no_executable_candidate'
            RUNTIME_STATE['last_auto_trade_skip_reasons'] = combined_reasons
            RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': combined_reasons, 'execution_errors': execution_errors}
    except Exception as exc:
        RUNTIME_STATE['last_scan_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['auto_cycle_exception']
        RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': ['auto_cycle_exception']}
        RUNTIME_STATE['last_auto_cycle_plan_error'] = str(exc)


def build_auto_trade_candidate_plan(scan_result: dict, scan_id: int | None = None, external_exposure_checks: bool = True) -> dict:
    ranked = []
    if scan_result.get('best_pick'):
        ranked.append(scan_result['best_pick'])
    ranked.extend(scan_result.get('watchlist', []))
    seen, candidates = set(), []
    for c in ranked:
        sym = (c or {}).get('symbol')
        if sym and sym not in seen:
            seen.add(sym)
            candidates.append(c)
    limited = candidates[:max(1, config.AUTO_TRADE_CANDIDATE_LIMIT)]
    attempt_plan, blockers = [], []
    for raw in limited:
        candidate = deepcopy(raw or {})
        if scan_id is not None:
            candidate['scan_id'] = scan_id
        try:
            verdict = validate_trade_candidate(candidate, auto=True, external_exposure_checks=external_exposure_checks)
        except TypeError:
            verdict = validate_trade_candidate(candidate, auto=True)
        skips = verdict.get('skip_reasons', []) or []
        blockers.extend(skips)
        attempt_plan.append({
            'symbol': candidate.get('symbol'),
            'ok': verdict.get('ok', False),
            'probe_trade': verdict.get('probe_trade', False),
            'setup_grade': candidate.get('setup_grade'),
            'score_total': candidate.get('score_total'),
            'entry_trigger': verdict.get('entry_trigger'),
            'risk_dollars': verdict.get('risk_dollars', candidate.get('risk_dollars') or candidate.get('max_dollar_loss')),
            'skip_reasons': skips,
            'probe_reasons': verdict.get('probe_reasons', []) or [],
            'soft_blockers_overridden': verdict.get('soft_blockers_overridden', []) or [],
            'hard_blockers_overridden': verdict.get('hard_blockers_overridden', []) or [],
            'first_trade_governor_applied': verdict.get('first_trade_governor_applied', False),
            'first_trade_original_qty': verdict.get('first_trade_original_qty'),
            'first_trade_final_qty': verdict.get('first_trade_final_qty'),
            'first_trade_risk_dollars': verdict.get('first_trade_risk_dollars'),
            'first_trade_blocked_reason': verdict.get('first_trade_blocked_reason') or ('first_trade_risk_too_high' if 'first_trade_risk_too_high' in skips else None),
            'final_qty': verdict.get('first_trade_final_qty') or candidate.get('qty'),
            'final_risk_dollars': verdict.get('first_trade_risk_dollars', verdict.get('risk_dollars', candidate.get('risk_dollars') or candidate.get('max_dollar_loss'))),
            'candidate': candidate,
            'verdict': verdict,
        })
    top_blockers = dict(Counter(blockers).most_common(8))
    executable_count = len([a for a in attempt_plan if a.get('ok')])
    probe_eligible_count = len([a for a in attempt_plan if a.get('probe_trade')])
    return {
        'candidate_count': len(limited),
        'validated_count': len(attempt_plan),
        'executable_count': executable_count,
        'probe_eligible_count': probe_eligible_count,
        'blocked_count': max(0, len(attempt_plan) - executable_count),
        'candidate_symbols': [c.get('symbol') for c in limited if c.get('symbol')],
        'top_blockers': top_blockers,
        'attempt_plan': attempt_plan,
    }


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

    return ok(compact_auto_cycle_payload(state))


@app.route('/api/auto-cycle-plan', methods=['POST'])
def api_auto_cycle_plan():
    payload = run_auto_cycle_plan_no_order(include_live_scan=True)
    if payload.get('status') == 'failed' and 'auto_cycle_blocked_not_paper' in ((payload.get('candidate_plan') or {}).get('blockers') or []):
        return fail('auto_cycle_blocked_not_paper', 409)
    if payload.get('status') == 'failed':
        return fail('auto_cycle_plan_failed', 500)
    return ok(payload)


def run_auto_cycle_plan_no_order(include_live_scan: bool = True) -> dict:
    if not (bool(config.PAPER_TRADING_DETECTED) or bool(config.SIMULATION_MODE)):
        return {'scan_summary': {}, 'candidate_plan': {'blocked': True, 'blockers': ['auto_cycle_blocked_not_paper']}, 'status': 'failed'}
    if not include_live_scan:
        return {'scan_summary': {}, 'candidate_plan': {'blocked': True, 'blockers': ['live_scan_disabled']}, 'status': 'not_run'}
    market_open, market_reason = market_open_for_auto_cycle()
    if not market_open:
        blocked_reason = 'outside_auto_scan_window' if 'outside' in market_reason else 'market_closed'
        plan = {'blocked': True, 'blockers': [blocked_reason], 'market_reason': market_reason}
        RUNTIME_STATE['last_auto_cycle_plan'] = plan
        RUNTIME_STATE['last_auto_cycle_plan_at'] = now_et().isoformat()
        RUNTIME_STATE['last_auto_cycle_plan_error'] = None
        return {'scan_summary': {}, 'candidate_plan': plan, 'status': 'blocked_market_closed'}
    try:
        result = run_scan()
        scan_id = insert_scan(result)
        result['scan_id'] = scan_id
        watchlist_manager.set_items(result.get('watchlist', []))
        plan = build_auto_trade_candidate_plan(result, scan_id=scan_id)
        compact = {k: v for k, v in plan.items() if k != 'attempt_plan'}
        compact['attempt_plan'] = [{k: v for k, v in a.items() if k not in {'candidate', 'verdict'}} for a in plan.get('attempt_plan', [])]
        RUNTIME_STATE['last_auto_cycle_plan'] = {
            'candidate_count': compact.get('candidate_count', 0),
            'executable_count': compact.get('executable_count', 0),
            'probe_eligible_count': compact.get('probe_eligible_count', 0),
            'blocked_count': compact.get('blocked_count', 0),
            'candidate_symbols': (compact.get('candidate_symbols') or [])[:8],
            'top_blockers': compact.get('top_blockers') or {},
        }
        RUNTIME_STATE['last_auto_cycle_plan_at'] = now_et().isoformat()
        RUNTIME_STATE['last_auto_cycle_plan_error'] = None
        return {'scan_summary': {'scan_id': scan_id, 'best_pick': (result.get('best_pick') or {}).get('symbol')}, 'candidate_plan': compact, 'status': 'PASS' if int(compact.get('executable_count') or 0) > 0 else 'WARN'}
    except Exception as exc:
        RUNTIME_STATE['last_auto_cycle_plan_error'] = str(exc)
        return {'scan_summary': {}, 'candidate_plan': {'blocked': True, 'blockers': ['auto_cycle_plan_failed']}, 'status': 'failed'}


@app.route('/api/market-open-rehearsal', methods=['POST'])
def api_market_open_rehearsal():
    return ok(run_market_open_rehearsal_plan())


def run_market_open_rehearsal_plan(symbol: str | None = None, allow_live_scan: bool = True) -> dict:
    market_open, market_reason = market_open_for_auto_cycle()
    state = get_runtime_state()
    scheduler_status = {
        'scheduler_running': bool(state.get('scheduler_running')),
        'auto_scan_job_registered': bool(state.get('auto_scan_job_registered')),
        'position_monitor_job_registered': bool(state.get('position_monitor_job_registered')),
        'flatten_job_registered': bool(state.get('flatten_job_registered')),
        'scheduled_jobs': state.get('scheduled_jobs') or [],
    }
    paper_or_sim_ok = bool(config.PAPER_TRADING_DETECTED) or bool(config.SIMULATION_MODE)
    blocking_reasons = []
    if not paper_or_sim_ok:
        blocking_reasons.append('auto_cycle_blocked_not_paper')
    if not scheduler_status['scheduler_running']:
        blocking_reasons.append('scheduler_not_running')
    elif not scheduler_status['auto_scan_job_registered']:
        blocking_reasons.append('auto_scan_job_not_registered')
    requires_open = not (config.SIMULATION_MODE or not config.AUTO_CYCLE_REQUIRE_MARKET_OPEN)
    blocked_market_closed = (not market_open) and requires_open
    if blocked_market_closed:
        blocking_reasons.append('market_closed')
    candidate_plan = {'blocked': True, 'blockers': list(blocking_reasons), 'market_reason': market_reason}
    first_candidate = None
    first_trade_governor = {}
    would_attempt_trade = False
    would_probe_trade = False
    try:
        if not blocking_reasons and allow_live_scan:
            result = run_scan()
            scan_id = insert_scan(result)
            result['scan_id'] = scan_id
            watchlist_manager.set_items(result.get('watchlist', []))
            plan = build_auto_trade_candidate_plan(result, scan_id=scan_id)
            candidate_plan = {k: v for k, v in plan.items() if k != 'attempt_plan'}
            candidate_plan['attempt_plan'] = [{k: v for k, v in a.items() if k not in {'candidate', 'verdict'}} for a in plan.get('attempt_plan', [])]
            first = next((a for a in plan.get('attempt_plan', []) if a.get('ok')), None)
            if first:
                first_candidate = {'symbol': first.get('symbol'), 'probe_trade': bool(first.get('probe_trade')), 'qty': (first.get('first_trade_final_qty') or (first.get('candidate') or {}).get('qty'))}
                first_trade_governor = {
                    'first_trade_governor_applied': bool(first.get('first_trade_governor_applied')),
                    'first_trade_original_qty': first.get('first_trade_original_qty'),
                    'first_trade_final_qty': first.get('first_trade_final_qty'),
                    'first_trade_risk_dollars': first.get('first_trade_risk_dollars'),
                    'first_trade_blocked_reason': first.get('first_trade_blocked_reason'),
                }
                would_attempt_trade = True
                would_probe_trade = bool(first.get('probe_trade'))
            else:
                blocking_reasons.append('no_executable_candidate')
        elif not blocked_market_closed and not allow_live_scan and market_open and requires_open:
            blocking_reasons.append('live_scan_disabled')
    except Exception as exc:
        payload = {'status': 'failed', 'blocking_reasons': ['market_open_rehearsal_failed'], 'would_attempt_trade': False, 'next_action_hint': 'review_market_open_rehearsal'}
        RUNTIME_STATE['last_market_open_rehearsal'] = payload
        RUNTIME_STATE['last_market_open_rehearsal_at'] = now_et().isoformat()
        RUNTIME_STATE['last_market_open_rehearsal_error'] = str(exc)
        return payload
    status = 'PASS' if would_attempt_trade and not blocking_reasons else 'WARN'
    if blocked_market_closed:
        status = 'blocked_market_closed'
    elif (not allow_live_scan) and market_open and requires_open:
        status = 'not_run_live_scan_disabled'
    next_action_hint = 'ready_for_auto_cycle' if would_attempt_trade and not blocking_reasons else ('wait_for_market_open' if blocked_market_closed else ('run_auto_cycle_plan' if status == 'not_run_live_scan_disabled' else ('review_scan_diagnostics' if candidate_plan.get('candidate_count', 0) > 0 else 'review_market_open_rehearsal')))
    payload = {
        'market_status': {'market_open_for_auto_cycle': market_open, 'market_reason': market_reason},
        'scheduler_status': scheduler_status,
        'paper_or_sim_ok': paper_or_sim_ok,
        'candidate_plan': candidate_plan,
        'first_executable_candidate': first_candidate,
        'first_trade_governor': first_trade_governor,
        'would_attempt_trade': would_attempt_trade,
        'would_probe_trade': would_probe_trade,
        'blocking_reasons': sorted(set(blocking_reasons)),
        'next_action_hint': next_action_hint,
        'status': status,
    }
    RUNTIME_STATE['last_market_open_rehearsal'] = payload
    RUNTIME_STATE['last_market_open_rehearsal_at'] = now_et().isoformat()
    RUNTIME_STATE['last_market_open_rehearsal_error'] = None
    return payload


def build_synthetic_rehearsal_scan(symbol: str = "TEST") -> dict:
    score_total = max(int(config.PROBE_MIN_SCORE) + 5, int(config.MIN_MOMENTUM_SCORE_TO_AUTOTRADE))
    candidate = {
        'symbol': (symbol or 'TEST').upper(),
        'setup_grade': 'WATCH',
        'decision': 'WATCH FOR BREAKOUT',
        'score_total': score_total,
        'current_price': 10.00,
        'entry_price': 10.00,
        'stop_price': 9.00,
        'target_1': 10.50,
        'target_2': 11.00,
        'buy_lower': 9.90,
        'buy_upper': 10.10,
        'qty': 20,
        'hard_reject_reasons': [],
        'why_not_buying': [],
        'details': {'spread_pct': 0.001, 'momentum_continuation': True, 'entry_trigger': 'MOMENTUM_CONTINUATION'},
    }
    return {'best_pick': candidate, 'watchlist': []}


def run_synthetic_auto_cycle_rehearsal(symbol: str | None = None) -> dict:
    result = build_synthetic_rehearsal_scan(symbol or 'TEST')
    plan = build_auto_trade_candidate_plan(result, external_exposure_checks=False)
    first = next((a for a in plan.get('attempt_plan', []) if a.get('ok')), None)
    first_candidate_symbol = (first or {}).get('symbol') or ((plan.get('attempt_plan') or [{}])[0].get('symbol'))
    blocking_reasons = sorted(set(([] if first else ['no_executable_candidate']) + list(plan.get('top_blockers', {}).keys())))
    payload = {
        'offline_synthetic_external_checks_skipped': True,
        'skipped_checks': ['duplicate_broker_exposure_lookup'],
        'candidate_count': plan.get('candidate_count', 0),
        'executable_count': plan.get('executable_count', 0),
        'first_candidate_symbol': first_candidate_symbol,
        'first_trade_governor_applied': bool((first or {}).get('first_trade_governor_applied')),
        'first_trade_original_qty': (first or {}).get('first_trade_original_qty'),
        'first_trade_final_qty': (first or {}).get('first_trade_final_qty'),
        'first_trade_risk_dollars': (first or {}).get('first_trade_risk_dollars'),
        'final_qty': (first or {}).get('final_qty'),
        'final_risk_dollars': (first or {}).get('final_risk_dollars'),
        'would_attempt_trade': bool(first),
        'would_probe_trade': bool((first or {}).get('probe_trade')),
        'blocking_reasons': blocking_reasons,
        'next_action_hint': 'ready_for_auto_cycle' if first else 'review_scan_diagnostics',
    }
    RUNTIME_STATE['last_synthetic_rehearsal'] = payload
    RUNTIME_STATE['last_synthetic_rehearsal_at'] = now_et().isoformat()
    RUNTIME_STATE['last_synthetic_rehearsal_error'] = None
    return payload


@app.route('/api/synthetic-auto-cycle-rehearsal', methods=['POST'])
def api_synthetic_auto_cycle_rehearsal():
    data = request.get_json(silent=True) or {}
    symbol = (data.get('symbol') or '').strip() or None
    return ok(run_synthetic_auto_cycle_rehearsal(symbol))


def build_deployment_checklist(state: dict | None = None) -> dict:
    state = state or get_runtime_state()
    preflight = state.get('last_paper_readiness_preflight') or {}
    plan = state.get('last_auto_cycle_plan') or {}
    market_rehearsal = state.get('last_market_open_rehearsal') or {}
    synthetic = state.get('last_synthetic_rehearsal') or {}
    payload = {
        'paper_readiness_preflight_recent': bool(state.get('last_paper_readiness_preflight_at')),
        'paper_readiness_ok': bool(preflight.get('ok')),
        'auto_cycle_plan_recent': bool(state.get('last_auto_cycle_plan_at')),
        'auto_cycle_plan_executable': int(plan.get('executable_count') or 0) > 0,
        'market_open_rehearsal_recent': bool(state.get('last_market_open_rehearsal_at')),
        'market_open_rehearsal_would_attempt': bool(market_rehearsal.get('would_attempt_trade')),
        'synthetic_rehearsal_recent': bool(state.get('last_synthetic_rehearsal_at')),
        'synthetic_rehearsal_would_attempt': bool(synthetic.get('would_attempt_trade')),
        'scheduler_running': bool(state.get('scheduler_running')),
        'auto_scan_job_registered': bool(state.get('auto_scan_job_registered')),
        'first_trade_governor_enabled': bool(config.FIRST_TRADE_GOVERNOR_ENABLED),
        'emergency_stop_clear': not bool(state.get('emergency_stop_active')),
        'operator_pause_clear': not bool(state.get('operator_auto_trade_paused')),
    }
    if not payload['paper_readiness_preflight_recent']:
        next_action = 'run_paper_readiness_preflight'
    elif not payload['paper_readiness_ok']:
        next_action = 'review_paper_readiness_preflight'
    elif not payload['auto_cycle_plan_recent']:
        next_action = 'run_auto_cycle_plan'
    elif not payload['auto_cycle_plan_executable']:
        next_action = 'review_scan_diagnostics'
    elif not payload['market_open_rehearsal_recent']:
        next_action = 'run_market_open_rehearsal'
    elif not payload['market_open_rehearsal_would_attempt']:
        next_action = 'review_market_open_rehearsal'
    elif not payload['synthetic_rehearsal_recent']:
        next_action = 'run_synthetic_rehearsal'
    elif not payload['synthetic_rehearsal_would_attempt']:
        next_action = 'review_synthetic_rehearsal'
    elif not payload['scheduler_running'] or not payload['auto_scan_job_registered']:
        next_action = 'start_scheduler'
    elif not payload['emergency_stop_clear']:
        next_action = 'clear_emergency_stop'
    elif not payload['operator_pause_clear']:
        next_action = 'resume_auto_trading'
    else:
        next_action = 'ready_for_market_open'
    payload['next_required_action'] = next_action
    return payload


def run_pre_market_readiness_pipeline(symbol: str | None = None, include_live_scan_plan: bool = False) -> dict:
    symbol = (symbol or config.PREFLIGHT_SYMBOL or 'TEST').upper()
    steps = []
    paper = run_paper_trade_readiness_preflight(symbol)
    RUNTIME_STATE['last_paper_readiness_preflight'] = {'ok': bool(paper.get('ok')), 'overall_status': paper.get('overall_status'), 'next_action_hint': paper.get('next_action_hint'), 'blocking_reasons': paper.get('blocking_reasons', []), 'warning_reasons': paper.get('warning_reasons', []), 'symbol': paper.get('symbol') or symbol}
    RUNTIME_STATE['last_paper_readiness_preflight_at'] = now_et().isoformat()
    RUNTIME_STATE['last_paper_readiness_preflight_error'] = None
    paper_ok = bool(paper.get('ok'))
    steps.append({'name': 'paper_readiness_preflight', 'ok': paper_ok, 'status': paper.get('overall_status', 'FAIL'), 'next_action_hint': paper.get('next_action_hint'), 'blocking_reasons': paper.get('blocking_reasons', []), 'warning_reasons': paper.get('warning_reasons', []), 'metrics': {'checks': len(paper.get('checks') or []), 'symbol': paper.get('symbol')}})

    synthetic = run_synthetic_auto_cycle_rehearsal(symbol)
    synthetic_ok = bool(synthetic.get('would_attempt_trade'))
    steps.append({'name': 'synthetic_auto_cycle_rehearsal', 'ok': synthetic_ok, 'status': 'PASS' if synthetic_ok else 'FAIL', 'next_action_hint': synthetic.get('next_action_hint'), 'blocking_reasons': synthetic.get('blocking_reasons', []), 'warning_reasons': [], 'metrics': {'first_trade_governor_applied': synthetic.get('first_trade_governor_applied'), 'first_trade_final_qty': synthetic.get('first_trade_final_qty')}})

    state = get_runtime_state()
    checklist = build_deployment_checklist(state)
    steps.append({'name': 'deployment_checklist', 'ok': checklist.get('next_required_action') == 'ready_for_market_open', 'status': 'PASS' if checklist.get('next_required_action') == 'ready_for_market_open' else 'WARN', 'next_action_hint': checklist.get('next_required_action'), 'blocking_reasons': [], 'warning_reasons': [], 'metrics': {'scheduler_running': checklist.get('scheduler_running'), 'auto_scan_job_registered': checklist.get('auto_scan_job_registered')}})

    mr = run_market_open_rehearsal_plan(symbol=symbol, allow_live_scan=include_live_scan_plan)
    market_step = {'name': 'market_open_rehearsal', 'ok': bool(mr.get('would_attempt_trade')), 'status': mr.get('status', 'failed'), 'next_action_hint': mr.get('next_action_hint'), 'blocking_reasons': mr.get('blocking_reasons', []), 'warning_reasons': [], 'metrics': {'would_attempt_trade': mr.get('would_attempt_trade'), 'market_reason': ((mr.get('market_status') or {}).get('market_reason'))}}
    steps.append(market_step)

    auto_cycle_plan_status = 'not_run'
    if include_live_scan_plan:
        cp = run_auto_cycle_plan_no_order(include_live_scan=True)
        candidate_plan = cp.get('candidate_plan') or {}
        auto_cycle_plan_status = cp.get('status') or ('PASS' if int(candidate_plan.get('executable_count') or 0) > 0 else 'WARN')
        steps.append({'name': 'auto_cycle_plan', 'ok': auto_cycle_plan_status == 'PASS', 'status': auto_cycle_plan_status, 'next_action_hint': 'review_scan_diagnostics' if auto_cycle_plan_status != 'PASS' else 'ready_for_auto_cycle', 'blocking_reasons': candidate_plan.get('blockers', []), 'warning_reasons': [], 'metrics': {'executable_count': candidate_plan.get('executable_count', 0)}})
    else:
        steps.append({'name': 'auto_cycle_plan', 'ok': True, 'status': 'not_run', 'next_action_hint': 'run_auto_cycle_plan', 'blocking_reasons': [], 'warning_reasons': [], 'metrics': {'executable_count': None}})

    first_trade_qty = int(synthetic.get('first_trade_final_qty') or 0)
    first_trade_risk = float(synthetic.get('first_trade_risk_dollars') or 0)
    first_trade_ok = all([bool(synthetic.get('would_attempt_trade')), bool(synthetic.get('first_trade_governor_applied')), 1 <= first_trade_qty <= int(config.FIRST_TRADE_MAX_QTY), 0 < first_trade_risk <= float(config.FIRST_TRADE_MAX_DOLLAR_RISK)])
    safe_enable = all([paper_ok, synthetic_ok, first_trade_ok, checklist.get('emergency_stop_clear'), checklist.get('operator_pause_clear'), checklist.get('scheduler_running'), checklist.get('auto_scan_job_registered')])
    timing_only_block = market_step.get('status') in {'blocked_market_closed', 'outside_auto_scan_window'}
    safe_manual = safe_enable and (bool(mr.get('would_attempt_trade')) or timing_only_block)
    next_action = checklist.get('next_required_action')
    if not paper_ok:
        next_action = 'review_paper_readiness_preflight'
    elif not synthetic_ok:
        next_action = 'review_synthetic_rehearsal'
    elif not checklist.get('emergency_stop_clear'):
        next_action = 'clear_emergency_stop'
    elif not checklist.get('operator_pause_clear'):
        next_action = 'resume_auto_trading'
    elif not checklist.get('scheduler_running') or not checklist.get('auto_scan_job_registered'):
        next_action = 'start_scheduler'
    elif not first_trade_ok:
        next_action = 'review_synthetic_rehearsal'
    elif market_step.get('status') in {'failed', 'WARN'} and not timing_only_block:
        next_action = 'review_market_open_rehearsal'
    elif timing_only_block:
        next_action = 'wait_for_market_open'
    elif market_step.get('status') == 'not_run_live_scan_disabled':
        next_action = 'run_auto_cycle_plan'
    elif not safe_enable:
        next_action = 'review_synthetic_rehearsal'
    else:
        next_action = 'ready_for_market_open'
    overall = 'PASS' if safe_enable and not timing_only_block else ('WARN' if safe_enable or timing_only_block else 'FAIL')
    payload = {'ok': overall != 'FAIL', 'overall_status': overall, 'symbol': symbol, 'steps': steps, 'deployment_checklist': checklist, 'go_no_go': 'GO' if safe_enable else 'NO_GO', 'next_required_action': next_action, 'safe_to_enable_auto_cycle': bool(safe_enable), 'safe_to_run_manual_auto_cycle': bool(safe_manual), 'offline_only': not include_live_scan_plan, 'include_live_scan_plan': bool(include_live_scan_plan), 'market_open_rehearsal_status': market_step.get('status'), 'auto_cycle_plan_status': auto_cycle_plan_status}
    RUNTIME_STATE['last_pre_market_readiness_pipeline'] = {'overall_status': overall, 'go_no_go': payload['go_no_go'], 'next_required_action': next_action, 'safe_to_enable_auto_cycle': payload['safe_to_enable_auto_cycle'], 'safe_to_run_manual_auto_cycle': payload['safe_to_run_manual_auto_cycle'], 'offline_only': payload['offline_only'], 'symbol': symbol}
    RUNTIME_STATE['last_pre_market_readiness_pipeline_at'] = now_et().isoformat()
    RUNTIME_STATE['last_pre_market_readiness_pipeline_error'] = None
    return payload


@app.route('/api/deployment-checklist', methods=['GET'])
def api_deployment_checklist():
    return ok(build_deployment_checklist())


def _operator_runbook_environment_summary() -> dict:
    broker_backend = 'simulation' if bool(config.SIMULATION_MODE) else ('alpaca_paper' if bool(config.PAPER_TRADING_DETECTED) else 'blocked_non_paper')
    return {
        'simulation_mode': bool(config.SIMULATION_MODE),
        'paper_trading_detected': bool(config.PAPER_TRADING_DETECTED),
        'auto_trade_enabled': bool(config.AUTO_TRADE_ENABLED),
        'active_paper_trading_mode': bool(config.SIMULATION_MODE) or bool(config.PAPER_TRADING_DETECTED),
        'first_trade_governor_enabled': bool(getattr(config, 'FIRST_TRADE_GOVERNOR_ENABLED', True)),
        'first_trade_max_qty': int(config.FIRST_TRADE_MAX_QTY),
        'first_trade_max_dollar_risk': float(config.FIRST_TRADE_MAX_DOLLAR_RISK),
        'max_auto_trades_per_day': int(config.MAX_AUTO_TRADES_PER_DAY),
        'auto_scan_interval_seconds': int(config.AUTO_SCAN_INTERVAL_SECONDS),
        'hard_exit_time_et': str(config.HARD_EXIT_TIME_ET),
        'broker_backend': broker_backend,
    }


def _operator_runbook_phases() -> list[dict]:
    return [
        {'name': 'pre_open_no_order', 'purpose': 'Run offline/paper readiness checks before market open without placing orders.', 'commands': [{'method': 'GET', 'path': '/api/bot-status'}, {'method': 'POST', 'path': '/api/paper-readiness-preflight'}, {'method': 'POST', 'path': '/api/synthetic-auto-cycle-rehearsal'}, {'method': 'POST', 'path': '/api/pre-market-readiness-pipeline', 'body': {'include_live_scan_plan': False}}, {'method': 'GET', 'path': '/api/deployment-checklist'}], 'success_criteria': ['paper preflight returns ok', 'synthetic rehearsal would_attempt_trade is true', 'pre-market readiness pipeline is not FAIL'], 'stop_conditions': ['paper preflight fails', 'synthetic rehearsal fails', 'emergency stop active or operator pause active']},
        {'name': 'market_open_no_order_validation', 'purpose': 'Validate market-open conditions and scan plan only; still no order execution.', 'commands': [{'method': 'POST', 'path': '/api/auto-cycle-plan'}, {'method': 'POST', 'path': '/api/market-open-rehearsal'}, {'method': 'POST', 'path': '/api/pre-market-readiness-pipeline', 'body': {'include_live_scan_plan': True}}, {'method': 'GET', 'path': '/api/deployment-checklist'}], 'success_criteria': ['auto-cycle plan returns executable candidates or clear blockers', 'market-open rehearsal status is PASS or timing-only block', 'deployment checklist action is ready_for_market_open or wait_for_market_open'], 'stop_conditions': ['market-open rehearsal fails', 'pipeline reports FAIL', 'new emergency stop or operator pause appears']},
        {'name': 'enable_or_confirm_scheduler', 'purpose': 'Confirm scheduler is running; if not, fix deployment/process setup (not an order action).', 'commands': [{'method': 'GET', 'path': '/api/bot-status'}], 'success_criteria': ['scheduler_running is true', 'auto_scan_job_registered is true'], 'stop_conditions': ['scheduler_running is false', 'auto_scan_job_registered is false']},
        {'name': 'first_trade_watch', 'purpose': 'Watch first attempted paper trade and verify governor/risk fields remain within limits.', 'commands': [{'method': 'GET', 'path': '/api/bot-status'}], 'success_criteria': ['last_auto_trade_attempts present when first trade is attempted', 'first_trade_final_qty <= FIRST_TRADE_MAX_QTY', 'probe_trade or first_trade_governor fields are consistent', 'no emergency stop and no unprotected position'], 'stop_conditions': ['first_trade_final_qty exceeds configured max', 'emergency stop active', 'unprotected position detected']},
        {'name': 'emergency_only', 'purpose': 'Use safety controls only when a real risk issue exists and after manual review.', 'commands': [{'method': 'POST', 'path': '/api/control/emergency-stop'}, {'method': 'POST', 'path': '/api/control/clear-emergency-stop'}], 'success_criteria': ['emergency stop can be activated when needed', 'clear action used only after manual review'], 'stop_conditions': ['clear-emergency-stop attempted before review', 'unsafe condition remains unresolved']},
    ]


def _operator_runbook_next_best_command(state: dict) -> dict:
    preflight = state.get('last_paper_readiness_preflight') or {}
    synthetic = state.get('last_synthetic_rehearsal') or {}
    pipeline = state.get('last_pre_market_readiness_pipeline') or {}
    if not state.get('last_paper_readiness_preflight_at'):
        return {'method': 'POST', 'path': '/api/paper-readiness-preflight', 'reason': 'No paper readiness preflight has been recorded yet.'}
    if preflight and not bool(preflight.get('ok')):
        return {'method': 'GET', 'path': '/api/paper-readiness-preflight', 'reason': 'Paper readiness preflight failed; inspect that result before continuing.'}
    if not state.get('last_synthetic_rehearsal_at'):
        return {'method': 'POST', 'path': '/api/synthetic-auto-cycle-rehearsal', 'reason': 'Paper preflight passed; synthetic rehearsal is the next safe no-order step.'}
    if synthetic and not bool(synthetic.get('would_attempt_trade')):
        return {'method': 'GET', 'path': '/api/synthetic-auto-cycle-rehearsal', 'reason': 'Synthetic rehearsal indicates blockers; inspect before continuing.'}
    if not state.get('last_pre_market_readiness_pipeline_at'):
        return {'method': 'POST', 'path': '/api/pre-market-readiness-pipeline', 'body': {'include_live_scan_plan': False}, 'reason': 'Run pre-market readiness pipeline without live scan plan first.'}
    if pipeline and str(pipeline.get('overall_status', '')).upper() == 'FAIL':
        return {'method': 'POST', 'path': '/api/pre-market-readiness-pipeline', 'body': {'include_live_scan_plan': False}, 'reason': 'Pre-market readiness pipeline failed; inspect and re-run only after fixes.'}
    market_open, _market_reason = market_open_for_auto_cycle()
    if bool(market_open) or bool(state.get('operator_market_open_validation_desired')):
        return {'method': 'POST', 'path': '/api/auto-cycle-plan', 'reason': 'Readiness chain passed and market-open validation is appropriate.'}
    return {'method': 'GET', 'path': '/api/deployment-checklist', 'reason': 'Readiness baseline is complete; confirm deployment checklist status.'}


@app.route('/api/operator-runbook', methods=['GET'])
def api_operator_runbook():
    state = get_runtime_state()
    snapshot = {
        'status': 'ok',
        'timestamps': {
            'last_paper_readiness_preflight_at': state.get('last_paper_readiness_preflight_at'),
            'last_pre_market_readiness_pipeline_at': state.get('last_pre_market_readiness_pipeline_at'),
            'last_auto_cycle_plan_at': state.get('last_auto_cycle_plan_at'),
            'last_market_open_rehearsal_at': state.get('last_market_open_rehearsal_at'),
            'last_synthetic_rehearsal_at': state.get('last_synthetic_rehearsal_at'),
        },
        'scheduler_running': bool(state.get('scheduler_running')),
        'auto_scan_job_registered': bool(state.get('auto_scan_job_registered')),
        'emergency_stop_active': bool(state.get('emergency_stop_active')),
        'operator_auto_trade_paused': bool(state.get('operator_auto_trade_paused')),
        'last_auto_trade_attempts': state.get('last_auto_trade_attempts', []),
        'last_auto_trade_error': state.get('last_auto_trade_error'),
        'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons', []),
        'would_attempt_trade': bool((state.get('last_synthetic_rehearsal') or {}).get('would_attempt_trade')),
        'executable_count': int((state.get('last_auto_cycle_plan') or {}).get('executable_count') or 0),
        'attempted_count': len(state.get('last_auto_trade_attempts') or []),
        'blocking_reasons': (state.get('last_auto_trade_skip_reasons') or []),
        'warning_reasons': [],
        'next_action_hint': build_deployment_checklist(state).get('next_required_action'),
        'next_required_action': build_deployment_checklist(state).get('next_required_action'),
    }
    if snapshot['emergency_stop_active'] or snapshot['operator_auto_trade_paused']:
        snapshot['status'] = 'blocked'
    payload = {
        'generated_at': now_et().isoformat(),
        'environment_summary': _operator_runbook_environment_summary(),
        'current_readiness_snapshot': snapshot,
        'phases': _operator_runbook_phases(),
        'next_best_command': _operator_runbook_next_best_command(state),
        'warnings': [
            'Do not enable live trading.',
            'Do not manually bypass first-trade governor.',
            'Do not run /api/auto-cycle until readiness is clean and market conditions are valid.',
            'Do not ignore emergency stop/operator pause.',
            'Do not assume synthetic rehearsal equals broker/account readiness.',
            'Do not assume paper readiness equals profitable strategy.',
        ],
        'forbidden_actions': [
            'set LIVE_TRADING_OVERRIDE=1',
            'use market buy entries',
            'increase FIRST_TRADE_MAX_QTY before first successful paper trade review',
            'remove stop protection',
            'disable emergency stop checks',
            'trade with paper_base_url pointing to live endpoint',
        ],
    }
    return ok(payload)


@app.route('/api/pre-market-readiness-pipeline', methods=['POST'])
def api_pre_market_readiness_pipeline():
    data = request.get_json(silent=True) or {}
    symbol = (data.get('symbol') or '').strip() or None
    include_live_scan_plan = bool(data.get('include_live_scan_plan', False))
    try:
        return ok(run_pre_market_readiness_pipeline(symbol=symbol, include_live_scan_plan=include_live_scan_plan))
    except Exception as exc:
        RUNTIME_STATE['last_pre_market_readiness_pipeline_error'] = str(exc)
        RUNTIME_STATE['last_pre_market_readiness_pipeline_at'] = now_et().isoformat()
        return fail('pre_market_readiness_pipeline_failed', 500)


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


def _is_active_sell_order(order: dict) -> bool:
    side = (order.get('side') or '').lower()
    status = (order.get('status') or '').lower()
    inactive = {'canceled', 'cancelled', 'rejected', 'filled', 'expired', 'done_for_day'}
    return side == 'sell' and status not in inactive


def build_position_protection_audit() -> dict:
    positions = get_open_positions() or []
    orders = get_open_orders() or []
    generated_at = now_et().isoformat()
    if not positions:
        return {
            'ok': True,
            'generated_at': generated_at,
            'status': 'PASS',
            'next_action_hint': 'no_positions',
            'summary_reason': 'no_positions',
            'open_positions_count': 0,
            'open_orders_count': len(orders),
            'unprotected_position_detected': False,
            'positions': [],
        }
    active_sell_orders = [o for o in orders if _is_active_sell_order(o)]
    per_position = []
    has_unprotected = False
    for pos in positions:
        symbol = (pos.get('symbol') or '').upper()
        symbol_orders = [o for o in active_sell_orders if (o.get('symbol') or '').upper() == symbol]
        has_stop = any((o.get('type') or '').lower() in {'stop', 'stop_limit'} for o in symbol_orders)
        has_trailing = any((o.get('type') or '').lower() == 'trailing_stop' for o in symbol_orders)
        has_target = any((o.get('type') or '').lower() == 'limit' for o in symbol_orders)
        protection_count = int(has_stop) + int(has_trailing) + int(has_target)
        if protection_count >= 2 and (has_stop or has_trailing):
            pstatus = 'PROTECTED'
        elif protection_count >= 1:
            pstatus = 'PARTIAL'
        else:
            pstatus = 'UNPROTECTED'
            has_unprotected = True
        missing = []
        if not (has_stop or has_trailing):
            missing.append('stop_or_trailing')
        if not has_target:
            missing.append('target')
        per_position.append({
            'symbol': symbol,
            'qty': pos.get('qty'),
            'side': pos.get('side'),
            'has_stop_order': has_stop,
            'has_target_order': has_target,
            'has_trailing_or_runner_order': has_trailing,
            'protection_status': pstatus,
            'missing_protection': missing,
        })
    return {
        'ok': True,
        'generated_at': generated_at,
        'status': 'FAIL' if has_unprotected else 'PASS',
        'next_action_hint': 'unprotected_position_detected' if has_unprotected else 'protected_positions_present',
        'summary_reason': 'unprotected_position_detected' if has_unprotected else 'positions_protected_or_partial',
        'open_positions_count': len(positions),
        'open_orders_count': len(orders),
        'unprotected_position_detected': has_unprotected,
        'positions': per_position,
    }


def build_first_trade_observer_snapshot() -> dict:
    state = get_runtime_state()
    recent_trades = get_recent_trades() or []
    recent_scans = get_recent_scans() or []
    open_positions = get_open_positions() or []
    open_orders = get_open_orders() or []
    protection = build_position_protection_audit()
    pipeline = state.get('last_pre_market_readiness_pipeline') or {}
    last_plan = state.get('last_auto_cycle_plan') or {}
    last_attempt = (state.get('last_auto_trade_attempts') or [])
    last_attempt_item = last_attempt[-1] if last_attempt else None
    has_auto_attempt_today = bool(last_attempt)
    next_action_hint = 'wait_for_auto_attempt'
    if not pipeline:
        next_action_hint = 'run_pre_market_readiness_pipeline'
    elif protection.get('status') == 'FAIL':
        next_action_hint = 'review_unprotected_position'
    elif state.get('last_auto_trade_error'):
        if state.get('last_auto_trade_error') == 'no_executable_candidate':
            next_action_hint = 'review_scan_diagnostics'
        else:
            next_action_hint = 'review_execution_error'
    elif int(last_plan.get('candidate_count') or 0) > 0 and int(last_plan.get('executable_count') or 0) == 0:
        next_action_hint = 'review_scan_diagnostics'
    elif open_positions:
        next_action_hint = 'monitor_open_trade' if protection.get('status') == 'PASS' else 'review_unprotected_position'
    elif has_auto_attempt_today:
        next_action_hint = 'ready_for_next_auto_cycle'
    return {
        'ok': True,
        'generated_at': now_et().isoformat(),
        'session_status': 'active' if bool(state.get('scheduler_running')) else 'idle',
        'has_auto_attempt_today': has_auto_attempt_today,
        'last_auto_trade_attempt': last_attempt_item,
        'last_auto_trade_error': state.get('last_auto_trade_error'),
        'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons', []),
        'attempted_candidate_count': len(last_attempt),
        'last_auto_trade_verdict': state.get('last_auto_trade_verdict'),
        'latest_plan_summary': {'candidate_count': int(last_plan.get('candidate_count') or 0), 'executable_count': int(last_plan.get('executable_count') or 0), 'generated_at': last_plan.get('generated_at')},
        'latest_rehearsal_summary': state.get('last_synthetic_rehearsal'),
        'latest_pipeline_summary': pipeline,
        'recent_auto_trades': [t for t in recent_trades if (t.get('source') or '').lower() == 'auto'][:5],
        'open_positions_count': len(open_positions),
        'open_orders_count': len(open_orders),
        'protection_summary': {'status': protection.get('status'), 'unprotected_position_detected': protection.get('unprotected_position_detected'), 'next_action_hint': protection.get('next_action_hint')},
        'next_action_hint': next_action_hint,
        'recent_scan_count': len(recent_scans),
    }


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
    market_open, market_reason = market_open_for_auto_cycle()
    market_status = {'market_open_for_auto_cycle': market_open, 'market_reason': market_reason, 'within_morning_scan_window': within_morning_scan_window(), 'within_auto_scan_window': within_auto_scan_window()}
    if not market_open:
        auto_cycle_blockers.append('outside_auto_scan_window' if 'outside' in market_reason else 'market_closed')
    if not bool(state.get('scheduler_running')):
        auto_cycle_blockers.append('scheduler_not_running')
    if count_trades_today(source='auto') >= config.MAX_AUTO_TRADES_PER_DAY:
        auto_cycle_blockers.append('max_auto_trades_reached')
    if estimated_daily_loss_risk_used_today() >= (config.CURRENT_BANKROLL * config.MAX_DAILY_REALIZED_LOSS_PCT):
        auto_cycle_blockers.append('daily_loss_limit_reached')
    if state.get('last_scan_error'):
        auto_cycle_blockers.append('last_scan_failed')
    if state.get('last_auto_trade_error') and state.get('last_auto_trade_error') != 'no_executable_candidate':
        auto_cycle_blockers.append('last_execution_failed')
    last_plan = state.get('last_auto_cycle_plan') or {}
    if not last_plan:
        auto_cycle_blockers.append('no_candidate_plan_available')
    if (last_plan.get('candidate_count') == 0) or ('no_candidates' in (state.get('last_auto_trade_skip_reasons') or [])):
        auto_cycle_blockers.append('scan_has_no_candidates')
    if (last_plan.get('candidate_count', 0) > 0 and last_plan.get('executable_count', 0) == 0) or (state.get('last_auto_trade_error') == 'no_executable_candidate'):
        auto_cycle_blockers.append('scan_has_no_executable_candidates')
    if not state.get('auto_scan_job_registered'):
        auto_cycle_blockers.append('auto_scan_job_not_registered')
    auto_cycle_blockers = sorted(set(auto_cycle_blockers))
    auto_cycle_ready = len(auto_cycle_blockers) == 0
    hint_priority = [
        ('emergency_stop_active', 'clear_emergency_stop'),
        ('operator_auto_trade_paused', 'resume_auto_trading'),
        ('auto_cycle_blocked_not_paper', 'fix_paper_credentials'),
        ('scheduler_not_running', 'start_scheduler'),
        ('auto_scan_job_not_registered', 'review_scheduler_jobs'),
        ('outside_auto_scan_window', 'wait_for_market_open'),
        ('market_closed', 'wait_for_market_open'),
        ('daily_loss_limit_reached', 'daily_loss_limit_reached'),
        ('max_auto_trades_reached', 'max_auto_trades_reached'),
        ('scan_has_no_candidates', 'review_scan_diagnostics'),
        ('scan_has_no_executable_candidates', 'review_scan_diagnostics'),
        ('no_candidate_plan_available', 'run_auto_cycle_plan'),
    ]
    next_action_hint = 'ready_for_auto_cycle'
    for blocker, hint in hint_priority:
        if blocker in auto_cycle_blockers:
            next_action_hint = hint
            break
    auto_cycle_readiness = {
        'ready': auto_cycle_ready,
        'blockers': auto_cycle_blockers,
        'market_open': market_open,
        'market_reason': market_reason,
        'paper_or_sim_ok': bool(config.PAPER_TRADING_DETECTED) or bool(config.SIMULATION_MODE),
    }

    return ok({
        **state,
        'last_auto_trade_attempts': state.get('last_auto_trade_attempts', []),
        'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons', []),
        'last_auto_trade_verdict': state.get('last_auto_trade_verdict'),
        'control_state': control_state,
        'market_status': market_status,
        'auto_cycle_ready': auto_cycle_ready,
        'auto_cycle_readiness': auto_cycle_readiness,
        'auto_cycle_blockers': auto_cycle_blockers,
        'why_no_motion': [] if auto_cycle_ready else auto_cycle_blockers,
        'next_action': next_action_hint,
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
        'latest_scan_diagnostics': ((LATEST_SCAN or {}).get('scan_diagnostics') or {}),
        'attempt_debug': {
            'last_auto_trade_attempts': state.get('last_auto_trade_attempts', []),
            'last_auto_trade_error': state.get('last_auto_trade_error'),
            'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons', []),
            'last_auto_trade_verdict': state.get('last_auto_trade_verdict'),
            'last_scan_skipped_reason': state.get('last_scan_skipped_reason'),
            'attempted_candidate_count': len(state.get('last_auto_trade_attempts') or []),
            'blocker_counts': state.get('last_auto_trade_blocker_counts') or {},
            'scheduler_running': state.get('scheduler_running'),
            'scheduled_jobs': state.get('scheduled_jobs'),
            'last_market_open_rehearsal': state.get('last_market_open_rehearsal'),
            'last_market_open_rehearsal_at': state.get('last_market_open_rehearsal_at'),
            'last_market_open_rehearsal_error': state.get('last_market_open_rehearsal_error'),
            'last_paper_readiness_preflight': state.get('last_paper_readiness_preflight'),
            'last_paper_readiness_preflight_at': state.get('last_paper_readiness_preflight_at'),
            'last_paper_readiness_preflight_error': state.get('last_paper_readiness_preflight_error'),
            'last_synthetic_rehearsal': state.get('last_synthetic_rehearsal'),
            'last_synthetic_rehearsal_at': state.get('last_synthetic_rehearsal_at'),
            'last_synthetic_rehearsal_error': state.get('last_synthetic_rehearsal_error'),
            'last_pre_market_readiness_pipeline': state.get('last_pre_market_readiness_pipeline'),
            'last_pre_market_readiness_pipeline_at': state.get('last_pre_market_readiness_pipeline_at'),
            'last_pre_market_readiness_pipeline_error': state.get('last_pre_market_readiness_pipeline_error'),
            'last_first_trade_observer': state.get('last_first_trade_observer'),
            'last_first_trade_observer_at': state.get('last_first_trade_observer_at'),
            'last_position_protection_audit': state.get('last_position_protection_audit'),
            'last_position_protection_audit_at': state.get('last_position_protection_audit_at'),
        },
        'readiness_debug': {
            'last_paper_readiness_preflight': state.get('last_paper_readiness_preflight'),
            'last_paper_readiness_preflight_at': state.get('last_paper_readiness_preflight_at'),
            'last_paper_readiness_preflight_error': state.get('last_paper_readiness_preflight_error'),
            'last_synthetic_rehearsal': state.get('last_synthetic_rehearsal'),
            'last_synthetic_rehearsal_at': state.get('last_synthetic_rehearsal_at'),
            'last_synthetic_rehearsal_error': state.get('last_synthetic_rehearsal_error'),
            'last_pre_market_readiness_pipeline': state.get('last_pre_market_readiness_pipeline'),
            'last_pre_market_readiness_pipeline_at': state.get('last_pre_market_readiness_pipeline_at'),
            'last_pre_market_readiness_pipeline_error': state.get('last_pre_market_readiness_pipeline_error'),
            'last_first_trade_observer': state.get('last_first_trade_observer'),
            'last_first_trade_observer_at': state.get('last_first_trade_observer_at'),
            'last_position_protection_audit': state.get('last_position_protection_audit'),
            'last_position_protection_audit_at': state.get('last_position_protection_audit_at'),
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


@app.route('/api/position-protection-audit', methods=['GET'])
def api_position_protection_audit():
    try:
        audit = build_position_protection_audit()
        RUNTIME_STATE['last_position_protection_audit'] = audit
        RUNTIME_STATE['last_position_protection_audit_at'] = now_et().isoformat()
        RUNTIME_STATE['last_position_protection_audit_error'] = None
        return ok(audit)
    except Exception as exc:
        RUNTIME_STATE['last_position_protection_audit_error'] = str(exc)
        RUNTIME_STATE['last_position_protection_audit_at'] = now_et().isoformat()
        return fail('position_protection_audit_failed', 500)


@app.route('/api/first-trade-observer', methods=['GET'])
def api_first_trade_observer():
    try:
        snapshot = build_first_trade_observer_snapshot()
        protection_audit = build_position_protection_audit()
        payload = {**snapshot, 'protection_audit': protection_audit, 'safe_next_action': snapshot.get('next_action_hint')}
        RUNTIME_STATE['last_first_trade_observer'] = {k: payload.get(k) for k in ['generated_at', 'session_status', 'has_auto_attempt_today', 'attempted_candidate_count', 'last_auto_trade_error', 'last_auto_trade_verdict', 'open_positions_count', 'open_orders_count', 'protection_summary', 'next_action_hint', 'safe_next_action']}
        RUNTIME_STATE['last_first_trade_observer_at'] = now_et().isoformat()
        RUNTIME_STATE['last_first_trade_observer_error'] = None
        return ok(payload)
    except Exception as exc:
        RUNTIME_STATE['last_first_trade_observer_error'] = str(exc)
        RUNTIME_STATE['last_first_trade_observer_at'] = now_et().isoformat()
        return fail('first_trade_observer_failed', 500)


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





@app.route('/api/paper-readiness-preflight', methods=['GET', 'POST'])
def api_paper_readiness_preflight():
    symbol = (request.args.get('symbol') or '').strip()
    body = (request.get_json(silent=True) or {}) if request.method == 'POST' else {}
    symbol = (body.get('symbol') or symbol or None)
    try:
        result = run_paper_trade_readiness_preflight(symbol)
        RUNTIME_STATE['last_paper_readiness_preflight'] = {
            'ok': result.get('ok'),
            'overall_status': result.get('overall_status'),
            'blocking_reasons': result.get('blocking_reasons', [])[:10],
            'warning_reasons': result.get('warning_reasons', [])[:10],
            'next_action_hint': result.get('next_action_hint'),
            'symbol': result.get('symbol'),
        }
        RUNTIME_STATE['last_paper_readiness_preflight_at'] = now_et().isoformat()
        RUNTIME_STATE['last_paper_readiness_preflight_error'] = None
    except Exception as exc:
        RUNTIME_STATE['last_paper_readiness_preflight_error'] = str(exc)
        RUNTIME_STATE['last_paper_readiness_preflight_at'] = now_et().isoformat()
        result = {'ok': False, 'overall_status': 'FAIL', 'checks': [], 'blocking_reasons': ['preflight_exception'], 'warning_reasons': [], 'next_action_hint': 'review_preflight_error', 'symbol': (symbol or config.PREFLIGHT_SYMBOL)}
    return ok(result)

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


if config.AUTO_START_EXECUTION_ENGINE and os.getenv("DISABLE_AUTO_START_FOR_TESTS") != "1":
    RUNTIME_STATE['engine_start_attempted'] = True
    RUNTIME_STATE['engine_start_at'] = now_et().isoformat()
    try:
        start_execution_engine(auto_scan_callback=run_scan_and_maybe_auto_trade)
        RUNTIME_STATE['engine_start_error'] = None
    except Exception as exc:
        RUNTIME_STATE['engine_start_error'] = str(exc)
        logger.exception('Execution engine auto-start failed')


if __name__ == '__main__':
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)
