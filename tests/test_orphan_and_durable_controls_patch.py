import execution
import execution_service
import app


def _cand():
    return {'symbol':'DOCS','decision':'BUY NOW','qty':1,'entry_price':10,'stop_price':9,'current_price':10,'buy_upper':10.5,'target_1':11,'target_2':12,'setup_grade':'A','score_total':90,'scores':{'catalyst':3},'details':{'spread_pct':0.01}}


def test_durable_pause_persists(monkeypatch):
    execution.set_operator_pause(True, reason='x')
    execution.RUNTIME_STATE.clear()
    blocks = execution.get_runtime_trade_blocks()
    assert 'operator_auto_trade_paused' in blocks
    execution.set_operator_pause(False)


def test_durable_emergency_persists(monkeypatch):
    execution.set_emergency_stop(True, reason='x')
    execution.RUNTIME_STATE.clear()
    blocks = execution.get_runtime_trade_blocks()
    assert 'emergency_stop_active' in blocks
    execution.set_emergency_stop(False)


def test_orphan_gate_blocks_auto(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_runtime_trade_blocks', lambda: [])
    execution_service.set_orphan_position_checker(lambda: (True, ['DOCS'], {}))
    execution_service.set_unprotected_position_checker(lambda: (False, [], {}))
    v = execution_service.validate_trade_candidate(_cand(), auto=True)
    assert not v['ok'] and 'orphan_broker_position' in v['skip_reasons']
    assert v['orphan_symbols'] == ['DOCS']
    execution_service.set_orphan_position_checker(None)


def test_orphan_endpoint_no_mutation(monkeypatch):
    called = {'n':0}
    for fn in ['execute_trade_candidate', 'place_managed_entry_order', 'submit_order', 'cancel_order', 'close_position', 'submit_market_sell']:
        if hasattr(app, fn):
            monkeypatch.setattr(app, fn, lambda *a, **k: called.__setitem__('n', called['n']+1))
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda *a, **k: [])
    c = app.app.test_client()
    r = c.get('/api/orphan-broker-position-audit')
    assert r.status_code == 200
    assert called['n'] == 0
