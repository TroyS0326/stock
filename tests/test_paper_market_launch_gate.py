import importlib


def _reload(monkeypatch, tmp_path):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'test.db'))
    import config, db, app
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(app)
    app.config.DB_PATH = str(tmp_path / 'test.db')
    db.config.DB_PATH = str(tmp_path / 'test.db')
    return app, db


def _base_state():
    return {
        'scheduler_running': True,
        'auto_scan_job_registered': True,
        'last_pre_market_readiness_pipeline': {'safe_to_enable_auto_cycle': True},
        'emergency_stop_active': False,
        'operator_auto_trade_paused': False,
        'last_market_open_rehearsal': {'ready_for_paper_session': True},
    }


def _setup_common(monkeypatch, app, state=None, attempts=None, market_open=False):
    st = _base_state()
    if state:
        st.update(state)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: st)
    monkeypatch.setattr(app, 'build_operator_safe_endpoint_health', lambda: {'ok': True, 'next_action_hint': 'ok'})
    monkeypatch.setattr(app, 'build_market_open_command_center', lambda: {'command_center_status': 'READY_FOR_PAPER_AUTO_CYCLE', 'primary_action': 'watch'})
    monkeypatch.setattr(app, 'build_deployment_checklist', lambda state=None: {'deployment_status': 'PASS', 'next_required_action': 'none'})
    monkeypatch.setattr(app, 'build_market_session_heartbeat', lambda: {'heartbeat_status': 'READY_WAITING_FOR_MARKET', 'next_action_hint': 'wait'})
    monkeypatch.setattr(app, 'build_first_trade_observer_snapshot', lambda: {'next_action_hint': 'wait'})
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'PASS', 'open_positions_count': 0, 'unprotected_position_detected': False})
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=10: attempts or [])
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (market_open, 'market_open' if market_open else 'market_closed'))
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', False)
    monkeypatch.setattr(app.config, 'LIVE_TRADING_OVERRIDE', False, raising=False)
    monkeypatch.setattr(app.config, 'FIRST_TRADE_GOVERNOR_ENABLED', True, raising=False)
    monkeypatch.setattr(app.config, 'FIRST_TRADE_MAX_QTY', 1, raising=False)
    monkeypatch.setattr(app.config, 'FIRST_TRADE_MAX_DOLLAR_RISK', 25.0, raising=False)


def test_launch_gate_wait_for_market_open(monkeypatch, tmp_path):
    app, _ = _reload(monkeypatch, tmp_path)
    _setup_common(monkeypatch, app, market_open=False)
    data = app.build_paper_market_launch_gate()
    assert data['launch_gate_status'] == 'WAIT_FOR_MARKET_OPEN'
    assert data['go_for_paper_validation'] is True
    assert data['may_leave_scheduler_armed'] is True
    assert data['may_run_manual_auto_cycle_now'] is False


def test_launch_gate_go_market_open(monkeypatch, tmp_path):
    app, _ = _reload(monkeypatch, tmp_path)
    _setup_common(monkeypatch, app, market_open=True)
    data = app.build_paper_market_launch_gate()
    assert data['launch_gate_status'] == 'GO_FOR_PAPER_MARKET_VALIDATION'
    assert data['go_for_paper_validation'] is True
    assert data['may_leave_scheduler_armed'] is True
    assert data['may_run_manual_auto_cycle_now'] is True


def test_launch_gate_blockers(monkeypatch, tmp_path):
    app, _ = _reload(monkeypatch, tmp_path)
    _setup_common(monkeypatch, app)

    monkeypatch.setattr(app.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', False)
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_READINESS'

    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app.config, 'LIVE_TRADING_OVERRIDE', True, raising=False)
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_SAFETY'

    monkeypatch.setattr(app.config, 'LIVE_TRADING_OVERRIDE', False, raising=False)
    _setup_common(monkeypatch, app, state={'scheduler_running': False})
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_SCHEDULER'

    _setup_common(monkeypatch, app, state={'emergency_stop_active': True})
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_SAFETY'

    _setup_common(monkeypatch, app, state={'operator_auto_trade_paused': True})
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_SAFETY'

    _setup_common(monkeypatch, app)
    monkeypatch.setattr(app, 'build_operator_safe_endpoint_health', lambda: {'ok': False, 'next_action_hint': 'fix'})
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_ENDPOINT_CONTRACT'

    _setup_common(monkeypatch, app, state={'last_pre_market_readiness_pipeline': None})
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_READINESS'

    _setup_common(monkeypatch, app, state={'last_pre_market_readiness_pipeline': {'safe_to_enable_auto_cycle': False}})
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_READINESS'

    _setup_common(monkeypatch, app)
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'FAIL', 'open_positions_count': 1, 'unprotected_position_detected': True})
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_UNPROTECTED_POSITION'

    _setup_common(monkeypatch, app, attempts=[{'status': 'failed'}])
    assert app.build_paper_market_launch_gate()['launch_gate_status'] == 'BLOCKED_REVIEW_REQUIRED'


def test_launch_gate_protected_open_position_adds_required_action(monkeypatch, tmp_path):
    app, _ = _reload(monkeypatch, tmp_path)
    _setup_common(monkeypatch, app, attempts=[{'status': 'executed'}], market_open=True)
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'PASS', 'open_positions_count': 1, 'unprotected_position_detected': False})
    data = app.build_paper_market_launch_gate()
    assert data['go_for_paper_validation'] is True
    assert 'monitor_open_trade' in data['required_actions']


def test_launch_gate_endpoint_runtime_and_no_order_calls(monkeypatch, tmp_path):
    app, _ = _reload(monkeypatch, tmp_path)
    _setup_common(monkeypatch, app)

    def _raise(*_a, **_k):
        raise AssertionError('must not be called')

    for name in ['run_scan', 'run_scan_and_maybe_auto_trade', 'execute_trade_candidate', 'place_managed_entry_order', 'submit_order', 'cancel_order', 'close_position', 'submit_market_sell']:
        monkeypatch.setattr(app, name, _raise, raising=False)

    r = app.app.test_client().get('/api/paper-market-launch-gate')
    assert r.status_code == 200
    payload = r.get_json()['data']
    assert payload['launch_gate_status'] == 'WAIT_FOR_MARKET_OPEN'
    assert 'last_paper_market_launch_gate' in app.RUNTIME_STATE
    assert app.RUNTIME_STATE['last_paper_market_launch_gate_error'] is None


def test_bot_status_includes_launch_gate_debug(monkeypatch, tmp_path):
    app, _ = _reload(monkeypatch, tmp_path)
    st = _base_state()
    st.update({'last_paper_market_launch_gate': {'launch_gate_status': 'WAIT_FOR_MARKET_OPEN'}, 'last_paper_market_launch_gate_at': 'x', 'last_paper_market_launch_gate_error': None})
    monkeypatch.setattr(app, 'get_runtime_state', lambda: st)
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {})
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(app, 'estimated_daily_loss_risk_used_today', lambda: 0)
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=10: [])
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))

    status = app.app.test_client().get('/api/bot-status').get_json()['data']
    assert 'last_paper_market_launch_gate' in status['attempt_debug']
    assert 'last_paper_market_launch_gate' in status['readiness_debug']
