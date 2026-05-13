import app


def _set_auth(monkeypatch, enabled=True, token='test-token', allow_localhost=True, header='X-Operator-Token'):
    monkeypatch.setattr(app.config, 'OPERATOR_AUTH_ENABLED', enabled)
    monkeypatch.setattr(app.config, 'OPERATOR_AUTH_TOKEN', token)
    monkeypatch.setattr(app.config, 'OPERATOR_AUTH_ALLOW_LOCALHOST', allow_localhost)
    monkeypatch.setattr(app.config, 'OPERATOR_AUTH_HEADER', header)


def _stub_bot_status_dependencies(monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True})
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    monkeypatch.setattr(app, 'within_morning_scan_window', lambda: True)
    monkeypatch.setattr(app, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(app, 'count_trades_today', lambda source='auto': 0)
    monkeypatch.setattr(app, 'estimated_daily_loss_risk_used_today', lambda: 0.0)


def test_auth_disabled_allows_api_and_operator(monkeypatch, client):
    _set_auth(monkeypatch, enabled=False)
    _stub_bot_status_dependencies(monkeypatch)
    assert client.get('/api/bot-status').status_code == 200
    assert client.get('/operator').status_code == 200


def test_auth_enabled_requires_token_for_api(monkeypatch, client):
    _set_auth(monkeypatch, enabled=True)
    assert client.get('/api/bot-status').status_code == 401


def test_auth_enabled_accepts_custom_header_token(monkeypatch, client):
    _set_auth(monkeypatch, enabled=True)
    _stub_bot_status_dependencies(monkeypatch)
    resp = client.get('/api/bot-status', headers={'X-Operator-Token': 'test-token'})
    assert resp.status_code == 200


def test_auth_enabled_accepts_bearer_token(monkeypatch, client):
    _set_auth(monkeypatch, enabled=True)
    _stub_bot_status_dependencies(monkeypatch)
    resp = client.get('/api/bot-status', headers={'Authorization': 'Bearer test-token'})
    assert resp.status_code == 200


def test_auth_enabled_wrong_token_rejected(monkeypatch, client):
    _set_auth(monkeypatch, enabled=True)
    assert client.get('/api/bot-status', headers={'X-Operator-Token': 'wrong'}).status_code == 401


def test_auth_enabled_operator_page_requires_token(monkeypatch, client):
    _set_auth(monkeypatch, enabled=True)
    assert client.get('/operator').status_code == 401
    assert client.get('/operator', headers={'X-Operator-Token': 'test-token'}).status_code == 200


def test_localhost_bypass_allowed_when_enabled(monkeypatch, client):
    _set_auth(monkeypatch, enabled=True, allow_localhost=True)
    monkeypatch.setattr(app, '_is_local_request', lambda: True)
    _stub_bot_status_dependencies(monkeypatch)
    assert client.get('/api/bot-status').status_code == 200


def test_localhost_bypass_rejected_when_disabled(monkeypatch, client):
    _set_auth(monkeypatch, enabled=True, allow_localhost=False)
    monkeypatch.setattr(app, '_is_local_request', lambda: True)
    assert client.get('/api/bot-status').status_code == 401


def test_safe_auth_status_exposed_without_token(monkeypatch, client):
    secret = 'super-secret-token'
    _set_auth(monkeypatch, enabled=True, token=secret, allow_localhost=False)
    _stub_bot_status_dependencies(monkeypatch)

    status_resp = client.get('/api/bot-status', headers={'X-Operator-Token': secret})
    assert status_resp.status_code == 200
    status_data = status_resp.get_json()['data']
    assert status_data['operator_auth_enabled'] is True
    assert status_data['operator_auth_header'] == 'X-Operator-Token'
    assert status_data['operator_auth_allow_localhost'] is False
    assert status_data['operator_auth_configured'] is True
    assert secret not in str(status_resp.get_json())

    health_resp = client.get('/api/operator-safe-endpoint-health', headers={'X-Operator-Token': secret})
    assert health_resp.status_code == 200
    health_data = health_resp.get_json()['data']
    assert health_data['operator_auth_enabled'] is True
    assert health_data['operator_auth_configured'] is True
    assert secret not in str(health_resp.get_json())
