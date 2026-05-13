from unittest.mock import patch
import app

def _attempt(status='planned', **kw):
    base = {'created_at': '2026-05-13T13:30:00+00:00', 'source': 'scheduled_auto_cycle', 'status': status, 'candidate_count': 1, 'executable_count': 1, 'skip_reasons': [], 'top_blockers': {}}
    base.update(kw)
    return base

def _common(monkeypatch, attempts, protection=None):
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=200: attempts)
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'build_first_trade_observer_snapshot', lambda: {'next_action_hint': 'wait'})
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: protection or {'status':'PASS','open_positions_count':0,'unprotected_position_detected':False})
    monkeypatch.setattr(app, 'build_market_session_heartbeat', lambda: {'heartbeat_status':'READY'})

def test_no_attempts_blocked(monkeypatch):
    _common(monkeypatch, [])
    r = app.build_paper_validation_session_report('2026-05-13')
    assert r['report_status'] == 'BLOCKED_NO_VALIDATION' and r['acceptance_pass'] is False

def test_market_closed_skip_explained(monkeypatch):
    _common(monkeypatch, [_attempt('skipped', skip_reasons=['market_closed'])])
    r = app.build_paper_validation_session_report('2026-05-13')
    assert r['report_status'] == 'NO_TRADE_BUT_EXPLAINED'

def test_planned_partial(monkeypatch):
    _common(monkeypatch, [_attempt('planned')])
    r = app.build_paper_validation_session_report('2026-05-13')
    assert r['report_status'] == 'PARTIAL_PAPER_VALIDATION'

def test_executed_accepted(monkeypatch):
    _common(monkeypatch, [_attempt('executed', attempted_symbol='AAPL', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=1.0)])
    r = app.build_paper_validation_session_report('2026-05-13')
    assert r['report_status'] == 'ACCEPTED_PAPER_VALIDATION' and r['acceptance_pass'] is True

def test_qty_over_cap_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=9999, first_trade_risk_dollars=1.0)])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'REVIEW_REQUIRED'

def test_risk_over_cap_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=99999.0)])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'REVIEW_REQUIRED'

def test_unprotected_forces_review(monkeypatch):
    _common(monkeypatch, [_attempt('executed', first_trade_governor_applied=True, first_trade_final_qty=1, first_trade_risk_dollars=1.0)], {'status':'FAIL','open_positions_count':1,'unprotected_position_detected':True})
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'UNPROTECTED_POSITION_REVIEW'

def test_failed_execution_review(monkeypatch):
    _common(monkeypatch, [_attempt('failed', execution_error='boom')])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'REVIEW_REQUIRED'

def test_no_executable_candidate_explained(monkeypatch):
    _common(monkeypatch, [_attempt('blocked', skip_reasons=['no_executable_candidate'])])
    assert app.build_paper_validation_session_report('2026-05-13')['report_status'] == 'NO_TRADE_BUT_EXPLAINED'

def test_endpoint_and_runtime_state(client, monkeypatch):
    monkeypatch.setattr(app, 'build_paper_validation_session_report', lambda day=None: {'ok':True,'generated_at':'x','market_day':day or '2026-05-13','report_status':'BLOCKED_NO_VALIDATION','acceptance_pass':False})
    resp = client.get('/api/paper-validation-session-report', query_string={'day':'2026-05-13'})
    assert resp.status_code == 200
    assert app.RUNTIME_STATE['last_paper_validation_session_report']['market_day'] == '2026-05-13'

def test_endpoint_no_trading_calls(client, monkeypatch):
    monkeypatch.setattr(app, 'build_paper_validation_session_report', lambda day=None: {'ok':True,'generated_at':'x','market_day':'2026-05-13','report_status':'BLOCKED_NO_VALIDATION','acceptance_pass':False})
    with patch('app.execute_trade_candidate', side_effect=AssertionError('no')):
        assert client.get('/api/paper-validation-session-report').status_code == 200
