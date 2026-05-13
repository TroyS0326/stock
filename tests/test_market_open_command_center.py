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
        'last_pre_market_readiness_pipeline': {'safe_to_enable_auto_cycle': True, 'overall_status': 'PASS', 'go_no_go': 'GO', 'next_required_action': 'ready_for_market_open'},
        'last_paper_readiness_preflight': {'ok': True, 'overall_status': 'PASS', 'next_action_hint': 'ready_for_paper'},
        'last_auto_cycle_plan': {'candidate_count': 1, 'executable_count': 1},
        'last_market_open_rehearsal': {'ok': True},
        'emergency_stop_active': False,
        'operator_auto_trade_paused': False,
    }


def _set_no_order_guards(monkeypatch, app):
    def _raise(*_a, **_k):
        raise AssertionError('should not call order/scan execution functions')
    for name in ['run_scan', 'run_scan_and_maybe_auto_trade', 'execute_trade_candidate', 'place_managed_entry_order', 'submit_order', 'cancel_order', 'close_position', 'submit_market_sell']:
        monkeypatch.setattr(app, name, _raise, raising=False)


def test_endpoint_returns_200_and_safe_json(monkeypatch, tmp_path):
    app, _db = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(app, 'get_runtime_state', _base_state)
    monkeypatch.setattr(app, 'build_deployment_checklist', lambda state=None: {'next_required_action': 'ready_for_market_open'})
    monkeypatch.setattr(app, 'build_first_trade_observer_snapshot', lambda: {'next_action_hint': 'wait_for_auto_attempt'})
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'PASS', 'open_positions_count': 0, 'next_action_hint': 'no_positions', 'unprotected_position_detected': False})
    monkeypatch.setattr(app, 'build_market_session_heartbeat', lambda: {'heartbeat_status': 'READY_MARKET_OPEN', 'next_action_hint': 'ready_for_next_auto_cycle'})
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=10: [])
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    _set_no_order_guards(monkeypatch, app)

    r = app.app.test_client().get('/api/market-open-command-center')
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['ok'] is True
    assert 'primary_action' in data and 'readiness_cards' in data


def test_primary_action_mappings(monkeypatch, tmp_path):
    app, _db = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(app, 'build_deployment_checklist', lambda state=None: {'next_required_action': 'ready_for_market_open'})
    monkeypatch.setattr(app, 'build_first_trade_observer_snapshot', lambda: {'next_action_hint': 'wait_for_auto_attempt'})
    monkeypatch.setattr(app, 'build_market_session_heartbeat', lambda: {'heartbeat_status': 'READY_MARKET_OPEN', 'next_action_hint': 'ready_for_next_auto_cycle'})
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    _set_no_order_guards(monkeypatch, app)

    cases = [
        ({'emergency_stop_active': True}, {'status': 'PASS', 'open_positions_count': 0}, [], 'clear_or_review_emergency_stop'),
        ({'operator_auto_trade_paused': True}, {'status': 'PASS', 'open_positions_count': 0}, [], 'resume_or_review_operator_pause'),
        ({}, {'status': 'FAIL', 'open_positions_count': 1}, [], 'review_unprotected_position'),
        ({'last_pre_market_readiness_pipeline': {}}, {'status': 'PASS', 'open_positions_count': 0}, [], 'run_pre_market_readiness_pipeline'),
        ({'last_pre_market_readiness_pipeline': {'safe_to_enable_auto_cycle': False}}, {'status': 'PASS', 'open_positions_count': 0}, [], 'review_pre_market_pipeline'),
        ({'scheduler_running': False}, {'status': 'PASS', 'open_positions_count': 0}, [], 'start_or_fix_scheduler'),
        ({}, {'status': 'PASS', 'open_positions_count': 0}, [], 'watch_for_next_scheduler_cycle'),
        ({}, {'status': 'PASS', 'open_positions_count': 0}, [{'status': 'failed'}], 'review_auto_cycle_failure'),
        ({}, {'status': 'PASS', 'open_positions_count': 1}, [{'status': 'executed'}], 'monitor_open_trade'),
        ({}, {'status': 'PASS', 'open_positions_count': 0}, [{'status': 'executed'}], 'ready_for_next_cycle'),
    ]

    for patch_state, protection, attempts, expected in cases:
        st = _base_state()
        st.update(patch_state)
        monkeypatch.setattr(app, 'get_runtime_state', lambda st=st: st)
        monkeypatch.setattr(app, 'build_position_protection_audit', lambda protection=protection: {**protection, 'next_action_hint': 'x', 'unprotected_position_detected': protection.get('status') == 'FAIL'})
        monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=10, attempts=attempts: attempts)
        payload = app.build_market_open_command_center()
        assert payload['primary_action'] == expected


def test_market_closed_and_bot_status_exposure(monkeypatch, tmp_path):
    app, _db = _reload(monkeypatch, tmp_path)
    st = _base_state()
    st.update({'last_market_open_command_center': {'primary_action': 'x'}, 'last_market_open_command_center_at': '2026-01-01T10:00:00', 'last_market_open_command_center_error': None})
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
    monkeypatch.setattr(app, 'build_deployment_checklist', lambda state=None: {'next_required_action': 'ready_for_market_open'})
    monkeypatch.setattr(app, 'build_first_trade_observer_snapshot', lambda: {'next_action_hint': 'wait_for_auto_attempt'})
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'PASS', 'open_positions_count': 0, 'next_action_hint': 'no_positions', 'unprotected_position_detected': False})
    monkeypatch.setattr(app, 'build_market_session_heartbeat', lambda: {'heartbeat_status': 'READY_WAITING_FOR_MARKET', 'next_action_hint': 'wait_for_market_open'})
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))

    assert app.build_market_open_command_center()['primary_action'] == 'wait_for_market_open'
    status = app.app.test_client().get('/api/bot-status').get_json()['data']
    assert 'last_market_open_command_center' in status['readiness_debug']
    assert 'last_market_open_command_center' in status['attempt_debug']
