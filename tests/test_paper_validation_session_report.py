from unittest.mock import patch
import app
import db


def _attempt(status='planned', **kw):
    base = {'created_at': '2026-05-13T13:30:00+00:00', 'source': 'scheduled_auto_cycle', 'status': status, 'candidate_count': 1, 'executable_count': 1, 'skip_reasons': [], 'top_blockers': {}}
    base.update(kw)
    return base


def _common(monkeypatch, attempts, protection=None):
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=200: attempts)
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'build_first_trade_observer_snapshot', lambda: {'next_action_hint': 'wait'})
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: protection or {'status': 'PASS', 'open_positions_count': 0, 'unprotected_position_detected': False})
    monkeypatch.setattr(app, 'build_market_session_heartbeat', lambda: {'heartbeat_status': 'READY'})


def _db_mode(monkeypatch, tmp_path):
    p = tmp_path / 'paper_report.sqlite'
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', db.get_recent_auto_cycle_attempts)
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'build_first_trade_observer_snapshot', lambda: {'next_action_hint': 'wait'})
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'PASS', 'open_positions_count': 0, 'unprotected_position_detected': False})
    monkeypatch.setattr(app, 'build_market_session_heartbeat', lambda: {'heartbeat_status': 'READY'})


def test_no_attempts_blocked(monkeypatch):
    _common(monkeypatch, [])
    r = app.build_paper_validation_session_report()
    assert r['report_status'] == 'BLOCKED_NO_VALIDATION' and r['acceptance_pass'] is False


def test_market_closed_skip_explained_from_db_json(monkeypatch, tmp_path):
    _db_mode(monkeypatch, tmp_path)
    db.insert_auto_cycle_attempt({'created_at': '2026-05-13T14:00:00+00:00', 'status': 'skipped', 'skip_reasons': ['market_closed'], 'source': 'scheduled_auto_cycle'})
    r = app.build_paper_validation_session_report()
    assert r['report_status'] == 'NO_TRADE_BUT_EXPLAINED'


def test_blocked_top_blockers_from_db_json(monkeypatch, tmp_path):
    _db_mode(monkeypatch, tmp_path)
    db.insert_auto_cycle_attempt({'created_at': '2026-05-13T14:00:00+00:00', 'status': 'blocked', 'top_blockers': {'no_executable_candidate': 1}, 'source': 'scheduled_auto_cycle'})
    r = app.build_paper_validation_session_report()
    assert r['report_status'] == 'NO_TRADE_BUT_EXPLAINED'


def test_failed_without_explicit_blocker_is_review(monkeypatch, tmp_path):
    _db_mode(monkeypatch, tmp_path)
    db.insert_auto_cycle_attempt({'created_at': '2026-05-13T14:00:00+00:00', 'status': 'failed', 'execution_error': 'boom', 'source': 'scheduled_auto_cycle'})
    r = app.build_paper_validation_session_report()
    assert r['report_status'] == 'REVIEW_REQUIRED'


def test_executed_accepted(monkeypatch):
    _common(monkeypatch, [_attempt('executed', attempted_symbol='AAPL', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=1.0)])
    r = app.build_paper_validation_session_report('2026-05-13')
    assert r['report_status'] == 'ACCEPTED_PAPER_VALIDATION' and r['acceptance_pass'] is True


def test_missing_qty_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=None, attempted_qty=None, first_trade_risk_dollars=1.0)])
    r = app.build_paper_validation_session_report('2026-05-13')
    assert r['report_status'] == 'REVIEW_REQUIRED' and not r['first_trade_review']['within_first_trade_limits']


def test_qty_zero_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=0, first_trade_risk_dollars=1.0)])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'REVIEW_REQUIRED'


def test_missing_risk_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=None)])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'REVIEW_REQUIRED'


def test_risk_zero_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=0)])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'REVIEW_REQUIRED'


def test_qty_and_risk_one_accepted(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=1)])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'ACCEPTED_PAPER_VALIDATION'


def test_first_executed_trade_is_used(monkeypatch):
    later = _attempt('executed', created_at='2026-05-13T15:00:00+00:00', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=1)
    earlier = _attempt('executed', created_at='2026-05-13T14:00:00+00:00', first_trade_governor_applied=True, first_trade_final_qty=9999, first_trade_risk_dollars=1)
    _common(monkeypatch, [later, earlier])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'REVIEW_REQUIRED'


def test_later_invalid_does_not_fail_first_trade_acceptance(monkeypatch):
    later_invalid = _attempt('executed', created_at='2026-05-13T15:00:00+00:00', first_trade_governor_applied=True, first_trade_final_qty=9999, first_trade_risk_dollars=1)
    earlier_valid = _attempt('executed', created_at='2026-05-13T14:00:00+00:00', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=1)
    _common(monkeypatch, [later_invalid, earlier_valid])
    r = app.build_paper_validation_session_report('2026-05-13')
    assert r['report_status'] == 'ACCEPTED_PAPER_VALIDATION'
    assert 'later_invalid_attempt' in r['warnings']


def test_unprotected_forces_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=1.0)], {'status': 'FAIL', 'open_positions_count': 1, 'unprotected_position_detected': True})
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'UNPROTECTED_POSITION_REVIEW'


def test_endpoint_and_runtime_state(client, monkeypatch):
    monkeypatch.setattr(app, 'build_paper_validation_session_report', lambda day=None: {'ok': True, 'generated_at': 'x', 'market_day': day or '2026-05-13', 'report_status': 'BLOCKED_NO_VALIDATION', 'acceptance_pass': False})
    resp = client.get('/api/paper-validation-session-report', query_string={'day': '2026-05-13'})
    assert resp.status_code == 200
    assert app.RUNTIME_STATE['last_paper_validation_session_report']['market_day'] == '2026-05-13'


def test_endpoint_no_trading_calls(client, monkeypatch):
    monkeypatch.setattr(app, 'build_paper_validation_session_report', lambda day=None: {'ok': True, 'generated_at': 'x', 'market_day': '2026-05-13', 'report_status': 'BLOCKED_NO_VALIDATION', 'acceptance_pass': False})
    with patch('app.run_scan', side_effect=AssertionError('no run_scan')), \
         patch('app.run_scan_and_maybe_auto_trade', side_effect=AssertionError('no run_scan_and_maybe_auto_trade')), \
         patch('app.execute_trade_candidate', side_effect=AssertionError('no execute_trade_candidate')), \
         patch('app.place_managed_entry_order', side_effect=AssertionError('no place_managed_entry_order'), create=True), \
         patch('app.submit_order', side_effect=AssertionError('no submit_order'), create=True), \
         patch('app.cancel_order', side_effect=AssertionError('no cancel_order'), create=True), \
         patch('app.close_position', side_effect=AssertionError('no close_position'), create=True), \
         patch('app.submit_market_sell', side_effect=AssertionError('no submit_market_sell'), create=True):
        assert client.get('/api/paper-validation-session-report').status_code == 200
