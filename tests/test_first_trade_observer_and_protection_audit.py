import app


def _set_state(**kwargs):
    app.RUNTIME_STATE.clear()
    app.RUNTIME_STATE.update(kwargs)


def test_position_protection_audit_no_positions(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    audit = app.build_position_protection_audit()
    assert audit['status'] == 'PASS'
    assert audit['next_action_hint'] == 'no_positions'


def test_position_protection_audit_protected_partial_unprotected(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'AAPL', 'qty': '10', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'symbol': 'AAPL', 'side': 'sell', 'type': 'stop', 'status': 'new'}, {'symbol': 'AAPL', 'side': 'sell', 'type': 'limit', 'status': 'open'}])
    assert app.build_position_protection_audit()['positions'][0]['protection_status'] == 'PROTECTED'
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'symbol': 'AAPL', 'side': 'sell', 'type': 'limit', 'status': 'open'}])
    assert app.build_position_protection_audit()['positions'][0]['protection_status'] == 'PARTIAL'
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'symbol': 'AAPL', 'side': 'buy', 'type': 'stop', 'status': 'open'}])
    audit = app.build_position_protection_audit()
    assert audit['status'] == 'FAIL'
    assert audit['positions'][0]['protection_status'] == 'UNPROTECTED'
    assert audit['summary_reason'] == 'unprotected_position_detected'


def test_position_protection_audit_ignores_inactive_and_counts_trailing(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'MSFT', 'qty': '5'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'symbol': 'MSFT', 'side': 'sell', 'type': 'limit', 'status': 'filled'}, {'symbol': 'MSFT', 'side': 'sell', 'type': 'trailing_stop', 'status': 'new'}])
    audit = app.build_position_protection_audit()
    assert audit['positions'][0]['has_trailing_or_runner_order'] is True
    assert audit['positions'][0]['has_target_order'] is False


def test_observer_next_action_hints(monkeypatch):
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])

    _set_state(last_auto_cycle_plan={}, last_auto_trade_attempts=[], last_auto_trade_error=None)
    assert app.build_first_trade_observer_snapshot()['next_action_hint'] == 'run_pre_market_readiness_pipeline'
    _set_state(last_pre_market_readiness_pipeline={'overall_status': 'PASS'}, last_auto_cycle_plan={}, last_auto_trade_attempts=[], last_auto_trade_error=None)
    assert app.build_first_trade_observer_snapshot()['next_action_hint'] == 'wait_for_auto_attempt'
    _set_state(last_pre_market_readiness_pipeline={'overall_status': 'PASS'}, last_auto_trade_error='broker_error', last_auto_trade_attempts=[{'symbol': 'AAPL'}], last_auto_cycle_plan={})
    assert app.build_first_trade_observer_snapshot()['next_action_hint'] == 'review_execution_error'
    _set_state(last_pre_market_readiness_pipeline={'overall_status': 'PASS'}, last_auto_trade_error=None, last_auto_cycle_plan={'candidate_count': 2, 'executable_count': 0}, last_auto_trade_attempts=[])
    assert app.build_first_trade_observer_snapshot()['next_action_hint'] == 'review_scan_diagnostics'


def test_observer_position_paths_and_runtime_and_bot_status(client, monkeypatch):
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {})
    _set_state(last_pre_market_readiness_pipeline={'overall_status': 'PASS'}, last_auto_trade_error=None, last_auto_trade_attempts=[{'symbol': 'AAPL'}], last_auto_cycle_plan={'candidate_count': 1, 'executable_count': 1})

    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'AAPL', 'qty': '1'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'symbol': 'AAPL', 'side': 'sell', 'type': 'stop', 'status': 'open'}, {'symbol': 'AAPL', 'side': 'sell', 'type': 'limit', 'status': 'open'}])
    assert app.build_first_trade_observer_snapshot()['next_action_hint'] == 'monitor_open_trade'

    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    assert app.build_first_trade_observer_snapshot()['next_action_hint'] == 'review_unprotected_position'

    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    assert app.build_first_trade_observer_snapshot()['next_action_hint'] == 'ready_for_next_auto_cycle'

    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no-order')))
    monkeypatch.setattr(app, 'place_managed_entry_order', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no-order')), raising=False)
    monkeypatch.setattr(app, 'submit_order', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no-order')), raising=False)
    monkeypatch.setattr(app, 'cancel_order', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no-order')), raising=False)
    monkeypatch.setattr(app, 'close_position', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no-order')), raising=False)
    monkeypatch.setattr(app, 'submit_market_sell', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no-order')), raising=False)

    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    resp = client.get('/api/first-trade-observer').get_json()['data']
    assert resp['safe_next_action'] == resp['next_action_hint']
    assert app.RUNTIME_STATE['last_first_trade_observer']
    client.get('/api/position-protection-audit')
    assert app.RUNTIME_STATE['last_position_protection_audit']
    status = client.get('/api/bot-status').get_json()['data']
    assert 'last_first_trade_observer' in status['attempt_debug']
    assert 'last_position_protection_audit' in status['readiness_debug']
