from datetime import datetime
from pathlib import Path
import sys
import types
import pytest

sys.modules.setdefault('dotenv', types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault('requests', types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None, patch=lambda *a, **k: None, delete=lambda *a, **k: None))

app = pytest.importorskip('app')


def test_bot_status_includes_latest_best_pick(monkeypatch):
    app.LATEST_SCAN = {'scan_id': 77, 'best_pick': {'symbol': 'XYZ', 'decision': 'BUY NOW'}}
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'last_scan_at': '2026-01-01T10:00:00'})
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {})

    client = app.app.test_client()
    resp = client.get('/api/bot-status')
    assert resp.status_code == 200
    data = resp.get_json()['data']
    assert data['latest_best_pick']['symbol'] == 'XYZ'
    assert data['latest_scan_id'] == 77


def test_auto_cycle_runs_in_paper_or_sim(monkeypatch):
    called = {'n': 0}
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(app, 'run_scan_and_maybe_auto_trade', lambda: called.__setitem__('n', called['n'] + 1))
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'last_auto_trade_attempts': []})
    app.LATEST_SCAN = {'best_pick': {'symbol': 'AAA'}}

    client = app.app.test_client()
    resp = client.post('/api/auto-cycle', json={})
    assert resp.status_code == 200
    assert called['n'] == 1


def test_auto_cycle_blocked_not_paper(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', False)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', False)
    client = app.app.test_client()
    resp = client.post('/api/auto-cycle', json={})
    assert resp.status_code == 409
    assert resp.get_json()['error'] == 'auto_cycle_blocked_not_paper'


def test_auto_cycle_outside_window_records_explicit_skip(monkeypatch):
    app.RUNTIME_STATE.clear()
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'outside_auto_scan_window'))
    app.run_scan_and_maybe_auto_trade()
    assert app.RUNTIME_STATE['last_scan_skipped_reason'] == 'outside_auto_scan_window'
    assert app.RUNTIME_STATE['last_auto_trade_error'] == 'outside_auto_scan_window'
    assert app.RUNTIME_STATE['last_auto_trade_skip_reasons'] == ['outside_auto_scan_window']
    assert app.RUNTIME_STATE['last_auto_trade_verdict'] == {'ok': False, 'skip_reasons': ['outside_auto_scan_window']}


def test_attempt_rows_include_required_fields(monkeypatch):
    app.RUNTIME_STATE.clear()
    monkeypatch.setattr(app, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'AAA', 'risk_dollars': 9.5}, 'watchlist': []})
    monkeypatch.setattr(app, 'insert_scan', lambda result: 5)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'now_et', lambda: datetime(2026, 1, 1, 10, 0, 0))
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {
        'ok': False,
        'entry_trigger': 'breakout',
        'fallback_used': False,
        'skip_reasons': ['blocked'],
        'fallback_reasons': ['spread_too_wide'],
        'hard_blockers_overridden': ['oversized_risk'],
        'soft_blockers_overridden': ['setup_grade_not_allowed'],
    })

    app.run_scan_and_maybe_auto_trade()
    attempt = app.RUNTIME_STATE['last_auto_trade_attempts'][0]
    for key in ['symbol', 'ok', 'skip_reasons', 'fallback_used', 'risk_dollars', 'entry_trigger', 'fallback_reasons', 'hard_blockers_overridden', 'overridden_blockers']:
        assert key in attempt
    assert 'hard_overridden:oversized_risk' in attempt['probe_reasons']


def test_market_open_for_auto_cycle_reason_labels(monkeypatch):
    monkeypatch.setattr(app.config, 'AUTO_CYCLE_REQUIRE_MARKET_OPEN', True)
    monkeypatch.setattr(app, 'get_clock', lambda: {'is_open': False})
    open_ok, reason = app.market_open_for_auto_cycle()
    assert open_ok is False and reason == 'market_closed'

    def _raise():
        raise RuntimeError('clock down')
    monkeypatch.setattr(app, 'get_clock', _raise)
    open_ok, reason = app.market_open_for_auto_cycle()
    assert open_ok is False
    assert reason.startswith('market_clock_unavailable:')


def test_bot_status_next_action_hint_priority_and_blockers(monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': False, 'operator_auto_trade_paused': False, 'emergency_stop_active': False})
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {})
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: app.config.MAX_AUTO_TRADES_PER_DAY)
    monkeypatch.setattr(app, 'estimated_daily_loss_risk_used_today', lambda: app.config.CURRENT_BANKROLL * app.config.MAX_DAILY_REALIZED_LOSS_PCT)
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))

    client = app.app.test_client()
    data = client.get('/api/bot-status').get_json()['data']
    assert 'scheduler_not_running' in data['auto_cycle_blockers']
    assert 'max_auto_trades_reached' in data['auto_cycle_blockers']
    assert 'daily_loss_limit_reached' in data['auto_cycle_blockers']
    assert data['next_action_hint'] == 'scheduler_not_running'


def test_minimal_ui_contains_new_and_excludes_old_markers():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    for marker in [
        'Run Auto Cycle',
        'Scan Only — No Trade',
        'Trade Readiness',
        'Market Reason',
        'market_clock_unavailable',
        'ALPACA_PAPER_BASE should be https://paper-api.alpaca.markets without /v2',
        'normalizeBestTrade',
        'confirm(',
    ]:
        assert marker in html
    for marker in ['LightweightCharts', 'Live Charts', 'Rejected Candidates', 'Engine Room', 'Top Market Candidates', 'chart-box']:
        assert marker not in html
