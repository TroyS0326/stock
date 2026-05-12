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


def test_synthetic_rehearsal_offline_skips_external_checks(monkeypatch):
    _safe_runtime(monkeypatch)

    def _boom(*_a, **_k):
        raise AssertionError('external function should not be called in synthetic rehearsal')

    monkeypatch.setattr(app, 'run_scan', _boom)
    monkeypatch.setattr(app, 'execute_trade_candidate', _boom)
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(app, 'get_open_positions', _boom)
    monkeypatch.setattr(app, 'get_open_orders', _boom)
    monkeypatch.setattr(app, 'get_account', _boom)
    monkeypatch.setattr(app, 'get_clock', _boom)

    out = app.run_synthetic_auto_cycle_rehearsal('TEST')
    assert out['would_attempt_trade'] is True
    assert out['offline_synthetic_external_checks_skipped'] is True
    assert 'duplicate_broker_exposure_lookup' in out['skipped_checks']


def test_synthetic_endpoint_offline_in_paper_mode(monkeypatch):
    _safe_runtime(monkeypatch)

    def _boom(*_a, **_k):
        raise AssertionError('external function should not be called from synthetic endpoint')

    monkeypatch.setattr(app, 'run_scan', _boom)
    monkeypatch.setattr(app, 'execute_trade_candidate', _boom)
    monkeypatch.setattr(app, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(app, 'get_open_positions', _boom)
    monkeypatch.setattr(app, 'get_open_orders', _boom)
    monkeypatch.setattr(app, 'get_account', _boom)
    monkeypatch.setattr(app, 'get_clock', _boom)

    resp = app.app.test_client().post('/api/synthetic-auto-cycle-rehearsal', json={'symbol': 'tst'})
    data = resp.get_json()['data']
    assert resp.status_code == 200
    assert data['first_candidate_symbol'] == 'TST'
    assert data['offline_synthetic_external_checks_skipped'] is True
    assert 'duplicate_broker_exposure_lookup' in data['skipped_checks']


def test_synthetic_rehearsal_respects_blockers(monkeypatch):
    _safe_runtime(monkeypatch)
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True, external_exposure_checks=True: {'ok': False, 'skip_reasons': ['emergency_stop_active', 'operator_auto_trade_paused']})
    out = app.run_synthetic_auto_cycle_rehearsal()
    assert out['would_attempt_trade'] is False
    assert 'emergency_stop_active' in out['blocking_reasons']
    assert 'operator_auto_trade_paused' in out['blocking_reasons']


def test_regular_plan_still_uses_external_exposure_checks(monkeypatch):
    calls = {'external': []}

    def _fake_validate(_candidate, auto=False, external_exposure_checks=True):
        calls['external'].append(external_exposure_checks)
        return {'ok': True, 'skip_reasons': [], 'probe_trade': False}

    monkeypatch.setattr(app, 'validate_trade_candidate', _fake_validate)
    app.build_auto_trade_candidate_plan(app.build_synthetic_rehearsal_scan(), scan_id=1)
    app.build_auto_trade_candidate_plan(app.build_synthetic_rehearsal_scan(), scan_id=1, external_exposure_checks=False)
    assert calls['external'] == [True, False]


def test_deployment_checklist_priorities(monkeypatch):
    client = app.app.test_client()
    base = {'operator_auto_trade_paused': False, 'emergency_stop_active': False}
    states = [
        ({}, 'run_paper_readiness_preflight'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': False}}, 'review_paper_readiness_preflight'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}}, 'run_auto_cycle_plan'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 0}}, 'review_scan_diagnostics'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}}, 'run_market_open_rehearsal'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': False}}, 'review_market_open_rehearsal'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}}, 'run_synthetic_rehearsal'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}, 'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal': {'would_attempt_trade': False}}, 'review_synthetic_rehearsal'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}, 'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal': {'would_attempt_trade': True}, 'scheduler_running': False, 'auto_scan_job_registered': False}, 'start_scheduler'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}, 'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal': {'would_attempt_trade': True}, 'scheduler_running': True, 'auto_scan_job_registered': True, 'emergency_stop_active': True}, 'clear_emergency_stop'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}, 'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal': {'would_attempt_trade': True}, 'scheduler_running': True, 'auto_scan_job_registered': True, 'operator_auto_trade_paused': True}, 'resume_auto_trading'),
        ({'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}, 'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal': {'would_attempt_trade': True}, 'scheduler_running': True, 'auto_scan_job_registered': True}, 'ready_for_market_open'),
    ]
    for state, action in states:
        monkeypatch.setattr(app, 'get_runtime_state', lambda s={**base, **state}: s)
        data = client.get('/api/deployment-checklist').get_json()['data']
        assert data['next_required_action'] == action
        assert 'paper_readiness_preflight_recent' in data


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
    assert 'last_pre_market_readiness_pipeline' in data['readiness_debug']


def test_build_deployment_checklist_matches_endpoint_priority(monkeypatch):
    state = {'last_paper_readiness_preflight_at': 'x', 'last_paper_readiness_preflight': {'ok': True}, 'last_auto_cycle_plan_at': 'x', 'last_auto_cycle_plan': {'executable_count': 1}, 'last_market_open_rehearsal_at': 'x', 'last_market_open_rehearsal': {'would_attempt_trade': True}, 'last_synthetic_rehearsal_at': 'x', 'last_synthetic_rehearsal': {'would_attempt_trade': True}, 'scheduler_running': False, 'auto_scan_job_registered': False, 'operator_auto_trade_paused': False, 'emergency_stop_active': False}
    monkeypatch.setattr(app, 'get_runtime_state', lambda: state)
    direct = app.build_deployment_checklist(state)
    via_endpoint = app.app.test_client().get('/api/deployment-checklist').get_json()['data']
    assert direct['next_required_action'] == via_endpoint['next_required_action'] == 'start_scheduler'


def test_pre_market_readiness_pipeline_endpoint_no_live_scan_default(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app, 'run_paper_trade_readiness_preflight', lambda symbol=None: {'ok': True, 'overall_status': 'PASS', 'checks': [], 'blocking_reasons': [], 'warning_reasons': [], 'next_action_hint': 'ready', 'symbol': symbol or 'TEST'})
    monkeypatch.setattr(app, 'run_synthetic_auto_cycle_rehearsal', lambda symbol=None: {'would_attempt_trade': True, 'first_trade_governor_applied': True, 'first_trade_final_qty': 1, 'blocking_reasons': [], 'next_action_hint': 'ready'})
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))
    monkeypatch.setattr(app, 'run_scan', lambda: (_ for _ in ()).throw(AssertionError('run_scan should not be called')))
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('no trade execution')))
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'operator_auto_trade_paused': False, 'emergency_stop_active': False})
    data = app.app.test_client().post('/api/pre-market-readiness-pipeline', json={}).get_json()['data']
    assert data['include_live_scan_plan'] is False
    assert data['auto_cycle_plan_status'] == 'not_run'
    assert data['market_open_rehearsal_status'] in {'blocked_market_closed', 'not_run'}


def test_pre_market_pipeline_live_scan_toggle(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app, 'run_paper_trade_readiness_preflight', lambda symbol=None: {'ok': True, 'overall_status': 'PASS', 'checks': [], 'blocking_reasons': [], 'warning_reasons': [], 'next_action_hint': 'ready', 'symbol': symbol or 'TEST'})
    monkeypatch.setattr(app, 'run_synthetic_auto_cycle_rehearsal', lambda symbol=None: {'would_attempt_trade': True, 'first_trade_governor_applied': True, 'first_trade_final_qty': 1, 'blocking_reasons': [], 'next_action_hint': 'ready'})
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    monkeypatch.setattr(app, 'api_market_open_rehearsal', lambda: app.ok({'would_attempt_trade': True, 'next_action_hint': 'ready_for_auto_cycle', 'blocking_reasons': []}))
    called = {'scan': 0}
    monkeypatch.setattr(app, 'api_auto_cycle_plan', lambda: (called.__setitem__('scan', called['scan'] + 1) or app.ok({'candidate_plan': {'executable_count': 1, 'blockers': []}})))
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'operator_auto_trade_paused': False, 'emergency_stop_active': False})
    client = app.app.test_client()
    client.post('/api/pre-market-readiness-pipeline', json={'include_live_scan_plan': False})
    assert called['scan'] == 0
    client.post('/api/pre-market-readiness-pipeline', json={'include_live_scan_plan': True})
    assert called['scan'] == 1
