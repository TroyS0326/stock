import app


def _base_runtime_state():
    return {
        'scheduler_running': True,
        'auto_scan_job_registered': True,
        'operator_auto_trade_paused': False,
        'emergency_stop_active': False,
        'last_auto_trade_attempts': [],
        'last_auto_trade_skip_reasons': [],
        'last_auto_trade_error': None,
        'last_auto_cycle_plan': {'executable_count': 0},
    }


def test_operator_runbook_returns_safe_payload_and_no_external_calls(monkeypatch):
    state = _base_runtime_state()
    monkeypatch.setattr(app, 'get_runtime_state', lambda: state)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))

    def _boom(*_a, **_k):
        raise AssertionError('forbidden external/order function called')

    for fn in [
        'run_scan', 'execute_trade_candidate', 'place_managed_entry_order', 'submit_order',
        'cancel_order', 'close_position', 'submit_market_sell', 'get_account',
        'get_latest_quote', 'get_clock', 'get_asset'
    ]:
        if hasattr(app, fn):
            monkeypatch.setattr(app, fn, _boom)

    resp = app.app.test_client().get('/api/operator-runbook')
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload['ok'] is True
    data = payload['data']
    assert 'environment_summary' in data
    assert 'current_readiness_snapshot' in data
    assert 'phases' in data
    assert 'next_best_command' in data
    assert 'warnings' in data
    assert 'forbidden_actions' in data
    env = data['environment_summary']
    disallowed = {'ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'api_key', 'secret', 'account_id', 'paper_base_url'}
    assert set(env.keys()).isdisjoint(disallowed)


def test_operator_runbook_phases_complete():
    data = app.app.test_client().get('/api/operator-runbook').get_json()['data']
    names = [p['name'] for p in data['phases']]
    assert names == [
        'pre_open_no_order',
        'market_open_no_order_validation',
        'enable_or_confirm_scheduler',
        'first_trade_watch',
        'emergency_only',
    ]
    for phase in data['phases']:
        assert phase['commands']
        assert phase['success_criteria']
        assert phase['stop_conditions']


def test_next_best_command_no_preflight(monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: _base_runtime_state())
    data = app.app.test_client().get('/api/operator-runbook').get_json()['data']
    cmd = data['next_best_command']
    assert cmd['method'] == 'POST'
    assert cmd['path'] == '/api/paper-readiness-preflight'


def test_next_best_command_failed_preflight(monkeypatch):
    s = _base_runtime_state()
    s['last_paper_readiness_preflight_at'] = 'x'
    s['last_paper_readiness_preflight'] = {'ok': False}
    monkeypatch.setattr(app, 'get_runtime_state', lambda: s)
    data = app.app.test_client().get('/api/operator-runbook').get_json()['data']
    cmd = data['next_best_command']
    assert cmd['path'] == '/api/paper-readiness-preflight'
    assert cmd['method'] == 'GET'


def test_next_best_command_synthetic_after_passing_preflight(monkeypatch):
    s = _base_runtime_state()
    s['last_paper_readiness_preflight_at'] = 'x'
    s['last_paper_readiness_preflight'] = {'ok': True}
    monkeypatch.setattr(app, 'get_runtime_state', lambda: s)
    data = app.app.test_client().get('/api/operator-runbook').get_json()['data']
    cmd = data['next_best_command']
    assert cmd['path'] == '/api/synthetic-auto-cycle-rehearsal'


def test_next_best_command_pipeline_after_synthetic(monkeypatch):
    s = _base_runtime_state()
    s['last_paper_readiness_preflight_at'] = 'x'
    s['last_paper_readiness_preflight'] = {'ok': True}
    s['last_synthetic_rehearsal_at'] = 'x'
    s['last_synthetic_rehearsal'] = {'would_attempt_trade': True}
    monkeypatch.setattr(app, 'get_runtime_state', lambda: s)
    data = app.app.test_client().get('/api/operator-runbook').get_json()['data']
    cmd = data['next_best_command']
    assert cmd['path'] == '/api/pre-market-readiness-pipeline'
    assert cmd['body'] == {'include_live_scan_plan': False}


def test_next_best_command_auto_cycle_plan_when_open_validation_desired(monkeypatch):
    s = _base_runtime_state()
    s['last_paper_readiness_preflight_at'] = 'x'
    s['last_paper_readiness_preflight'] = {'ok': True}
    s['last_synthetic_rehearsal_at'] = 'x'
    s['last_synthetic_rehearsal'] = {'would_attempt_trade': True}
    s['last_pre_market_readiness_pipeline_at'] = 'x'
    s['last_pre_market_readiness_pipeline'] = {'overall_status': 'PASS'}
    s['operator_market_open_validation_desired'] = True
    monkeypatch.setattr(app, 'get_runtime_state', lambda: s)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))
    data = app.app.test_client().get('/api/operator-runbook').get_json()['data']
    cmd = data['next_best_command']
    assert cmd['path'] == '/api/auto-cycle-plan'


def test_forbidden_actions_required_items_present():
    data = app.app.test_client().get('/api/operator-runbook').get_json()['data']
    forbidden = set(data['forbidden_actions'])
    assert 'set LIVE_TRADING_OVERRIDE=1' in forbidden
    assert 'use market buy entries' in forbidden
    assert 'remove stop protection' in forbidden
    assert 'increase FIRST_TRADE_MAX_QTY before first successful paper trade review' in forbidden
