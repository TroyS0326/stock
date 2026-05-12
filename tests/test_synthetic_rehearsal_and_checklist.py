import app


def _safe_runtime(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)


def test_build_synthetic_rehearsal_scan_fields():
    scan = app.build_synthetic_rehearsal_scan('xyz')
    c = scan['best_pick']
    assert c['symbol'] == 'XYZ'
    assert c['qty'] > 1
    assert c['details']['momentum_continuation'] is True
    assert c['hard_reject_reasons'] == []


def test_synthetic_candidate_validates_and_governs(monkeypatch):
    _safe_runtime(monkeypatch)
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    plan = app.build_auto_trade_candidate_plan(app.build_synthetic_rehearsal_scan(), scan_id=1)
    first = plan['attempt_plan'][0]
    assert first['ok'] is True
    assert first['first_trade_original_qty'] == 20
    assert first['first_trade_final_qty'] == 1


def test_run_synthetic_rehearsal_no_execute_and_no_scan(monkeypatch):
    _safe_runtime(monkeypatch)
    called = {'scan': 0, 'exec': 0}
    monkeypatch.setattr(app, 'run_scan', lambda: called.__setitem__('scan', called['scan'] + 1))
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *a, **k: called.__setitem__('exec', called['exec'] + 1))
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    out = app.run_synthetic_auto_cycle_rehearsal('TEST')
    assert called['scan'] == 0
    assert called['exec'] == 0
    assert out['would_attempt_trade'] is True
    assert out['final_qty'] == 1
    assert out['first_trade_original_qty'] == 20
    assert out['first_trade_final_qty'] == 1


def test_synthetic_rehearsal_respects_blockers(monkeypatch):
    _safe_runtime(monkeypatch)
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': False, 'skip_reasons': ['emergency_stop_active', 'operator_auto_trade_paused']})
    out = app.run_synthetic_auto_cycle_rehearsal()
    assert out['would_attempt_trade'] is False
    assert 'emergency_stop_active' in out['blocking_reasons']
    assert 'operator_auto_trade_paused' in out['blocking_reasons']


def test_synthetic_endpoint_and_runtime_state(monkeypatch):
    _safe_runtime(monkeypatch)
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    resp = app.app.test_client().post('/api/synthetic-auto-cycle-rehearsal', json={'symbol': 'tst'})
    data = resp.get_json()['data']
    assert resp.status_code == 200
    assert data['first_candidate_symbol'] == 'TST'
    assert app.RUNTIME_STATE.get('last_synthetic_rehearsal')


def test_deployment_checklist_priorities(monkeypatch):
    client = app.app.test_client()
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {})
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'run_paper_readiness_preflight'

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}})
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'run_auto_cycle_plan'

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}})
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'run_market_open_rehearsal'

    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}})
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'run_synthetic_rehearsal'


def test_deployment_checklist_final_and_blockers(monkeypatch):
    client = app.app.test_client()
    monkeypatch.setattr(app.config, 'FIRST_TRADE_GOVERNOR_ENABLED', True)
    base = {
        'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True},
        'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1},
        'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True},
        'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal': {'would_attempt_trade': True},
        'scheduler_running': True, 'auto_scan_job_registered': True,
        'operator_auto_trade_paused': False, 'emergency_stop_active': False,
    }
    monkeypatch.setattr(app, 'get_runtime_state', lambda: base)
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'ready_for_market_open'
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {**base, 'emergency_stop_active': True})
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'clear_emergency_stop'
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {**base, 'operator_auto_trade_paused': True})
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'resume_auto_trading'
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {**base, 'last_auto_cycle_plan': {'executable_count': 0}})
    assert client.get('/api/deployment-checklist').get_json()['data']['next_required_action'] == 'review_scan_diagnostics'


def test_bot_status_exposes_synthetic_fields(monkeypatch):
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {})
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(app, 'estimated_daily_loss_risk_used_today', lambda: 0)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {'candidate_count': 1, 'executable_count': 1}, 'last_synthetic_rehearsal': {'would_attempt_trade': True}, 'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal_error': None})
    data = app.app.test_client().get('/api/bot-status').get_json()['data']
    assert 'last_synthetic_rehearsal' in data['readiness_debug']
