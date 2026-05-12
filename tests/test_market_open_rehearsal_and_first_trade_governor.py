import pytest

import execution_service
import app


def _cand(**kw):
    c = {
        'symbol': 'AAA', 'setup_grade': 'A', 'score_total': 35, 'decision': 'BUY NOW', 'qty': 5,
        'current_price': 10.0, 'entry_price': 10.0, 'buy_upper': 10.1, 'stop_price': 9.0,
        'target_1': 10.5, 'target_2': 11.0, 'details': {'spread_pct': 0.001, 'momentum_continuation': True},
    }
    c.update(kw)
    return c


def _patch_safe(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'has_active_symbol_exposure', lambda symbol: False)
    monkeypatch.setattr(execution_service, 'has_active_user_symbol_trade', lambda user_id, symbol: False)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(execution_service, 'estimated_daily_loss_risk_used_today', lambda: 0.0)


def test_first_trade_governor_downsizes(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'trade_risk_limit', lambda: 10.0)
    v = execution_service.validate_trade_candidate(_cand(), auto=True)
    assert v['ok'] is True
    assert 'oversized_risk' not in v['skip_reasons']
    assert v['first_trade_governor_applied'] is True
    assert v['first_trade_original_qty'] == 5
    assert v['first_trade_final_qty'] == 1


def test_first_trade_governor_blocks_when_risk_too_high(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'trade_risk_limit', lambda: 10.0)
    monkeypatch.setattr(execution_service.config, 'FIRST_TRADE_MAX_DOLLAR_RISK', 5.0)
    v = execution_service.validate_trade_candidate(_cand(qty=20, stop_price=4.0), auto=True)
    assert v['ok'] is False
    assert 'first_trade_risk_too_high' in v['skip_reasons']
    assert v['first_trade_blocked_reason'] == 'first_trade_risk_too_high'


def test_first_trade_governor_does_not_override_invalid_risk(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'trade_risk_limit', lambda: 10.0)
    v = execution_service.validate_trade_candidate(_cand(qty=20, stop_price=10.0), auto=True)
    assert v['ok'] is False
    assert 'invalid_risk' in v['skip_reasons']
    assert v['first_trade_governor_applied'] is False


def test_first_trade_governor_does_not_override_duplicate_exposure(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'trade_risk_limit', lambda: 10.0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: {'id': 1})
    v = execution_service.validate_trade_candidate(_cand(qty=20), auto=True)
    assert v['ok'] is False
    assert 'duplicate_symbol_trade_blocked' in v['skip_reasons']


def test_first_trade_governor_does_not_override_non_paper(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'trade_risk_limit', lambda: 10.0)
    monkeypatch.setattr(execution_service.config, 'PAPER_TRADING_DETECTED', False)
    monkeypatch.setattr(execution_service.config, 'SIMULATION_MODE', False)
    v = execution_service.validate_trade_candidate(_cand(qty=20), auto=True)
    assert v['ok'] is False
    assert 'not_paper_or_simulation' in v['skip_reasons']


def test_probe_override_includes_governor_fields(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    v = execution_service.validate_trade_candidate(_cand(setup_grade='C', decision='WAIT', qty=5), auto=True)
    assert v['ok'] is True
    assert v['probe_trade'] is True
    assert v['first_trade_governor_applied'] is True
    assert v['first_trade_final_qty'] == 1


def test_probe_override_blocked_by_first_trade_dollar_risk(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service.config, 'FIRST_TRADE_MAX_DOLLAR_RISK', 0.5)
    v = execution_service.validate_trade_candidate(_cand(setup_grade='C', decision='WAIT', qty=5), auto=True)
    assert v['ok'] is False
    assert 'first_trade_risk_too_high' in v['skip_reasons']


def test_manual_trade_not_governed(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'trade_risk_limit', lambda: 10.0)
    v = execution_service.validate_trade_candidate(_cand(qty=20), auto=False)
    assert v['first_trade_governor_applied'] is False


def test_governor_not_applied_after_first_auto_trade(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 2)
    v = execution_service.validate_trade_candidate(_cand(), auto=True)
    assert v['ok'] is True
    assert v['first_trade_governor_applied'] is False


def test_execution_rechecks_and_raises(monkeypatch):
    _patch_safe(monkeypatch)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'place_managed_entry_order', lambda **kwargs: {'id': 'o1', 'status': 'accepted'})
    monkeypatch.setattr(execution_service, 'insert_trade', lambda payload: 1)
    c = _cand(qty=5)
    c['first_trade_governor_applied'] = True
    c['first_trade_final_qty'] = 3
    with pytest.raises(ValueError):
        execution_service.execute_trade_candidate(c, source='auto')


def test_rehearsal_no_execute(monkeypatch):
    called = {'n': 0}
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_open_not_required'))
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'AAA'}, 'watchlist': []})
    monkeypatch.setattr(app, 'insert_scan', lambda _r: 2)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True})
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': True, 'skip_reasons': [], 'first_trade_governor_applied': True, 'first_trade_final_qty': 1, 'first_trade_risk_dollars': 1.0})
    resp = app.app.test_client().post('/api/market-open-rehearsal', json={})
    data = resp.get_json()['data']
    assert resp.status_code == 200
    assert called['n'] == 0
    assert data['would_attempt_trade'] is True
    assert data['first_trade_governor']['first_trade_governor_applied'] is True


def test_rehearsal_uses_runtime_scheduler_status_and_blockers(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open_not_required'))
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'AAA'}, 'watchlist': []})
    monkeypatch.setattr(app, 'insert_scan', lambda _r: 2)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': True, 'skip_reasons': [], 'first_trade_governor_applied': True, 'first_trade_final_qty': 1, 'first_trade_risk_dollars': 1.0})

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'position_monitor_job_registered': True, 'flatten_job_registered': True, 'scheduled_jobs': ['auto_scan_loop']})
    data = app.app.test_client().post('/api/market-open-rehearsal', json={}).get_json()['data']
    assert data['scheduler_status']['scheduler_running'] is True
    assert data['scheduler_status']['auto_scan_job_registered'] is True

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': False})
    data = app.app.test_client().post('/api/market-open-rehearsal', json={}).get_json()['data']
    assert 'auto_scan_job_not_registered' in data['blocking_reasons']

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': False, 'auto_scan_job_registered': True})
    data = app.app.test_client().post('/api/market-open-rehearsal', json={}).get_json()['data']
    assert 'scheduler_not_running' in data['blocking_reasons']


def test_rehearsal_closed_real_paper_blocked(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(app.config, 'AUTO_CYCLE_REQUIRE_MARKET_OPEN', True)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))
    resp = app.app.test_client().post('/api/market-open-rehearsal', json={})
    assert resp.status_code == 200
    assert 'market_closed' in resp.get_json()['data']['blocking_reasons']


def test_bot_status_plan_hints(monkeypatch):
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {})
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(app, 'estimated_daily_loss_risk_used_today', lambda: 0)
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {}})
    data = app.app.test_client().get('/api/bot-status').get_json()['data']
    assert 'no_candidate_plan_available' in data['auto_cycle_blockers']
    assert data['next_action_hint'] == 'run_auto_cycle_plan'

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {'candidate_count': 1, 'executable_count': 1}})
    data = app.app.test_client().get('/api/bot-status').get_json()['data']
    assert data['next_action_hint'] == 'ready_for_auto_cycle'

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {'candidate_count': 2, 'executable_count': 0}})
    data = app.app.test_client().get('/api/bot-status').get_json()['data']
    assert data['next_action_hint'] == 'review_scan_diagnostics'
