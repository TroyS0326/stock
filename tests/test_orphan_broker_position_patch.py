import app
import execution_service


def test_orphan_audit_none(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda **_k: [])
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=50: [])
    payload = app.build_orphan_broker_position_audit()
    assert payload['orphan_position_detected'] is False
    assert payload['orphan_symbols'] == []


def test_orphan_audit_detects_docs(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'DOCS', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda **_k: [])
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=50: [])
    payload = app.build_orphan_broker_position_audit()
    assert payload['orphan_position_detected'] is True
    assert payload['orphan_symbols'] == ['DOCS']


def test_orphan_audit_matching_trade(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'DOCS', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda **_k: [])
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=50: [])
    monkeypatch.setattr(app, 'build_orphan_broker_position_audit', lambda: {'orphan_position_detected': False, 'orphan_symbols': [], 'positions': [{'symbol':'DOCS','has_matching_db_open_trade':True,'matching_db_trade_ids':[1]}], 'next_action_hint':'no_orphan_positions'})
    detected, syms, _ = app.has_orphan_broker_position()
    assert detected is False and syms == []


def test_validate_auto_blocked_by_orphan(monkeypatch):
    execution_service.set_orphan_position_checker(lambda: (True, ['DOCS'], {'orphan_symbols': ['DOCS']}))
    c = {'symbol':'ABC','setup_grade':'A','decision':'BUY NOW','qty':1,'entry_price':1.0,'stop_price':0.9,'current_price':1.0,'buy_upper':1.02,'target_1':1.1,'target_2':1.2,'details':{'spread_pct':0.001,'entry_trigger':'MOMENTUM_CONTINUATION','momentum_continuation':True}}
    v = execution_service.validate_trade_candidate(c, auto=True)
    assert v['ok'] is False
    assert 'orphan_broker_position' in v['skip_reasons']


def test_orphan_endpoint_no_order_calls(monkeypatch, client):
    for fn in ['execute_trade_candidate', 'place_managed_entry_order', 'submit_order', 'cancel_order', 'close_position', 'submit_market_sell']:
        if hasattr(app, fn):
            monkeypatch.setattr(app, fn, lambda *a, **k: (_ for _ in ()).throw(AssertionError(fn)))
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda **_k: [])
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=50: [])
    r = client.get('/api/orphan-broker-position-audit')
    assert r.status_code == 200
