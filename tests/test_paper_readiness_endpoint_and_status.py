import app


def test_preflight_endpoint_stores_runtime_state_and_bot_status(monkeypatch):
    expected = {
        'ok': False,
        'overall_status': 'FAIL',
        'checks': [],
        'blocking_reasons': ['account_accessible'],
        'warning_reasons': ['scheduler_ready'],
        'next_action_hint': 'fix_paper_account_access',
        'symbol': 'SPY',
    }
    monkeypatch.setattr(app, 'run_paper_trade_readiness_preflight', lambda symbol=None: expected)

    monkeypatch.setattr(app.request, 'method', 'POST', raising=False)
    monkeypatch.setattr(app.request, 'args', {}, raising=False)
    monkeypatch.setattr(app.request, 'get_json', lambda silent=True: {'symbol': 'SPY'}, raising=False)
    payload = app.api_paper_readiness_preflight().json['data']
    assert payload['next_action_hint'] == 'fix_paper_account_access'

    runtime = app.RUNTIME_STATE
    assert runtime['last_paper_readiness_preflight']['overall_status'] == 'FAIL'
    assert runtime['last_paper_readiness_preflight']['symbol'] == 'SPY'
    assert runtime['last_paper_readiness_preflight_at']

    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {})
    with app.app.app_context():
        status = app.api_bot_status().json['data']
    assert status['readiness_debug']['last_paper_readiness_preflight']['next_action_hint'] == 'fix_paper_account_access'
    assert status['attempt_debug']['last_paper_readiness_preflight']['overall_status'] == 'FAIL'


def test_preflight_endpoint_does_not_place_or_cancel_orders(monkeypatch):
    calls = []

    def _raise(name):
        def inner(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f'{name} should not be called')
        return inner

    monkeypatch.setattr(app, 'run_paper_trade_readiness_preflight', lambda symbol=None: {
        'ok': True,
        'overall_status': 'PASS',
        'checks': [],
        'blocking_reasons': [],
        'warning_reasons': [],
        'next_action_hint': 'ready_for_open',
        'symbol': 'SPY',
    })

    for fn in ['execute_trade_candidate', 'place_managed_entry_order', 'submit_order', 'cancel_order', 'close_position', 'submit_market_sell']:
        monkeypatch.setattr(app, fn, _raise(fn), raising=False)

    monkeypatch.setattr(app.request, 'method', 'POST', raising=False)
    monkeypatch.setattr(app.request, 'args', {}, raising=False)
    monkeypatch.setattr(app.request, 'get_json', lambda silent=True: {'symbol': 'SPY'}, raising=False)
    resp = app.api_paper_readiness_preflight().json['data']
    assert resp['ok'] is True
    assert calls == []
