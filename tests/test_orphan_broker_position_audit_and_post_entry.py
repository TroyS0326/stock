import app


def test_orphan_audit_no_positions(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda *_a, **_k: [])
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=25: [])
    audit = app.build_orphan_broker_position_audit()
    assert audit['orphan_position_detected'] is False


def test_orphan_audit_detects_unmatched_doc(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'DOCS', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda *_a, **_k: [])
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=25: [])
    audit = app.build_orphan_broker_position_audit()
    assert audit['orphan_symbols'] == ['DOCS']


def test_reconciliation_orphan_status(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'DOCS'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'unsafe_protection_symbols': [], 'close_pending_symbols': [], 'unprotected_symbols': [], 'partial_symbols': []})
    monkeypatch.setattr(app, 'build_stale_db_trade_cleanup_plan', lambda: {'stale_count': 0})
    rec = app.build_paper_position_reconciliation()
    assert rec['reconciliation_status'] == 'FAIL_ORPHAN_BROKER_POSITION'


def test_post_entry_verification_missing_db_and_protection(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'DOCS'}])
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'positions': [{'symbol': 'DOCS', 'has_stop_order': False, 'has_trailing_or_runner_order': False, 'has_target_order': False}]})
    out = app.verify_post_entry_execution({'symbol': 'DOCS'}, {'order': {'id': 'o1', 'status': 'filled'}})
    assert out['status'] == 'FAIL'
    assert out['missing_db_trade_record'] is True
    assert out['missing_protective_orders'] is True
