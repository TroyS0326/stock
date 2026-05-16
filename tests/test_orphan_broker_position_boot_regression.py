import importlib


def test_import_app_succeeds():
    module = importlib.import_module('app')
    assert module is not None


def test_orphan_audit_route_and_endpoint_registered_once():
    import app

    if hasattr(app.app, 'url_map'):
        route_rules = [rule for rule in app.app.url_map.iter_rules() if rule.rule == '/api/orphan-broker-position-audit']
        assert len(route_rules) == 1
        endpoint_rules = [rule for rule in app.app.url_map.iter_rules() if rule.endpoint == 'api_orphan_broker_position_audit']
        assert len(endpoint_rules) == 1
    else:
        route_rules = [key for key in app.app._routes if key[0] == '/api/orphan-broker-position-audit']
        assert len(route_rules) == 1
        assert app.app._routes[('/api/orphan-broker-position-audit', 'GET')].__name__ == 'api_orphan_broker_position_audit'


def test_orphan_audit_endpoint_read_only_no_order_calls(monkeypatch):
    import app

    for fn in ['execute_trade_candidate', 'place_managed_entry_order', 'submit_order', 'cancel_order', 'close_position', 'submit_market_sell']:
        if hasattr(app, fn):
            monkeypatch.setattr(app, fn, lambda *a, **k: (_ for _ in ()).throw(AssertionError(f'{fn} called')))

    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda **_k: [])
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=50: [])

    c = app.app.test_client()
    response = c.get('/api/orphan-broker-position-audit')
    assert response.status_code == 200
