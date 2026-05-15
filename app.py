import json
import logging
import os
import re
import sqlite3
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from flask_sock import Sock

import config
from broker_facade import BrokerError, get_order, maybe_activate_runner_trailing, get_open_orders, get_open_positions, get_account, get_clock
import db
from db import count_trades_today, estimated_daily_loss_risk_used_today, get_failed_trades_today, get_recent_auto_cycle_attempts, get_recent_operator_actions, get_recent_scans, get_recent_trades, get_trade_by_order_id, init_db, insert_auto_cycle_attempt, insert_operator_action, insert_scan, update_trade_status
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
import execution_service
from execution_service import validate_trade_candidate, execute_trade_candidate
from preflight import run_paper_trade_readiness_preflight, run_preflight

app = Flask(__name__)
app.config['SECRET_KEY'] = config.SECRET_KEY
sock = Sock(app)
logger = logging.getLogger(__name__)

PUBLIC_PATH_ALLOWLIST = {'/favicon.ico'}


def _operator_auth_status() -> dict:
    return {
        'operator_auth_enabled': bool(config.OPERATOR_AUTH_ENABLED),
        'operator_auth_header': config.OPERATOR_AUTH_HEADER,
        'operator_auth_allow_localhost': bool(config.OPERATOR_AUTH_ALLOW_LOCALHOST),
        'operator_auth_configured': bool(config.OPERATOR_AUTH_TOKEN),
    }


def _is_local_request() -> bool:
    remote_addr = (getattr(request, 'remote_addr', '') or '').strip().lower()
    return remote_addr in {'127.0.0.1', '::1', 'localhost'}


def _operator_auth_passes() -> bool:
    if not config.OPERATOR_AUTH_ENABLED:
        return True
    if config.OPERATOR_AUTH_ALLOW_LOCALHOST and _is_local_request():
        return True
    token = config.OPERATOR_AUTH_TOKEN
    if not token:
        return False
    header_value = request.headers.get(config.OPERATOR_AUTH_HEADER, '').strip()
    if header_value and header_value == token:
        return True
    auth_header = request.headers.get('Authorization', '').strip()
    if auth_header.startswith('Bearer ') and auth_header[len('Bearer '):].strip() == token:
        return True
    return False


@app.before_request
def operator_auth_guard():
    path = request.path or '/'
    if path in PUBLIC_PATH_ALLOWLIST or path.startswith('/static/'):
        return None
    protected = path == '/' or path == '/operator' or path.startswith('/api/')
    if not protected:
        return None
    if _operator_auth_passes():
        return None
    if path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'operator_auth_required'}), 401
    return ('operator_auth_required', 401)


def compact_error_message(error: str) -> str:
    text = str(error or '').strip()
    if not text:
        return text
    lower = text.lower()
    if '401 authorization required' in lower or 'market_clock_unavailable' in lower:
        return 'Alpaca auth failed. Check ALPACA_API_KEY, ALPACA_API_SECRET, and ALPACA_PAPER_BASE.'
    text = re.sub(r'<[^>]*>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > 200:
        text = f"{text[:197]}..."
    return text


OPERATOR_SAFE_ENDPOINTS = [
    {'label': 'market_open_command_center', 'method': 'GET', 'path': '/api/market-open-command-center', 'requires_market_open': False, 'notes': 'Primary readiness dashboard summary.'},
    {'label': 'paper_market_launch_gate', 'method': 'GET', 'path': '/api/paper-market-launch-gate', 'requires_market_open': False, 'notes': 'Final no-order paper-market validation launch gate.'},
    {'label': 'paper_readiness_preflight', 'method': 'POST', 'path': '/api/paper-readiness-preflight', 'requires_market_open': False, 'notes': 'Paper-only readiness checks.'},
    {'label': 'synthetic_auto_cycle_rehearsal', 'method': 'POST', 'path': '/api/synthetic-auto-cycle-rehearsal', 'requires_market_open': False, 'notes': 'No-order synthetic rehearsal.'},
    {'label': 'pre_market_readiness_pipeline', 'method': 'POST', 'path': '/api/pre-market-readiness-pipeline', 'requires_market_open': False, 'notes': 'Aggregated pre-market readiness pipeline.'},
    {'label': 'market_open_rehearsal', 'method': 'POST', 'path': '/api/market-open-rehearsal', 'requires_market_open': True, 'notes': 'Market-open validation rehearsal without order execution.'},
    {'label': 'auto_cycle_plan', 'method': 'POST', 'path': '/api/auto-cycle-plan', 'requires_market_open': True, 'notes': 'Plan-only candidate evaluation.'},
    {'label': 'first_trade_observer', 'method': 'GET', 'path': '/api/first-trade-observer', 'requires_market_open': False, 'notes': 'First-trade safety observer state.'},
    {'label': 'position_protection_audit', 'method': 'GET', 'path': '/api/position-protection-audit', 'requires_market_open': False, 'notes': 'Position protection audit status.'},
    {'label': 'paper_position_reconciliation', 'method': 'GET', 'path': '/api/paper-position-reconciliation', 'requires_market_open': False, 'notes': 'Paper broker/DB position reconciliation status.'},
    {'label': 'stale_db_trade_cleanup_plan', 'method': 'GET', 'path': '/api/stale-db-trade-cleanup-plan', 'requires_market_open': False, 'notes': 'Read-only stale DB trade cleanup recommendation plan.'},
    {'label': 'market_session_heartbeat', 'method': 'GET', 'path': '/api/market-session-heartbeat', 'requires_market_open': False, 'notes': 'Session heartbeat and next action hint.'},
    {'label': 'paper_validation_session_report', 'method': 'GET', 'path': '/api/paper-validation-session-report', 'requires_market_open': False, 'notes': 'Post-session paper validation acceptance report.'},
    {'label': 'auto_cycle_attempts', 'method': 'GET', 'path': '/api/auto-cycle-attempts?limit=10', 'requires_market_open': False, 'notes': 'Recent attempt ledger snapshot.'},
    {'label': 'deployment_checklist', 'method': 'GET', 'path': '/api/deployment-checklist', 'requires_market_open': False, 'notes': 'Deployment checklist state.'},
    {'label': 'operator_runbook', 'method': 'GET', 'path': '/api/operator-runbook', 'requires_market_open': False, 'notes': 'Operator runbook and next best safe command.'},
]
OPERATOR_SAFE_BACKEND_ONLY_ENDPOINTS = {'/api/deployment-checklist', '/api/operator-runbook', '/api/stale-db-trade-cleanup-plan'}
OPERATOR_FORBIDDEN_ENDPOINTS = [
    {'method': 'POST', 'path': '/api/auto-cycle', 'reason': 'Executes full auto-cycle and may place orders.'},
    {'method': 'POST', 'path': '/api/run-auto-cycle', 'reason': 'Alias for auto-cycle execution endpoint.'},
    {'method': 'POST', 'path': '/api/control/pause-auto-trading', 'reason': 'Operator page remains read-only diagnostics only.'},
    {'method': 'POST', 'path': '/api/control/resume-auto-trading', 'reason': 'Operator page remains read-only diagnostics only.'},
    {'method': 'POST', 'path': '/api/control/emergency-stop', 'reason': 'Emergency stop controls are out-of-scope for this page.'},
    {'method': 'POST', 'path': '/api/control/clear-emergency-stop', 'reason': 'Emergency stop controls are out-of-scope for this page.'},
    {'method': 'POST', 'path': '/api/order', 'reason': 'Order mutation endpoints are forbidden.'},
    {'method': 'POST', 'path': '/api/orders', 'reason': 'Order mutation endpoints are forbidden.'},
    {'method': 'DELETE', 'path': '/api/order', 'reason': 'Order cancellation endpoints are forbidden.'},
    {'method': 'POST', 'path': '/api/position/close', 'reason': 'Position mutation endpoints are forbidden.'},
    {'method': 'POST', 'path': '/api/positions/close', 'reason': 'Position mutation endpoints are forbidden.'},
]


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
            'last_scan_error': compact_error_message(state.get('last_scan_error')),
            'last_auto_trade_at': state.get('last_auto_trade_at'),
            'last_auto_trade_error': compact_error_message(state.get('last_auto_trade_error')),
            'last_auto_trade_skip_reasons': state.get('last_auto_trade_skip_reasons') or [],
            'last_scan_skipped_reason': state.get('last_scan_skipped_reason'),
            'attempted_candidate_count': len(state.get('last_auto_trade_attempts') or []),
            'blocker_counts': state.get('last_auto_trade_blocker_counts') or {},
        },
        'latest_scan': scan_preview,
        'last_auto_trade_attempts': attempts,
        'last_auto_trade_error': compact_error_message(state.get('last_auto_trade_error')),
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


def new_cycle_id(prefix: str = "cycle") -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def record_auto_cycle_attempt(payload: dict) -> None:
    try:
        insert_auto_cycle_attempt(payload)
    except Exception as exc:
        logger.warning('auto_cycle_attempt_ledger_write_failed: %s', exc)

def run_scan_and_maybe_auto_trade():
    global LATEST_SCAN
    cycle_id = new_cycle_id("cycle")
    market_open, market_reason = market_open_for_auto_cycle()
    if not market_open:
        RUNTIME_STATE['last_scan_skipped_reason'] = market_reason
        RUNTIME_STATE['last_auto_trade_error'] = market_reason
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = [market_reason]
        RUNTIME_STATE['last_auto_trade_attempts'] = []
        RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': [market_reason]}
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'scheduled_auto_cycle', 'status': 'skipped', 'market_reason': market_reason, 'skip_reasons': [market_reason]})
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
            record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'scheduled_auto_cycle', 'status': 'blocked', 'market_reason': market_reason, 'candidate_count': int(plan.get('candidate_count') or 0), 'executable_count': int(plan.get('executable_count') or 0), 'skip_reasons': ['no_candidates'], 'top_blockers': plan.get('top_blockers') or {}})
            return
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'scheduled_auto_cycle', 'status': 'planned' if int(plan.get('executable_count') or 0) > 0 else 'blocked', 'market_reason': market_reason, 'candidate_count': int(plan.get('candidate_count') or 0), 'executable_count': int(plan.get('executable_count') or 0), 'top_blockers': plan.get('top_blockers') or {}})
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
                'unprotected_symbols': verdict.get('unprotected_symbols', candidate.get('unprotected_symbols', [])),
                'unsafe_protection_symbols': verdict.get('unsafe_protection_symbols', candidate.get('unsafe_protection_symbols', candidate.get('unprotected_symbols', []))),
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
                    record_auto_cycle_attempt({
                        'cycle_id': cycle_id, 'source': 'scheduled_auto_cycle', 'status': 'executed', 'market_reason': market_reason,
                        'candidate_count': int(plan.get('candidate_count') or 0), 'executable_count': int(plan.get('executable_count') or 0),
                        'attempted_symbol': candidate.get('symbol'), 'attempted_qty': attempts[-1].get('qty'),
                        'probe_trade': bool(verdict.get('probe_trade')), 'first_trade_governor_applied': bool(verdict.get('first_trade_governor_applied')),
                        'first_trade_final_qty': verdict.get('first_trade_final_qty'), 'first_trade_risk_dollars': verdict.get('first_trade_risk_dollars'),
                        'skip_reasons': verdict.get('skip_reasons') or [], 'top_blockers': plan.get('top_blockers') or {},
                        'compact_json': {'order_id': trade_order.get('id'), 'order_status': trade_order.get('status'), 'symbol': candidate.get('symbol'), 'qty': attempts[-1].get('qty')}
                    })
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
            unsafe_symbols = sorted(set(
                s for a in attempts for s in ((a.get('unsafe_protection_symbols') or []) + (a.get('unprotected_symbols') or [])) if s
            ))
            if 'unprotected_open_position' in combined_reasons:
                RUNTIME_STATE['last_auto_trade_error'] = 'unprotected_open_position'
            else:
                RUNTIME_STATE['last_auto_trade_error'] = ';'.join(execution_errors) if execution_errors else 'no_executable_candidate'
            RUNTIME_STATE['last_auto_trade_skip_reasons'] = combined_reasons
            RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': combined_reasons, 'execution_errors': execution_errors, 'unsafe_protection_symbols': unsafe_symbols, 'unprotected_symbols': unsafe_symbols}
            record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'scheduled_auto_cycle', 'status': 'failed' if execution_errors else 'blocked', 'market_reason': market_reason, 'candidate_count': int(plan.get('candidate_count') or 0), 'executable_count': int(plan.get('executable_count') or 0), 'skip_reasons': combined_reasons, 'top_blockers': plan.get('top_blockers') or {}, 'execution_error': ';'.join(execution_errors), 'compact_json': {'unsafe_protection_symbols': unsafe_symbols, 'unprotected_symbols': unsafe_symbols}})
    except Exception as exc:
        RUNTIME_STATE['last_scan_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_error'] = str(exc)
        RUNTIME_STATE['last_auto_trade_skip_reasons'] = ['auto_cycle_exception']
        RUNTIME_STATE['last_auto_trade_verdict'] = {'ok': False, 'skip_reasons': ['auto_cycle_exception']}
        RUNTIME_STATE['last_auto_cycle_plan_error'] = str(exc)
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'scheduled_auto_cycle', 'status': 'failed', 'market_reason': market_reason, 'execution_error': str(exc), 'skip_reasons': ['auto_cycle_exception']})


def build_auto_trade_candidate_plan(scan_result: dict, scan_id: int | None = None, external_exposure_checks: bool = True, ignore_time_window: bool = False) -> dict:
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
            if ignore_time_window:
                verdict = validate_trade_candidate(candidate, auto=True, external_exposure_checks=external_exposure_checks, ignore_time_window=True)
            else:
                verdict = validate_trade_candidate(candidate, auto=True, external_exposure_checks=external_exposure_checks)
        except TypeError:
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
    cycle_id = new_cycle_id("cycle")
    if not (bool(config.PAPER_TRADING_DETECTED) or bool(config.SIMULATION_MODE)):
        payload = {'scan_summary': {}, 'candidate_plan': {'blocked': True, 'blockers': ['auto_cycle_blocked_not_paper']}, 'status': 'failed', 'cycle_id': cycle_id}
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'auto_cycle_plan', 'status': 'failed', 'skip_reasons': ['auto_cycle_blocked_not_paper']})
        return payload
    if not include_live_scan:
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'auto_cycle_plan', 'status': 'skipped', 'skip_reasons': ['live_scan_disabled']})
        return {'scan_summary': {}, 'candidate_plan': {'blocked': True, 'blockers': ['live_scan_disabled']}, 'status': 'not_run', 'cycle_id': cycle_id}
    market_open, market_reason = market_open_for_auto_cycle()
    if not market_open:
        blocked_reason = 'outside_auto_scan_window' if 'outside' in market_reason else 'market_closed'
        plan = {'blocked': True, 'blockers': [blocked_reason], 'market_reason': market_reason}
        RUNTIME_STATE['last_auto_cycle_plan'] = plan
        RUNTIME_STATE['last_auto_cycle_plan_at'] = now_et().isoformat()
        RUNTIME_STATE['last_auto_cycle_plan_error'] = None
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'auto_cycle_plan', 'status': 'skipped', 'market_reason': market_reason, 'skip_reasons': [blocked_reason]})
        return {'scan_summary': {}, 'candidate_plan': plan, 'status': 'blocked_market_closed', 'cycle_id': cycle_id}
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
        status = 'PASS' if int(compact.get('executable_count') or 0) > 0 else 'WARN'
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'auto_cycle_plan', 'status': 'planned' if status == 'PASS' else 'blocked', 'market_reason': market_reason, 'candidate_count': compact.get('candidate_count'), 'executable_count': compact.get('executable_count'), 'top_blockers': compact.get('top_blockers') or {}})
        return {'scan_summary': {'scan_id': scan_id, 'best_pick': (result.get('best_pick') or {}).get('symbol')}, 'candidate_plan': compact, 'status': status, 'cycle_id': cycle_id}
    except Exception as exc:
        RUNTIME_STATE['last_auto_cycle_plan_error'] = str(exc)
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'auto_cycle_plan', 'status': 'failed', 'execution_error': str(exc), 'skip_reasons': ['auto_cycle_plan_failed']})
        return {'scan_summary': {}, 'candidate_plan': {'blocked': True, 'blockers': ['auto_cycle_plan_failed']}, 'status': 'failed', 'cycle_id': cycle_id}


@app.route('/api/market-open-rehearsal', methods=['POST'])
def api_market_open_rehearsal():
    return ok(run_market_open_rehearsal_plan())


def run_market_open_rehearsal_plan(symbol: str | None = None, allow_live_scan: bool = True) -> dict:
    cycle_id = new_cycle_id("cycle")
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
        payload = {'status': 'failed', 'blocking_reasons': ['market_open_rehearsal_failed'], 'would_attempt_trade': False, 'next_action_hint': 'review_market_open_rehearsal', 'cycle_id': cycle_id}
        RUNTIME_STATE['last_market_open_rehearsal'] = payload
        RUNTIME_STATE['last_market_open_rehearsal_at'] = now_et().isoformat()
        RUNTIME_STATE['last_market_open_rehearsal_error'] = str(exc)
        record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'market_open_rehearsal', 'status': 'failed', 'execution_error': str(exc), 'skip_reasons': payload.get('blocking_reasons') or []})
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
        'cycle_id': cycle_id,
    }
    record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'market_open_rehearsal', 'status': 'planned' if would_attempt_trade else ('skipped' if status in {'blocked_market_closed', 'not_run_live_scan_disabled'} else 'blocked'), 'market_reason': market_reason, 'candidate_count': (candidate_plan or {}).get('candidate_count') or 0, 'executable_count': (candidate_plan or {}).get('executable_count') or 0, 'skip_reasons': sorted(set(blocking_reasons))})
    RUNTIME_STATE['last_market_open_rehearsal'] = payload
    RUNTIME_STATE['last_market_open_rehearsal_at'] = now_et().isoformat()
    RUNTIME_STATE['last_market_open_rehearsal_error'] = None
    return payload


def build_synthetic_rehearsal_scan(symbol: str = "TEST") -> dict:
    score_total = max(int(config.PROBE_MIN_SCORE) + 5, int(config.MIN_MOMENTUM_SCORE_TO_AUTOTRADE))
    candidate = {
        'symbol': (symbol or 'TEST').upper(),
        'setup_grade': 'A',
        'decision': 'BUY NOW',
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
    cycle_id = new_cycle_id("cycle")
    result = build_synthetic_rehearsal_scan(symbol or 'TEST')
    plan = build_auto_trade_candidate_plan(result, external_exposure_checks=False, ignore_time_window=True)
    first = next((a for a in plan.get('attempt_plan', []) if a.get('ok')), None)
    first_candidate_symbol = (first or {}).get('symbol') or ((plan.get('attempt_plan') or [{}])[0].get('symbol'))
    top_blockers = set(list(plan.get('top_blockers', {}).keys()))
    timing_blockers = {'outside_auto_scan_window', 'market_closed'}
    structural_blockers = sorted(set(top_blockers - timing_blockers))
    blocking_reasons = []
    if not first:
        if not bool((plan.get('attempt_plan') or [{}])[0].get('first_trade_governor_applied')):
            blocking_reasons.append('first_trade_governor_not_applied')
        else:
            blocking_reasons.append('no_executable_candidate')
    blocking_reasons = sorted(set(blocking_reasons + structural_blockers))
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
        'next_action_hint': 'ready_for_market_open' if first else 'review_scan_diagnostics',
        'cycle_id': cycle_id,
    }
    record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'synthetic_rehearsal', 'status': 'planned' if first else 'blocked', 'candidate_count': plan.get('candidate_count', 0), 'executable_count': plan.get('executable_count', 0), 'skip_reasons': blocking_reasons, 'top_blockers': plan.get('top_blockers') or {}})
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
    cycle_id = new_cycle_id("cycle")
    symbol = (symbol or config.PREFLIGHT_SYMBOL or 'F').upper()
    steps = []
    paper = run_paper_trade_readiness_preflight(symbol)
    RUNTIME_STATE['last_paper_readiness_preflight'] = {'ok': bool(paper.get('ok')), 'overall_status': paper.get('overall_status'), 'next_action_hint': paper.get('next_action_hint'), 'blocking_reasons': paper.get('blocking_reasons', []), 'warning_reasons': paper.get('warning_reasons', []), 'symbol': paper.get('symbol') or symbol}
    RUNTIME_STATE['last_paper_readiness_preflight_at'] = now_et().isoformat()
    RUNTIME_STATE['last_paper_readiness_preflight_error'] = None
    paper_blocking = list(paper.get('blocking_reasons') or [])
    paper_ok = len(paper_blocking) == 0
    steps.append({'name': 'paper_readiness_preflight', 'ok': paper_ok, 'status': paper.get('overall_status', 'FAIL'), 'next_action_hint': paper.get('next_action_hint'), 'blocking_reasons': paper.get('blocking_reasons', []), 'warning_reasons': paper.get('warning_reasons', []), 'metrics': {'checks': len(paper.get('checks') or []), 'symbol': paper.get('symbol')}})

    synthetic = run_synthetic_auto_cycle_rehearsal(symbol)
    synthetic_blocking = set(synthetic.get('blocking_reasons') or [])
    synthetic_structural_blockers = synthetic_blocking - {'outside_auto_scan_window', 'market_closed'}
    synthetic_ok = bool(synthetic.get('would_attempt_trade')) and bool(synthetic.get('first_trade_governor_applied')) and not synthetic_structural_blockers
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
    safe_enable = all([paper_ok, synthetic_ok, first_trade_ok, checklist.get('emergency_stop_clear'), checklist.get('scheduler_running'), checklist.get('auto_scan_job_registered'), checklist.get('operator_pause_clear')])
    timing_only_block = market_step.get('status') in {'blocked_market_closed', 'outside_auto_scan_window'}
    safe_manual = safe_enable and bool(mr.get('would_attempt_trade'))
    next_action = checklist.get('next_required_action')
    if not paper_ok:
        next_action = 'review_paper_readiness_preflight'
    elif bool(synthetic.get('would_attempt_trade')) is False and synthetic_structural_blockers:
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
    go_no_go = 'WAIT_FOR_MARKET_OPEN' if (safe_enable and timing_only_block) else ('GO' if safe_enable else 'NO_GO')
    payload = {'ok': overall != 'FAIL', 'overall_status': overall, 'symbol': symbol, 'steps': steps, 'deployment_checklist': checklist, 'go_no_go': go_no_go, 'next_required_action': next_action, 'safe_to_enable_auto_cycle': bool(safe_enable), 'safe_to_run_manual_auto_cycle': bool(safe_manual), 'offline_only': not include_live_scan_plan, 'include_live_scan_plan': bool(include_live_scan_plan), 'market_open_rehearsal_status': market_step.get('status'), 'auto_cycle_plan_status': auto_cycle_plan_status, 'cycle_id': cycle_id}
    RUNTIME_STATE['last_pre_market_readiness_pipeline'] = {'overall_status': overall, 'go_no_go': payload['go_no_go'], 'next_required_action': next_action, 'safe_to_enable_auto_cycle': payload['safe_to_enable_auto_cycle'], 'safe_to_run_manual_auto_cycle': payload['safe_to_run_manual_auto_cycle'], 'offline_only': payload['offline_only'], 'symbol': symbol}
    RUNTIME_STATE['last_pre_market_readiness_pipeline_at'] = now_et().isoformat()
    RUNTIME_STATE['last_pre_market_readiness_pipeline_error'] = None
    record_auto_cycle_attempt({'cycle_id': cycle_id, 'source': 'pre_market_pipeline', 'status': 'planned' if payload['safe_to_enable_auto_cycle'] else ('blocked' if payload['overall_status'] == 'WARN' else 'failed'), 'skip_reasons': [payload.get('next_required_action')], 'compact_json': {'overall_status': payload.get('overall_status'), 'go_no_go': payload.get('go_no_go')}})
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
    partial_symbols, unprotected_symbols, protected_symbols, close_pending_symbols = [], [], [], []
    for pos in positions:
        symbol = (pos.get('symbol') or '').upper()
        symbol_orders = [o for o in active_sell_orders if (o.get('symbol') or '').upper() == symbol]
        qty = abs(float(pos.get('qty') or 0))
        has_stop = has_trailing = has_target = False
        has_close_pending = False
        protective_qty = target_qty = trailing_qty = 0.0
        close_pending_qty = 0.0
        close_order_statuses = []
        close_orders_summary = []
        order_summary = []
        for o in symbol_orders:
            t = (o.get('type') or '').lower()
            oq = abs(float(o.get('qty') or o.get('remaining_qty') or qty or 0))
            if t in {'stop', 'stop_limit'}:
                has_stop = True
                protective_qty += oq
            elif t == 'trailing_stop':
                has_trailing = True
                protective_qty += oq
                trailing_qty += oq
            elif t == 'limit':
                has_target = True
                target_qty += oq
            elif t == 'market':
                has_close_pending = True
                close_pending_qty += oq
                close_order_statuses.append((o.get('status') or '').lower())
                close_orders_summary.append({'id': o.get('id'), 'qty': oq, 'status': o.get('status')})
            order_summary.append({'id': o.get('id'), 'type': t, 'qty': oq, 'status': o.get('status')})
        total_sell_qty = sum(abs(float(o.get('qty') or o.get('remaining_qty') or qty or 0)) for o in symbol_orders)
        if qty <= 0:
            pstatus = 'UNKNOWN'
        elif protective_qty >= qty:
            pstatus = 'PROTECTED'
        elif has_close_pending and close_pending_qty >= qty:
            pstatus = 'CLOSE_PENDING_UNPROTECTED'
        elif protective_qty > 0 or target_qty > 0:
            pstatus = 'PARTIAL'
        else:
            pstatus = 'UNPROTECTED'
        if pstatus == 'UNPROTECTED':
            unprotected_symbols.append(symbol)
        elif pstatus == 'CLOSE_PENDING_UNPROTECTED':
            close_pending_symbols.append(symbol)
            unprotected_symbols.append(symbol)
        elif pstatus == 'PARTIAL':
            partial_symbols.append(symbol)
        elif pstatus == 'PROTECTED':
            protected_symbols.append(symbol)
        missing = []
        if not (has_stop or has_trailing) or protective_qty <= 0:
            missing.append('stop_or_trailing')
        if target_qty <= 0:
            missing.append('target')
        per_position.append({
            'symbol': symbol,
            'qty': pos.get('qty'),
            'side': pos.get('side'),
            'market_value': pos.get('market_value'),
            'avg_entry_price': pos.get('avg_entry_price'),
            'current_price': pos.get('current_price'),
            'has_stop_order': has_stop,
            'has_target_order': has_target,
            'has_trailing_or_runner_order': has_trailing,
            'active_protective_sell_qty': protective_qty,
            'active_target_sell_qty': target_qty,
            'active_trailing_sell_qty': trailing_qty,
            'has_close_order_pending': has_close_pending,
            'active_close_order_qty': close_pending_qty,
            'close_order_statuses': sorted(set([s for s in close_order_statuses if s])),
            'close_orders_summary': close_orders_summary,
            'total_downside_protection_qty': protective_qty,
            'total_active_sell_order_qty': total_sell_qty,
            'protection_status': pstatus,
            'missing_protection': missing,
            'active_orders_summary': order_summary,
        })
    unsafe_symbols = sorted(set(unprotected_symbols + partial_symbols + close_pending_symbols))
    has_unprotected = bool(unsafe_symbols)
    status = 'FAIL' if has_unprotected else 'PASS'
    return {
        'ok': True,
        'generated_at': generated_at,
        'status': status,
        'next_action_hint': 'wait_for_close_order_fill' if close_pending_symbols else ('unprotected_position_detected' if has_unprotected else 'protected_positions_present'),
        'summary_reason': 'unprotected_position_detected' if has_unprotected else 'positions_protected_or_partial',
        'open_positions_count': len(positions),
        'open_orders_count': len(orders),
        'unprotected_position_detected': has_unprotected,
        'unprotected_symbols': sorted(set(unprotected_symbols)),
        'partial_symbols': sorted(set(partial_symbols)),
        'unsafe_protection_symbols': unsafe_symbols,
        'close_pending_symbols': sorted(set(close_pending_symbols)),
        'protected_symbols': sorted(set(protected_symbols)),
        'positions': per_position,
    }


def has_unprotected_open_position() -> tuple[bool, list[str], dict]:
    if bool(config.SIMULATION_MODE) and not bool(config.ACTIVE_PAPER_TRADING_MODE):
        return False, [], {'status': 'PASS', 'next_action_hint': 'simulation_mode'}
    audit = build_position_protection_audit() or {}
    symbols = list(audit.get('unsafe_protection_symbols') or audit.get('unprotected_symbols') or [])
    compact = {
        'status': audit.get('status'),
        'unprotected_symbols': list(audit.get('unprotected_symbols') or []),
        'unsafe_protection_symbols': symbols,
        'next_action_hint': audit.get('next_action_hint'),
    }
    return bool(audit.get('unprotected_position_detected')), symbols, compact


def build_paper_position_reconciliation() -> dict:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, order_id, order_status, outcome, qty, entry_price, filled_avg_price, filled_qty, created_at, updated_at, notes FROM trades
            WHERE outcome IS NULL OR outcome IN ('open', 'working_or_filled', 'partial_win', 'breakeven_or_small_win')
            """
        ).fetchall()
    db_symbols = sorted({str(r['symbol']).upper() for r in rows if r['symbol']})
    broker_positions = get_open_positions() or []
    broker_orders = get_open_orders() or []
    broker_symbols = sorted({str(p.get('symbol') or '').upper() for p in broker_positions if p.get('symbol')})
    protection = build_position_protection_audit() or {}
    unmatched_db = sorted(set(db_symbols) - set(broker_symbols))
    unmatched_broker = sorted(set(broker_symbols) - set(db_symbols))
    stale_cleanup_plan = build_stale_db_trade_cleanup_plan()
    stale_plan_count = int(stale_cleanup_plan.get('stale_count') or 0)
    stale_details = []
    for r in rows:
        sym = str(r['symbol'] or '').upper()
        if sym in unmatched_db:
            stale_details.append({'id': r['id'], 'symbol': sym, 'order_id': r['order_id'], 'order_status': r['order_status'], 'outcome': r['outcome'], 'created_at': r['created_at']})
    close_pending_symbols = list(protection.get('close_pending_symbols') or [])
    unsafe_symbols = list(protection.get('unsafe_protection_symbols') or [])
    status = 'PASS'
    if unsafe_symbols:
        status = 'FAIL_UNPROTECTED_POSITION'
    elif close_pending_symbols and not unmatched_db and not unmatched_broker:
        status = 'WARN_CLOSE_PENDING'
    elif unmatched_broker:
        status = 'FAIL_MISMATCH'
    elif unmatched_db:
        status = 'WARN_STALE_DB'
    return {
        'ok': status == 'PASS',
        'generated_at': now_et().isoformat(),
        'reconciliation_status': status,
        'db_open_trades_count': len(db_symbols),
        'broker_open_positions_count': len(broker_symbols),
        'broker_open_orders_count': len(broker_orders),
        'matched_symbols': sorted(set(db_symbols) & set(broker_symbols)),
        'unmatched_db_open_trades': unmatched_db,
        'unmatched_broker_positions': unmatched_broker,
        'unprotected_symbols': list(protection.get('unprotected_symbols') or []),
        'partial_symbols': list(protection.get('partial_symbols') or []),
        'unsafe_protection_symbols': unsafe_symbols,
        'close_pending_symbols': close_pending_symbols,
        'stale_open_db_trades': unmatched_db,
        'stale_db_cleanup_available': True,
        'stale_db_cleanup_plan_count': stale_plan_count,
        'stale_db_trade_details': stale_details,
        'next_action_hint': 'wait_for_close_order_fill' if status == 'WARN_CLOSE_PENDING' else ('protect_or_flatten_open_positions' if status == 'FAIL_UNPROTECTED_POSITION' else ('run_stale_db_trade_cleanup_plan' if status == 'WARN_STALE_DB' else ('reconcile_broker_positions' if status == 'FAIL_MISMATCH' else 'no_action'))),
    }


def build_stale_db_trade_cleanup_plan() -> dict:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, order_id, order_status, outcome, qty, entry_price, filled_avg_price, filled_qty, created_at, updated_at
            FROM trades
            WHERE outcome IS NULL OR outcome IN ('open', 'working_or_filled', 'partial_win', 'breakeven_or_small_win')
            """
        ).fetchall()
    broker_positions = get_open_positions() or []
    broker_orders = get_open_orders() or []
    broker_symbols = {str(p.get('symbol') or '').upper() for p in broker_positions if p.get('symbol')}
    broker_order_symbols = {str(o.get('symbol') or '').upper() for o in broker_orders if (o.get('symbol') and str(o.get('status') or '').lower() in {'new','accepted','pending_new','partially_filled','open','held','done_for_day'})}

    stale_trades = []
    for row in rows:
        sym = str(row['symbol'] or '').upper()
        if not sym:
            continue
        if sym in broker_symbols or sym in broker_order_symbols:
            continue
        stale_trades.append({
            'id': row['id'], 'symbol': sym, 'order_id': row['order_id'], 'order_status': row['order_status'], 'outcome': row['outcome'],
            'qty': row['qty'], 'entry_price': row['entry_price'], 'filled_avg_price': row['filled_avg_price'], 'filled_qty': row['filled_qty'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
        })
    stale_symbols = sorted({x['symbol'] for x in stale_trades})
    updates = [
        {'trade_id': item['id'], 'symbol': item['symbol'], 'current_outcome': item.get('outcome'), 'recommended_outcome': 'broker_position_missing', 'reason': 'no_matching_broker_position'}
        for item in stale_trades
    ]
    return {
        'ok': True,
        'generated_at': now_et().isoformat(),
        'stale_count': len(stale_trades),
        'stale_symbols': stale_symbols,
        'stale_trades': stale_trades,
        'recommended_updates': updates,
        'next_action_hint': 'review_stale_db_trade_cleanup' if updates else 'no_stale_db_trades',
        'broker_open_positions_count': len(broker_positions),
        'broker_open_orders_count': len(broker_orders),
    }


def _safe_reconciliation_compact() -> dict:
    try:
        rec = build_paper_position_reconciliation() or {}
        return {
            'stale_open_db_trades': rec.get('stale_open_db_trades', []),
            'close_pending_symbols': rec.get('close_pending_symbols', []),
            'unsafe_protection_symbols': rec.get('unsafe_protection_symbols', []),
            'next_action_hint': rec.get('next_action_hint'),
        }
    except Exception:
        return {'stale_open_db_trades': [], 'close_pending_symbols': [], 'unsafe_protection_symbols': [], 'next_action_hint': 'reconciliation_unavailable'}


execution_service.set_unprotected_position_checker(has_unprotected_open_position)


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
        'protection_summary': {'status': protection.get('status'), 'protection_status': protection.get('status'), 'unprotected_position_detected': protection.get('unprotected_position_detected'), 'unprotected_symbols': protection.get('unprotected_symbols', []), 'next_action_hint': protection.get('next_action_hint')},
        'close_pending_symbols': protection.get('close_pending_symbols', []),
        'unsafe_protection_symbols': protection.get('unsafe_protection_symbols', []),
        'next_action_hint': next_action_hint,
        'recent_scan_count': len(recent_scans),
    }


def build_market_session_heartbeat() -> dict:
    state = get_runtime_state()
    market_open, market_reason = market_open_for_auto_cycle()
    attempts = list(get_recent_auto_cycle_attempts(limit=10))
    latest = attempts[0] if attempts else None
    scheduler_running = bool(state.get('scheduler_running'))
    auto_scan_job_registered = bool(state.get('auto_scan_job_registered'))
    heartbeat_status = 'READY_MARKET_OPEN' if market_open else 'READY_WAITING_FOR_MARKET'
    next_hint = 'ready_waiting_for_next_cycle' if market_open else 'wait_for_market_open'
    if not scheduler_running:
        heartbeat_status, next_hint = 'BLOCKED', 'start_scheduler'
    if latest and latest.get('status') == 'failed':
        heartbeat_status, next_hint = 'ERROR', 'review_execution_error'
    elif latest and latest.get('status') == 'blocked':
        heartbeat_status, next_hint = 'BLOCKED', 'review_scan_diagnostics'
    elif latest and latest.get('status') == 'executed':
        pa = build_position_protection_audit() or {}
        open_positions_count = int(pa.get('open_positions_count') or 0)
        if open_positions_count == 0:
            heartbeat_status = 'TRADE_EXECUTED' if latest else ('READY_MARKET_OPEN' if market_open else 'READY_WAITING_FOR_MARKET')
            next_hint = 'ready_for_next_auto_cycle'
        elif pa.get('status') == 'PASS':
            heartbeat_status = 'POSITION_OPEN_PROTECTED'
            next_hint = 'monitor_open_trade'
        elif pa.get('status') == 'FAIL':
            heartbeat_status = 'POSITION_OPEN_UNPROTECTED'
            next_hint = 'review_unprotected_position'
        else:
            heartbeat_status = 'TRADE_ATTEMPTED'
            next_hint = 'review_auto_cycle_attempts'
    last_age = None
    if latest and latest.get('created_at'):
        try:
            last_age = int((datetime.now(timezone.utc) - datetime.fromisoformat(str(latest['created_at']).replace('Z', '+00:00'))).total_seconds())
        except Exception:
            last_age = None
    return {'ok': True, 'generated_at': now_et().isoformat(), 'market_status': {'market_open_for_auto_cycle': market_open, 'market_reason': market_reason}, 'scheduler_status': {'scheduler_running': scheduler_running, 'auto_scan_job_registered': auto_scan_job_registered}, 'readiness_summary': state.get('last_pre_market_readiness_pipeline') or {}, 'latest_cycle_attempt': latest, 'recent_cycle_attempts': attempts[:5], 'heartbeat_status': heartbeat_status, 'silence_detection': {'no_cycle_attempts_recorded': not bool(attempts), 'last_cycle_age_seconds': last_age, 'expected_scheduler_running': True, 'auto_scan_job_registered': auto_scan_job_registered, 'likely_silent_failure': bool(scheduler_running and auto_scan_job_registered and (market_open or not bool(getattr(config, 'AUTO_CYCLE_REQUIRE_MARKET_OPEN', True))) and ((not attempts) or (last_age is not None and last_age > 1800)))}, 'next_action_hint': next_hint}


def build_market_open_command_center() -> dict:
    state = get_runtime_state()
    checklist = build_deployment_checklist(state)
    observer = build_first_trade_observer_snapshot()
    protection = build_position_protection_audit()
    heartbeat = build_market_session_heartbeat()
    attempts = list(get_recent_auto_cycle_attempts(limit=10))
    latest = attempts[0] if attempts else None
    market_open, market_reason = market_open_for_auto_cycle()
    pipeline = state.get('last_pre_market_readiness_pipeline') or {}
    scheduler_running = bool(state.get('scheduler_running'))
    auto_scan_job_registered = bool(state.get('auto_scan_job_registered'))
    emergency_stop = bool(state.get('emergency_stop_active'))
    operator_pause = bool(state.get('operator_auto_trade_paused'))
    has_recent_rehearsal = bool(state.get('last_market_open_rehearsal'))
    has_executable_plan = bool((state.get('last_auto_cycle_plan') or {}).get('executable_count'))
    has_any_plan = bool(state.get('last_auto_cycle_plan'))
    safe_to_enable = bool(pipeline.get('safe_to_enable_auto_cycle'))
    scheduler_armed = scheduler_running and auto_scan_job_registered
    latest_status = (latest or {}).get('status')
    heartbeat_status = heartbeat.get('heartbeat_status')

    primary_action = 'review_bot_status'
    if emergency_stop:
        primary_action = 'clear_or_review_emergency_stop'
    elif operator_pause:
        primary_action = 'resume_or_review_operator_pause'
    elif protection.get('status') == 'FAIL':
        primary_action = 'review_unprotected_position'
    elif heartbeat_status == 'ERROR':
        primary_action = 'review_execution_error'
    elif latest_status == 'failed':
        primary_action = 'review_auto_cycle_failure'
    elif not pipeline:
        primary_action = 'run_pre_market_readiness_pipeline'
    elif not safe_to_enable:
        primary_action = 'review_pre_market_pipeline'
    elif not scheduler_armed:
        primary_action = 'start_or_fix_scheduler'
    elif not market_open:
        primary_action = 'wait_for_market_open'
    elif (not has_any_plan) or (not has_executable_plan):
        primary_action = 'run_auto_cycle_plan'
    elif not has_recent_rehearsal:
        primary_action = 'run_market_open_rehearsal'
    elif market_open and scheduler_armed and not latest:
        primary_action = 'watch_for_next_scheduler_cycle'
    elif latest_status == 'planned':
        primary_action = 'watch_for_execution_or_review_blockers'
    elif latest_status == 'executed' and int(protection.get('open_positions_count') or 0) == 0:
        primary_action = 'ready_for_next_cycle'
    elif latest_status == 'executed' and int(protection.get('open_positions_count') or 0) > 0 and protection.get('status') == 'PASS':
        primary_action = 'monitor_open_trade'

    command_center_status = 'READY_FOR_PLAN_CHECK'
    if emergency_stop:
        command_center_status = 'ERROR_REVIEW_REQUIRED'
    elif operator_pause:
        command_center_status = 'SCHEDULER_NOT_READY'
    elif protection.get('status') == 'FAIL':
        command_center_status = 'POSITION_OPEN_UNPROTECTED'
    elif heartbeat_status == 'ERROR':
        command_center_status = 'ERROR_REVIEW_REQUIRED'
    elif latest_status == 'failed':
        command_center_status = 'TRADE_ATTEMPTED'
    elif not pipeline:
        command_center_status = 'READY_FOR_PLAN_CHECK'
    elif not safe_to_enable:
        command_center_status = 'PLAN_BLOCKED'
    elif not scheduler_armed:
        command_center_status = 'SCHEDULER_NOT_READY'
    elif not market_open:
        command_center_status = 'WAITING_FOR_MARKET'
    elif latest_status == 'executed' and int(protection.get('open_positions_count') or 0) > 0 and protection.get('status') == 'PASS':
        command_center_status = 'POSITION_OPEN_PROTECTED'
    elif latest_status == 'executed':
        command_center_status = 'TRADE_EXECUTED'
    elif latest_status in {'planned', 'blocked', 'skipped'}:
        command_center_status = 'AUTO_CYCLE_ACTIVE'
    elif market_open and scheduler_armed:
        command_center_status = 'READY_FOR_PAPER_AUTO_CYCLE'
    elif has_executable_plan:
        command_center_status = 'READY_FOR_SCHEDULER'

    return {
        'ok': True,
        'generated_at': now_et().isoformat(),
        'market_status': {'open_for_auto_cycle': market_open, 'reason': market_reason},
        'command_center_status': command_center_status,
        'primary_action': primary_action,
        'secondary_actions': [heartbeat.get('next_action_hint'), observer.get('next_action_hint'), checklist.get('next_required_action')],
        'readiness_cards': {
            'paper_readiness': {'status': (state.get('last_paper_readiness_preflight') or {}).get('overall_status'), 'ok': (state.get('last_paper_readiness_preflight') or {}).get('ok'), 'next_action': (state.get('last_paper_readiness_preflight') or {}).get('next_action_hint')},
            'pipeline': {'status': pipeline.get('overall_status'), 'go_no_go': pipeline.get('go_no_go'), 'safe_to_enable_auto_cycle': pipeline.get('safe_to_enable_auto_cycle'), 'next_action': pipeline.get('next_required_action')},
            'scheduler': {'running': scheduler_running, 'auto_scan_job_registered': auto_scan_job_registered, 'status': 'READY' if scheduler_armed else 'NOT_READY'},
            'market': {'open_for_auto_cycle': market_open, 'reason': market_reason},
            'latest_attempt': {'status': (latest or {}).get('status'), 'source': (latest or {}).get('source'), 'symbol': (latest or {}).get('attempted_symbol'), 'qty': (latest or {}).get('attempted_qty'), 'created_at': (latest or {}).get('created_at')},
            'protection': {'status': protection.get('status'), 'open_positions_count': protection.get('open_positions_count'), 'next_action': protection.get('next_action_hint')},
            'heartbeat': {'status': heartbeat_status, 'next_action': heartbeat.get('next_action_hint')},
        },
        'latest_attempt': latest,
        'protection_summary': {'status': protection.get('status'), 'open_positions_count': protection.get('open_positions_count'), 'unprotected_position_detected': protection.get('unprotected_position_detected')},
        'close_pending_symbols': protection.get('close_pending_symbols', []),
        'unsafe_protection_symbols': protection.get('unsafe_protection_symbols', []),
        'stale_open_db_trades': _safe_reconciliation_compact().get('stale_open_db_trades', []),
        'scheduler_summary': {'running': scheduler_running, 'auto_scan_job_registered': auto_scan_job_registered, 'armed': scheduler_armed},
        'safety_summary': {'emergency_stop_active': emergency_stop, 'operator_pause_active': operator_pause, 'safe_to_enable_auto_cycle': safe_to_enable},
        'operator_warnings': [w for w in [state.get('last_auto_trade_error'), state.get('last_scan_error'), state.get('last_market_session_heartbeat_error')] if w],
    }


def _iso_day_et(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(now_et().tzinfo).date().isoformat()
    except Exception:
        return None


def _attempt_skip_reasons(attempt: dict) -> list[str]:
    raw = attempt.get('skip_reasons')
    if raw is None:
        raw = attempt.get('skip_reasons_json')
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None and str(x).strip()]
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    return [str(raw)]


def _attempt_top_blockers(attempt: dict) -> dict:
    raw = attempt.get('top_blockers')
    if raw is None:
        raw = attempt.get('top_blockers_json')
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, str) and item.strip():
                out[item] = out.get(item, 0) + 1
            elif isinstance(item, dict):
                for k, v in item.items():
                    out[str(k)] = v
        return out
    if isinstance(raw, str):
        return {raw: 1} if raw.strip() else {}
    return {str(raw): 1}


def build_paper_validation_session_report(day: str | None = None) -> dict:
    market_day = day or now_et().date().isoformat()
    state = get_runtime_state() or {}
    attempts_recent = [a for a in list(get_recent_auto_cycle_attempts(limit=200)) if _iso_day_et(a.get('created_at')) == market_day]
    attempts_chronological = sorted(attempts_recent, key=lambda a: str(a.get('created_at') or ''))
    attempts = attempts_recent
    recent_trades = [t for t in (get_recent_trades() or []) if _iso_day_et(t.get('created_at')) == market_day][:10]
    observer = build_first_trade_observer_snapshot() or {}
    protection = build_position_protection_audit() or {}
    heartbeat = build_market_session_heartbeat() or {}
    pipeline = state.get('last_pre_market_readiness_pipeline') or {}
    launch_gate = state.get('last_paper_market_launch_gate') or {}

    scan_or_plan_count = sum(1 for a in attempts if int(a.get('candidate_count') or 0) > 0 or a.get('status') == 'planned')
    executable_plan_count = sum(1 for a in attempts if int(a.get('executable_count') or 0) > 0)
    execution_attempt_count = sum(1 for a in attempts if (a.get('attempted_symbol') or a.get('status') == 'executed'))
    executed = [a for a in attempts if a.get('status') == 'executed']
    executed_chronological = [a for a in attempts_chronological if a.get('status') == 'executed']
    failed = [a for a in attempts if a.get('status') == 'failed']
    blocked = [a for a in attempts if a.get('status') in {'blocked', 'skipped'}]
    latest = attempts[0] if attempts else {}
    first = executed_chronological[0] if executed_chronological else (attempts_chronological[0] if attempts_chronological else {})
    qty_source = 'first_trade_final_qty'
    qty_value = first.get('first_trade_final_qty')
    if qty_value is None:
        qty_source = 'attempted_qty'
        qty_value = first.get('attempted_qty')
    qty, qty_issue = None, None
    try:
        qty = int(qty_value) if qty_value is not None else None
    except Exception:
        qty_issue = 'invalid_qty'
    if qty is None:
        qty_issue = qty_issue or 'missing_qty'
    elif qty < 1:
        qty_issue = 'qty_must_be_gte_1'
    elif qty > int(config.FIRST_TRADE_MAX_QTY):
        qty_issue = 'qty_exceeds_cap'

    risk_value = first.get('first_trade_risk_dollars')
    risk_dollars, risk_issue = None, None
    try:
        risk_dollars = float(risk_value) if risk_value is not None else None
    except Exception:
        risk_issue = 'invalid_risk'
    if risk_dollars is None:
        risk_issue = risk_issue or 'missing_risk'
    elif risk_dollars <= 0:
        risk_issue = 'risk_must_be_gt_0'
    elif risk_dollars > float(config.FIRST_TRADE_MAX_DOLLAR_RISK):
        risk_issue = 'risk_exceeds_cap'
    within_limits = qty_issue is None and risk_issue is None
    top_blockers = []
    skip_reasons = []
    execution_errors = []
    for a in attempts:
        tb = _attempt_top_blockers(a)
        if a.get('status') in {'blocked', 'skipped'}:
            top_blockers.extend(list(tb.keys()))
            skip_reasons.extend(_attempt_skip_reasons(a))
        if a.get('execution_error'):
            execution_errors.append(a.get('execution_error'))
    explicit_no_trade = {'market_closed', 'outside_window', 'no_executable_candidate', 'spread_too_wide', 'no_quote', 'data_unavailable', 'daily_loss_limit', 'max_trades_reached', 'scheduler_not_running', 'auto_trading_disabled', 'paper_preflight_blocked', 'paper_account_blocked'}
    combined_reasons = set(skip_reasons + top_blockers)
    has_explicit_blocker = any(any(tag in str(r) for tag in explicit_no_trade) for r in combined_reasons)
    unprotected = bool(protection.get('unprotected_position_detected'))

    report_status, acceptance_pass = 'BLOCKED_NO_VALIDATION', False
    required_actions, warnings = [], []
    if executed_chronological[1:]:
        for later in executed_chronological[1:]:
            lqty = later.get('first_trade_final_qty') if later.get('first_trade_final_qty') is not None else later.get('attempted_qty')
            lrisk = later.get('first_trade_risk_dollars')
            try:
                lqty_ok = 1 <= int(lqty) <= int(config.FIRST_TRADE_MAX_QTY)
            except Exception:
                lqty_ok = False
            try:
                lrisk_ok = 0 < float(lrisk) <= float(config.FIRST_TRADE_MAX_DOLLAR_RISK)
            except Exception:
                lrisk_ok = False
            if not (lqty_ok and lrisk_ok):
                warnings.append('later_invalid_attempt')
                break
    if not attempts:
        required_actions.append('ensure_scheduler_or_manual_auto_cycle_runs')
    elif unprotected:
        report_status = 'UNPROTECTED_POSITION_REVIEW'
        required_actions.append('protect_or_flatten_open_positions')
    elif executed:
        governor_applied = bool(first.get('first_trade_governor_applied'))
        if governor_applied and within_limits:
            report_status, acceptance_pass = 'ACCEPTED_PAPER_VALIDATION', True
        else:
            report_status = 'REVIEW_REQUIRED'
            required_actions.append('review_first_trade_governor_or_risk_caps')
    elif failed and not executed:
        report_status = 'REVIEW_REQUIRED'
        required_actions.append('review_failed_execution_attempts')
    elif has_explicit_blocker:
        report_status, acceptance_pass = 'NO_TRADE_BUT_EXPLAINED', True
    elif executable_plan_count > 0:
        report_status = 'PARTIAL_PAPER_VALIDATION'
        warnings.append('executable_plan_seen_without_execution')
    else:
        report_status = 'BLOCKED_NO_VALIDATION'

    return {
        'ok': True,
        'generated_at': now_et().isoformat(),
        'market_day': market_day,
        'report_status': report_status,
        'acceptance_pass': acceptance_pass,
        'summary': {
            'cycle_attempt_count': len(attempts), 'scan_or_plan_count': scan_or_plan_count, 'executable_plan_count': executable_plan_count,
            'execution_attempt_count': execution_attempt_count, 'executed_trade_count': len(executed), 'failed_attempt_count': len(failed),
            'blocked_attempt_count': len(blocked), 'latest_attempt_status': latest.get('status'),
        },
        'first_trade_review': {
            'attempted': bool(execution_attempt_count), 'executed': bool(executed), 'symbol': first.get('attempted_symbol'), 'qty': qty,
            'probe_trade': bool(first.get('probe_trade')), 'first_trade_governor_applied': bool(first.get('first_trade_governor_applied')),
            'first_trade_final_qty': first.get('first_trade_final_qty'), 'first_trade_risk_dollars': first.get('first_trade_risk_dollars'),
            'within_first_trade_limits': within_limits, 'order_status': first.get('order_status'),
            'first_trade_qty_source': qty_source if qty_value is not None else None,
            'first_trade_limit_issue': ','.join([x for x in [qty_issue, risk_issue] if x]) or None,
        },
        'protection_review': {'open_positions_count': protection.get('open_positions_count'), 'protection_status': protection.get('status'), 'unprotected_position_detected': unprotected},
        'close_pending_symbols': protection.get('close_pending_symbols', []),
        'unsafe_protection_symbols': protection.get('unsafe_protection_symbols', []),
        'stale_open_db_trades': _safe_reconciliation_compact().get('stale_open_db_trades', []),
        'closeout_review': {'open_position_remaining': int(protection.get('open_positions_count') or 0) > 0, 'eod_flatten_seen': 'eod' in ' '.join(skip_reasons).lower(), 'stale_exit_seen': 'stale' in ' '.join(skip_reasons).lower(), 'quick_profit_seen': 'profit' in ' '.join(skip_reasons).lower(), 'stopped_out_seen': 'stop' in ' '.join(skip_reasons).lower()},
        'blockers': {'top_blockers': sorted(set(top_blockers))[:10], 'skip_reasons': sorted(set(skip_reasons))[:10], 'execution_errors': execution_errors[:5]},
        'required_actions': sorted(set(required_actions)),
        'warnings': warnings,
        'evidence': {'recent_attempts': attempts_recent[:5], 'recent_trades': recent_trades[:5]},
        'context': {'heartbeat_status': heartbeat.get('heartbeat_status'), 'pipeline_status': pipeline.get('overall_status'), 'launch_gate_status': launch_gate.get('launch_gate_status'), 'observer_next_action': observer.get('next_action_hint')},
    }




def build_operator_safe_endpoint_health() -> dict:
    expected_envelope = {'ok': 'boolean', 'data': 'any'}
    endpoints = [
        {
            'label': e['label'],
            'method': e['method'],
            'path': e['path'],
            'expected_envelope': expected_envelope,
            'mutates_orders': False,
            'requires_market_open': bool(e['requires_market_open']),
            'notes': e['notes'],
        }
        for e in OPERATOR_SAFE_ENDPOINTS
    ]
    expected = {(e['method'], e['path']) for e in OPERATOR_SAFE_ENDPOINTS}
    observed = {(e['method'], e['path']) for e in endpoints}
    missing_expected = [f"{m} {p}" for m, p in sorted(expected - observed)]
    forbidden = [f"{e['method']} {e['path']}" for e in OPERATOR_FORBIDDEN_ENDPOINTS]
    unexpected_forbidden_present = sorted(set(forbidden) & {f"{e['method']} {e['path']}" for e in endpoints})
    next_action_hint = 'Endpoint contract clean. Run safe diagnostics from /operator before market open.'
    if missing_expected:
        next_action_hint = 'Endpoint contract drift detected. Restore missing safe endpoints before market open checks.'
    elif unexpected_forbidden_present:
        next_action_hint = 'Forbidden endpoints leaked into safe health set. Remove before using /operator.'
    return {
        'ok': len(missing_expected) == 0 and len(unexpected_forbidden_present) == 0,
        'generated_at': now_et().isoformat(),
        'endpoint_count': len(endpoints),
        'endpoints': endpoints,
        'forbidden_endpoints': forbidden,
        'missing_expected_endpoints': missing_expected,
        'unexpected_forbidden_present': unexpected_forbidden_present,
        'next_action_hint': next_action_hint,
        **_operator_auth_status(),
        'operator_auth_protects_root': True,
    }

def build_paper_market_launch_gate() -> dict:
    state = get_runtime_state() or {}
    endpoint_health = build_operator_safe_endpoint_health() or {}
    command_center = build_market_open_command_center() or {}
    deployment_checklist = build_deployment_checklist(state) or {}
    heartbeat = build_market_session_heartbeat() or {}
    observer = build_first_trade_observer_snapshot() or {}
    protection_audit = build_position_protection_audit() or {}
    attempts = list(get_recent_auto_cycle_attempts(limit=10))
    latest_attempt = attempts[0] if attempts else None
    market_open, market_reason = market_open_for_auto_cycle()
    pipeline = state.get('last_pre_market_readiness_pipeline') or {}

    blocking_reasons, warnings, required_actions = [], [], []
    statuses = []
    scheduler_running = bool(state.get('scheduler_running'))
    auto_scan_job_registered = bool(state.get('auto_scan_job_registered'))
    emergency_stop = bool(state.get('emergency_stop_active'))
    operator_pause = bool(state.get('operator_auto_trade_paused'))
    paper_or_sim = bool(config.SIMULATION_MODE) or bool(config.PAPER_TRADING_DETECTED)
    live_override = bool(getattr(config, 'LIVE_TRADING_OVERRIDE', False))
    first_trade_governor_enabled = bool(getattr(config, 'FIRST_TRADE_GOVERNOR_ENABLED', False))
    first_trade_max_qty_ok = int(getattr(config, 'FIRST_TRADE_MAX_QTY', 0) or 0) >= 1
    first_trade_risk_ok = float(getattr(config, 'FIRST_TRADE_MAX_DOLLAR_RISK', 0) or 0) > 0

    if not paper_or_sim:
        statuses.append('BLOCKED_READINESS')
        blocking_reasons.append('paper_or_sim_required')
        required_actions.append('set_paper_or_sim_mode')
    if live_override:
        statuses.append('BLOCKED_SAFETY')
        blocking_reasons.append('live_trading_override_active')
        required_actions.append('disable_live_trading_override')
    if not first_trade_governor_enabled:
        statuses.append('BLOCKED_SAFETY')
        blocking_reasons.append('first_trade_governor_disabled')
        required_actions.append('enable_first_trade_governor')
    if not first_trade_max_qty_ok:
        statuses.append('BLOCKED_SAFETY')
        blocking_reasons.append('first_trade_max_qty_invalid')
        required_actions.append('set_first_trade_max_qty')
    if not first_trade_risk_ok:
        statuses.append('BLOCKED_SAFETY')
        blocking_reasons.append('first_trade_max_dollar_risk_invalid')
        required_actions.append('set_first_trade_max_dollar_risk')
    if not pipeline:
        statuses.append('BLOCKED_READINESS')
        blocking_reasons.append('missing_pre_market_pipeline')
        required_actions.append('run_pre_market_readiness_pipeline')
    elif not bool(pipeline.get('safe_to_enable_auto_cycle')):
        pipeline_next_action = str(pipeline.get('next_required_action') or '')
        if pipeline_next_action in {'resume_auto_trading', 'resume_or_review_operator_pause'}:
            statuses.append('BLOCKED_SAFETY')
            blocking_reasons.append('operator_pause_active')
            required_actions.append('resume_or_review_operator_pause')
        else:
            statuses.append('BLOCKED_READINESS')
            blocking_reasons.append('pre_market_pipeline_not_safe')
            required_actions.append('resolve_pipeline_blockers')
    if not scheduler_running or not auto_scan_job_registered:
        statuses.append('BLOCKED_SCHEDULER')
        if not scheduler_running:
            blocking_reasons.append('scheduler_not_running')
            required_actions.append('start_scheduler')
        if not auto_scan_job_registered:
            blocking_reasons.append('auto_scan_job_not_registered')
            required_actions.append('register_auto_scan_job')
    if emergency_stop:
        statuses.append('BLOCKED_SAFETY')
        blocking_reasons.append('emergency_stop_active')
        required_actions.append('clear_or_review_emergency_stop')
    if operator_pause:
        statuses.append('BLOCKED_SAFETY')
        blocking_reasons.append('operator_pause_active')
        required_actions.append('resume_or_review_operator_pause')
    if not bool(endpoint_health.get('ok')):
        statuses.append('BLOCKED_ENDPOINT_CONTRACT')
        blocking_reasons.append('operator_safe_endpoint_health_not_ok')
        required_actions.append('fix_operator_safe_endpoint_contract')
    if bool(protection_audit.get('unprotected_position_detected')):
        statuses.append('BLOCKED_UNPROTECTED_POSITION')
        blocking_reasons.append('unprotected_open_position')
        required_actions.append('review_unprotected_position')

    latest_status = (latest_attempt or {}).get('status')
    if latest_status == 'failed':
        statuses.append('BLOCKED_REVIEW_REQUIRED')
        blocking_reasons.append('latest_attempt_failed')
        required_actions.append('review_execution_error')
    if latest_status == 'executed' and int(protection_audit.get('open_positions_count') or 0) > 0:
        if protection_audit.get('status') == 'PASS':
            warnings.append('protected_open_position_active')
            required_actions.append('monitor_open_trade')
        elif protection_audit.get('status') == 'FAIL':
            statuses.append('BLOCKED_UNPROTECTED_POSITION')
            blocking_reasons.append('executed_attempt_with_unprotected_open_position')
            required_actions.append('review_unprotected_position')

    critical_blocker = bool(statuses)
    if critical_blocker:
        launch_gate_status = statuses[0]
        go_for_paper_validation = False
        may_leave_scheduler_armed = False
        may_run_manual_auto_cycle_now = False
    else:
        go_for_paper_validation = True
        may_leave_scheduler_armed = True
        rehearsal_ready = bool((state.get('last_market_open_rehearsal') or {}).get('ready_for_paper_session'))
        command_ready = command_center.get('command_center_status') in {'READY_FOR_PAPER_AUTO_CYCLE', 'AUTO_CYCLE_ACTIVE', 'READY_FOR_SCHEDULER'}
        if market_open:
            launch_gate_status = 'GO_FOR_PAPER_MARKET_VALIDATION'
            may_run_manual_auto_cycle_now = bool(rehearsal_ready or command_ready)
        else:
            launch_gate_status = 'WAIT_FOR_MARKET_OPEN'
            may_run_manual_auto_cycle_now = False
            warnings.append(market_reason or 'market_closed')
            required_actions.append('wait_for_market_open')

    return {
        'ok': True,
        'generated_at': now_et().isoformat(),
        'launch_gate_status': launch_gate_status,
        'go_for_paper_validation': go_for_paper_validation,
        'may_leave_scheduler_armed': may_leave_scheduler_armed,
        'may_run_manual_auto_cycle_now': may_run_manual_auto_cycle_now,
        'blocking_reasons': sorted(set(blocking_reasons)),
        'warnings': sorted(set(warnings)),
        'required_actions': sorted(set(required_actions)),
        'close_pending_symbols': protection_audit.get('close_pending_symbols', []),
        'unsafe_protection_symbols': protection_audit.get('unsafe_protection_symbols', []),
        'stale_open_db_trades': _safe_reconciliation_compact().get('stale_open_db_trades', []),
        'next_action_hint': heartbeat.get('next_action_hint'),
        'evidence': {
            'paper_or_sim': paper_or_sim,
            'live_trading_override': live_override,
            'first_trade_governor_enabled': first_trade_governor_enabled,
            'first_trade_max_qty_ok': first_trade_max_qty_ok,
            'first_trade_risk_ok': first_trade_risk_ok,
            'pipeline_safe_to_enable_auto_cycle': bool(pipeline.get('safe_to_enable_auto_cycle')) if pipeline else False,
            'scheduler_running': scheduler_running,
            'auto_scan_job_registered': auto_scan_job_registered,
            'emergency_stop_active': emergency_stop,
            'operator_pause_active': operator_pause,
            'market_open_for_auto_cycle': market_open,
            'latest_attempt_status': latest_status,
        },
        'endpoint_health': {'ok': endpoint_health.get('ok'), 'next_action_hint': endpoint_health.get('next_action_hint')},
        'command_center': {'status': command_center.get('command_center_status'), 'primary_action': command_center.get('primary_action')},
        'deployment_checklist': {'status': deployment_checklist.get('deployment_status'), 'next_required_action': deployment_checklist.get('next_required_action')},
        'heartbeat': {'status': heartbeat.get('heartbeat_status'), 'next_action_hint': heartbeat.get('next_action_hint')},
        'protection_audit': {'status': protection_audit.get('status'), 'open_positions_count': protection_audit.get('open_positions_count'), 'unprotected_position_detected': protection_audit.get('unprotected_position_detected')},
        'latest_attempt': latest_attempt,
    }


@app.route('/api/operator-safe-endpoint-health', methods=['GET'])
def api_operator_safe_endpoint_health():
    return ok(build_operator_safe_endpoint_health())

@app.route('/api/market-session-heartbeat', methods=['GET'])
def api_market_session_heartbeat():
    try:
        hb = build_market_session_heartbeat()
        RUNTIME_STATE['last_market_session_heartbeat'] = {'generated_at': hb.get('generated_at'), 'heartbeat_status': hb.get('heartbeat_status'), 'next_action_hint': hb.get('next_action_hint')}
        RUNTIME_STATE['last_market_session_heartbeat_at'] = now_et().isoformat()
        RUNTIME_STATE['last_market_session_heartbeat_error'] = None
        return ok(hb)
    except Exception as exc:
        RUNTIME_STATE['last_market_session_heartbeat_error'] = str(exc)
        RUNTIME_STATE['last_market_session_heartbeat_at'] = now_et().isoformat()
        return fail('market_session_heartbeat_failed', 500)


@app.route('/api/auto-cycle-attempts', methods=['GET'])
def api_auto_cycle_attempts():
    limit = min(max(int(request.args.get('limit', 20) or 20), 1), 100)
    return ok({'items': list(get_recent_auto_cycle_attempts(limit=limit)), 'limit': limit})


@app.route('/api/paper-position-reconciliation', methods=['GET'])
def api_paper_position_reconciliation():
    try:
        payload = build_paper_position_reconciliation()
        RUNTIME_STATE['last_paper_position_reconciliation'] = {'generated_at': payload.get('generated_at'), 'reconciliation_status': payload.get('reconciliation_status'), 'unprotected_symbols': payload.get('unprotected_symbols', [])}
        RUNTIME_STATE['last_paper_position_reconciliation_at'] = now_et().isoformat()
        RUNTIME_STATE['last_paper_position_reconciliation_error'] = None
        return ok(payload)
    except Exception as exc:
        RUNTIME_STATE['last_paper_position_reconciliation_error'] = str(exc)
        RUNTIME_STATE['last_paper_position_reconciliation_at'] = now_et().isoformat()
        return fail('paper_position_reconciliation_failed', 500)


@app.route('/api/stale-db-trade-cleanup-plan', methods=['GET'])
def api_stale_db_trade_cleanup_plan():
    try:
        payload = build_stale_db_trade_cleanup_plan()
        RUNTIME_STATE['last_stale_db_trade_cleanup_plan'] = {'generated_at': payload.get('generated_at'), 'stale_count': payload.get('stale_count'), 'stale_symbols': payload.get('stale_symbols', [])}
        RUNTIME_STATE['last_stale_db_trade_cleanup_plan_at'] = now_et().isoformat()
        RUNTIME_STATE['last_stale_db_trade_cleanup_plan_error'] = None
        return ok(payload)
    except Exception as exc:
        RUNTIME_STATE['last_stale_db_trade_cleanup_plan_error'] = str(exc)
        RUNTIME_STATE['last_stale_db_trade_cleanup_plan_at'] = now_et().isoformat()
        return fail('stale_db_trade_cleanup_plan_failed', 500)


@app.route('/api/stale-db-trade-cleanup-apply', methods=['POST'])
def api_stale_db_trade_cleanup_apply():
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'MARK_STALE_DB_TRADES':
        return fail('invalid_confirm', 400)
    try:
        plan = build_stale_db_trade_cleanup_plan() or {}
        stale_trades = list(plan.get('stale_trades') or [])
        now = db.utc_now()
        updated_ids, updated_symbols = [], []
        with db.get_conn() as conn:
            for item in stale_trades:
                tid = item.get('id')
                row = conn.execute('SELECT notes FROM trades WHERE id = ?', (tid,)).fetchone()
                if not row:
                    continue
                prev_notes = (row['notes'] or '').strip()
                suffix = 'Marked stale by broker/DB reconciliation cleanup.'
                notes = (prev_notes + "\n" + suffix) if prev_notes else suffix
                conn.execute('UPDATE trades SET outcome = ?, notes = ?, updated_at = ? WHERE id = ?', ('broker_position_missing', notes, now, tid))
                updated_ids.append(tid)
                updated_symbols.append(item.get('symbol'))
        remaining = build_stale_db_trade_cleanup_plan().get('stale_count', 0)
        payload = {'ok': True, 'updated_count': len(updated_ids), 'updated_trade_ids': updated_ids, 'updated_symbols': sorted(set([s for s in updated_symbols if s])), 'remaining_stale_count': remaining}
        RUNTIME_STATE['last_stale_db_trade_cleanup_apply'] = payload
        RUNTIME_STATE['last_stale_db_trade_cleanup_apply_at'] = now_et().isoformat()
        RUNTIME_STATE['last_stale_db_trade_cleanup_apply_error'] = None
        return ok(payload)
    except Exception as exc:
        RUNTIME_STATE['last_stale_db_trade_cleanup_apply_error'] = str(exc)
        RUNTIME_STATE['last_stale_db_trade_cleanup_apply_at'] = now_et().isoformat()
        return fail('stale_db_trade_cleanup_apply_failed', 500)

@app.route('/api/paper-validation-session-report', methods=['GET'])
def api_paper_validation_session_report():
    try:
        day = (request.args.get('day') or '').strip() or None
        payload = build_paper_validation_session_report(day=day)
        RUNTIME_STATE['last_paper_validation_session_report'] = {
            'generated_at': payload.get('generated_at'),
            'market_day': payload.get('market_day'),
            'report_status': payload.get('report_status'),
            'acceptance_pass': payload.get('acceptance_pass'),
        }
        RUNTIME_STATE['last_paper_validation_session_report_at'] = now_et().isoformat()
        RUNTIME_STATE['last_paper_validation_session_report_error'] = None
        return ok(payload)
    except Exception as exc:
        RUNTIME_STATE['last_paper_validation_session_report_error'] = str(exc)
        RUNTIME_STATE['last_paper_validation_session_report_at'] = now_et().isoformat()
        return fail('paper_validation_session_report_failed', 500)


@app.route('/api/market-open-command-center', methods=['GET'])
def api_market_open_command_center():
    try:
        payload = build_market_open_command_center()
        RUNTIME_STATE['last_market_open_command_center'] = {
            'generated_at': payload.get('generated_at'),
            'command_center_status': payload.get('command_center_status'),
            'primary_action': payload.get('primary_action'),
        }
        RUNTIME_STATE['last_market_open_command_center_at'] = now_et().isoformat()
        RUNTIME_STATE['last_market_open_command_center_error'] = None
        return ok(payload)
    except Exception as exc:
        RUNTIME_STATE['last_market_open_command_center_error'] = str(exc)
        RUNTIME_STATE['last_market_open_command_center_at'] = now_et().isoformat()
        return fail('market_open_command_center_failed', 500)

@app.route('/api/paper-market-launch-gate', methods=['GET'])
def api_paper_market_launch_gate():
    try:
        payload = build_paper_market_launch_gate()
        RUNTIME_STATE['last_paper_market_launch_gate'] = {
            'generated_at': payload.get('generated_at'),
            'launch_gate_status': payload.get('launch_gate_status'),
            'go_for_paper_validation': payload.get('go_for_paper_validation'),
            'may_leave_scheduler_armed': payload.get('may_leave_scheduler_armed'),
        }
        RUNTIME_STATE['last_paper_market_launch_gate_at'] = now_et().isoformat()
        RUNTIME_STATE['last_paper_market_launch_gate_error'] = None
        return ok(payload)
    except Exception as exc:
        RUNTIME_STATE['last_paper_market_launch_gate_error'] = str(exc)
        RUNTIME_STATE['last_paper_market_launch_gate_at'] = now_et().isoformat()
        return fail('paper_market_launch_gate_failed', 500)


@app.route('/operator')
def operator_readiness_page():
    return render_template('index.html', app_title="Veteran Pro")


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
        **_operator_auth_status(),
        'operator_auth_protects_root': True,
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
            'last_market_session_heartbeat': state.get('last_market_session_heartbeat'),
            'last_market_session_heartbeat_at': state.get('last_market_session_heartbeat_at'),
            'last_market_session_heartbeat_error': state.get('last_market_session_heartbeat_error'),
            'last_market_open_command_center': state.get('last_market_open_command_center'),
            'last_market_open_command_center_at': state.get('last_market_open_command_center_at'),
            'last_market_open_command_center_error': state.get('last_market_open_command_center_error'),
            'last_paper_market_launch_gate': state.get('last_paper_market_launch_gate'),
            'last_paper_market_launch_gate_at': state.get('last_paper_market_launch_gate_at'),
            'last_paper_market_launch_gate_error': state.get('last_paper_market_launch_gate_error'),
            'recent_auto_cycle_attempts_count': len(get_recent_auto_cycle_attempts(limit=5)),
            'latest_auto_cycle_attempt': (get_recent_auto_cycle_attempts(limit=1) or [None])[0],
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
            'last_market_session_heartbeat': state.get('last_market_session_heartbeat'),
            'last_market_session_heartbeat_at': state.get('last_market_session_heartbeat_at'),
            'last_market_session_heartbeat_error': state.get('last_market_session_heartbeat_error'),
            'last_market_open_command_center': state.get('last_market_open_command_center'),
            'last_market_open_command_center_at': state.get('last_market_open_command_center_at'),
            'last_market_open_command_center_error': state.get('last_market_open_command_center_error'),
            'last_paper_market_launch_gate': state.get('last_paper_market_launch_gate'),
            'last_paper_market_launch_gate_at': state.get('last_paper_market_launch_gate_at'),
            'last_paper_market_launch_gate_error': state.get('last_paper_market_launch_gate_error'),
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
