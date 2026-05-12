import importlib
from datetime import datetime

import pytest

app = pytest.importorskip('app')


def test_build_auto_trade_candidate_plan_dedupes_limits_and_probe(monkeypatch):
    monkeypatch.setattr(app.config, 'AUTO_TRADE_CANDIDATE_LIMIT', 2)
    calls = []

    def _validate(c, auto=True):
        calls.append(c['symbol'])
        if c['symbol'] == 'AAA':
            return {'ok': False, 'skip_reasons': ['blocked_reason']}
        return {
            'ok': True, 'probe_trade': True, 'skip_reasons': [], 'entry_trigger': 'breakout',
            'first_trade_governor_applied': True, 'first_trade_final_qty': 1, 'first_trade_risk_dollars': 1.0
        }

    monkeypatch.setattr(app, 'validate_trade_candidate', _validate)
    plan = app.build_auto_trade_candidate_plan({
        'best_pick': {'symbol': 'AAA', 'setup_grade': 'A'},
        'watchlist': [{'symbol': 'AAA'}, {'symbol': 'BBB', 'setup_grade': 'WATCH'}],
    }, scan_id=9)

    assert plan['candidate_symbols'] == ['AAA', 'BBB']
    assert calls == ['AAA', 'BBB']
    assert plan['blocked_count'] == 1
    assert plan['executable_count'] == 1
    assert any(x['symbol'] == 'BBB' and x['probe_trade'] for x in plan['attempt_plan'])
    probe = next(x for x in plan['attempt_plan'] if x['symbol'] == 'BBB')
    assert probe['first_trade_governor_applied'] is True
    assert probe['first_trade_final_qty'] == 1
    assert probe['final_qty'] == 1


def test_auto_cycle_plan_endpoint_market_closed(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))
    monkeypatch.setattr(app, 'now_et', lambda: datetime(2026, 1, 1, 10, 0, 0))

    resp = app.app.test_client().post('/api/auto-cycle-plan', json={})
    data = resp.get_json()['data']
    assert resp.status_code == 200
    assert data['candidate_plan']['blocked'] is True
    assert 'market_closed' in data['candidate_plan']['blockers']


def test_auto_cycle_plan_no_execute(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open_not_required'))
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'AAA'}, 'watchlist': [{'symbol': 'BBB'}]})
    monkeypatch.setattr(app, 'insert_scan', lambda _r: 3)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': True, 'skip_reasons': []})
    called = {'n': 0}
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *a, **k: called.__setitem__('n', called['n'] + 1))

    resp = app.app.test_client().post('/api/auto-cycle-plan', json={})
    assert resp.status_code == 200
    assert called['n'] == 0
    assert app.RUNTIME_STATE['last_auto_cycle_plan']['candidate_count'] == 2


def test_autostart_failure_sets_runtime_state(monkeypatch):
    monkeypatch.setenv('AUTO_START_EXECUTION_ENGINE', '1')
    monkeypatch.delenv('DISABLE_AUTO_START_FOR_TESTS', raising=False)
    monkeypatch.setattr('config.AUTO_START_EXECUTION_ENGINE', True, raising=False)
    monkeypatch.setattr('execution.start_execution_engine', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('boom')))
    mod = importlib.reload(importlib.import_module('app'))
    assert mod.RUNTIME_STATE.get('engine_start_attempted') is True
    assert mod.RUNTIME_STATE.get('engine_start_error') == 'boom'


def test_autostart_success_passes_auto_scan_callback(monkeypatch):
    monkeypatch.setenv('AUTO_START_EXECUTION_ENGINE', '1')
    monkeypatch.delenv('DISABLE_AUTO_START_FOR_TESTS', raising=False)
    monkeypatch.setattr('config.AUTO_START_EXECUTION_ENGINE', True, raising=False)
    captured = {}

    def _start_execution_engine(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr('execution.start_execution_engine', _start_execution_engine)
    mod = importlib.reload(importlib.import_module('app'))
    assert mod.RUNTIME_STATE.get('engine_start_attempted') is True
    assert mod.RUNTIME_STATE.get('engine_start_error') is None
    assert captured.get('auto_scan_callback') is mod.run_scan_and_maybe_auto_trade


def test_autostart_disabled_by_env_skips_engine_start(monkeypatch):
    monkeypatch.setenv('AUTO_START_EXECUTION_ENGINE', '1')
    monkeypatch.setenv('DISABLE_AUTO_START_FOR_TESTS', '1')
    monkeypatch.setattr('config.AUTO_START_EXECUTION_ENGINE', True, raising=False)
    called = {'n': 0}

    def _start_execution_engine(**kwargs):
        called['n'] += 1

    monkeypatch.setattr('execution.start_execution_engine', _start_execution_engine)
    mod = importlib.reload(importlib.import_module('app'))
    assert called['n'] == 0
    assert mod.RUNTIME_STATE.get('engine_start_attempted') is not True
