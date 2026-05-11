import db
import execution_service


def test_has_active_user_symbol_trade_blocks_active(monkeypatch):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, p=()):
            class R:
                def fetchone(self2): return {'n': 1}
            return R()
    monkeypatch.setattr(db, 'get_conn', lambda: C())
    assert db.has_active_user_symbol_trade(7, 'AAPL') is True


def test_has_active_user_symbol_trade_closed_rows_do_not_block(monkeypatch):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, p=()):
            class R:
                def fetchone(self2): return {'n': 0}
            return R()
    monkeypatch.setattr(db, 'get_conn', lambda: C())
    assert db.has_active_user_symbol_trade(7, 'AAPL') is False


def test_has_active_user_symbol_trade_missing_user_explicit():
    assert db.has_active_user_symbol_trade(None, 'AAPL') is False


def test_execute_trade_candidate_blocks_duplicate_before_broker(monkeypatch):
    monkeypatch.setattr(execution_service, 'resolve_trade_user_id', lambda c: 7)
    monkeypatch.setattr(execution_service.db, 'has_active_user_symbol_trade', lambda user_id, symbol: True)
    called = {'broker': False}
    monkeypatch.setattr(execution_service, 'place_managed_entry_order', lambda *a, **k: called.__setitem__('broker', True))
    out = execution_service.execute_trade_candidate({'symbol': 'AAPL', 'qty': 1, 'entry_price': 1, 'stop_price': 0.9, 'target_1': 1.1, 'target_2': 1.2}, source='auto')
    assert out['ok'] is False
    assert 'duplicate_symbol_trade_blocked' in out['reason']
    assert called['broker'] is False


def test_execute_trade_candidate_exposure_lookup_fail_closed(monkeypatch):
    monkeypatch.setattr(execution_service, 'resolve_trade_user_id', lambda c: 7)
    monkeypatch.setattr(execution_service.db, 'has_active_user_symbol_trade', lambda user_id, symbol: False)
    monkeypatch.setattr(execution_service.db, 'get_open_exposure_for_user', lambda user_id: (_ for _ in ()).throw(RuntimeError('db down')))
    out = execution_service.execute_trade_candidate({'symbol': 'AAPL', 'qty': 1, 'entry_price': 1, 'stop_price': 0.9, 'target_1': 1.1, 'target_2': 1.2}, source='auto')
    assert out['ok'] is False
    assert out['reason'] == 'exposure_lookup_failed'
