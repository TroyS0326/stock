import json
import db
import execution_service


def test_has_active_user_symbol_trade_blocks_active(monkeypatch):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, p=()):
            class R:
                def fetchall(self2):
                    return [{'raw_json': json.dumps({'execution_request': {'user_id': 7}})}]
            return R()
    monkeypatch.setattr(db, 'get_conn', lambda: C())
    assert db.has_active_user_symbol_trade(7, 'AAPL') is True


def test_has_active_user_symbol_trade_closed_rows_do_not_block(monkeypatch):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, p=()):
            class R:
                def fetchall(self2):
                    return []
            return R()
    monkeypatch.setattr(db, 'get_conn', lambda: C())
    assert db.has_active_user_symbol_trade(7, 'AAPL') is False


def test_has_active_user_symbol_trade_missing_user_explicit():
    assert db.has_active_user_symbol_trade(None, 'AAPL') is False


def test_execute_trade_candidate_blocks_duplicate_before_broker(monkeypatch):
    monkeypatch.setattr(execution_service, 'has_active_user_symbol_trade', lambda user_id, symbol: True)
    monkeypatch.setattr(execution_service, 'place_managed_entry_order', lambda *a, **k: (_ for _ in ()).throw(AssertionError('broker should not be called')))
    candidate = {
        'symbol': 'AAPL', 'qty': 1, 'entry_price': 10, 'stop_price': 9,
        'target_1': 11, 'target_2': 12, 'current_price': 10, 'buy_upper': 10.2,
        'score_total': 30, 'decision': 'BUY NOW', 'user_id': 7,
    }
    import pytest
    with pytest.raises(ValueError, match='duplicate_symbol_trade_blocked'):
        execution_service.execute_trade_candidate(candidate, source='auto')


def test_execute_trade_candidate_exposure_lookup_fail_closed(monkeypatch):
    monkeypatch.setattr(execution_service, 'has_active_user_symbol_trade', lambda user_id, symbol: False)
    monkeypatch.setattr(execution_service, 'has_active_symbol_exposure', lambda symbol: True)
    candidate = {
        'symbol': 'AAPL', 'qty': 1, 'entry_price': 10, 'stop_price': 9,
        'target_1': 11, 'target_2': 12, 'current_price': 10, 'buy_upper': 10.2,
        'score_total': 30, 'decision': 'BUY NOW', 'user_id': 7,
    }
    import pytest
    with pytest.raises(ValueError, match='duplicate_symbol_trade_blocked'):
        execution_service.execute_trade_candidate(candidate, source='auto')
