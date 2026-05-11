import sys
import types

sys.modules.setdefault('dotenv', types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault('requests', types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None, patch=lambda *a, **k: None, delete=lambda *a, **k: None))
sys.modules.setdefault('websockets', types.SimpleNamespace(connect=lambda *a, **k: []))

aps_mod = types.ModuleType('apscheduler')
schedulers_mod = types.ModuleType('apscheduler.schedulers')
bg_mod = types.ModuleType('apscheduler.schedulers.background')
class _DummyScheduler:
    def __init__(self, *a, **k): self.running=False
    def add_job(self, *a, **k): pass
    def start(self): self.running=True
    def get_jobs(self): return []
bg_mod.BackgroundScheduler = _DummyScheduler
sys.modules['apscheduler'] = aps_mod
sys.modules['apscheduler.schedulers'] = schedulers_mod
sys.modules['apscheduler.schedulers.background'] = bg_mod


flask_mod = types.ModuleType('flask')
class _DummyFlask:
    def __init__(self,*a,**k): self.config={}
    def route(self,*a,**k):
        def deco(fn): return fn
        return deco

def _jsonify(payload):
    return types.SimpleNamespace(json=payload)
flask_mod.Flask = _DummyFlask
flask_mod.jsonify = _jsonify
flask_mod.render_template = lambda *a, **k: ''
flask_mod.request = types.SimpleNamespace(headers={}, get_json=lambda silent=True: {})
sys.modules['flask'] = flask_mod
flask_sock_mod = types.ModuleType('flask_sock')
class _DummySock:
    def __init__(self,*a,**k): pass
    def route(self,*a,**k):
        def deco(fn): return fn
        return deco
flask_sock_mod.Sock = _DummySock
sys.modules['flask_sock'] = flask_sock_mod
import app
import db


def test_template_has_bot_controls_card_and_poller():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'Pause' in html
    assert "fetch('/api/control/state')" in html
    assert 'pauseToggleBtn' in html
    assert 'emergencyCancelAndCloseBtn' in html
    assert 'clearEmergencyStopBtn' in html
    assert "'/api/control/pause-auto-trading'" in html
    assert "'/api/control/resume-auto-trading'" in html
    assert "'/api/control/emergency-stop'" in html
    assert "'/api/control/clear-emergency-stop'" in html


def test_api_control_state_shape(monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {
        'operator_auto_trade_paused': True,
        'operator_pause_reason': 'maintenance',
        'emergency_stop_active': False,
        'emergency_stop_reason': None,
    })
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [{'action': 'pause_auto_trading'}])
    payload = app.api_control_state().json
    assert payload['ok'] is True
    data = payload['data']
    assert data['operator_auto_trade_paused'] is True
    assert isinstance(data['automation_blockers'], list)
    assert isinstance(data['recent_operator_actions'], list)


def test_insert_operator_action_serializes_details(monkeypatch):
    captured = {}
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, params):
            captured['params'] = params
            class R:
                lastrowid = 7
            return R()
    monkeypatch.setattr(db, 'get_conn', lambda: C())
    row_id = db.insert_operator_action('pause_auto_trading', reason='test', success=False, details={'a': 1})
    assert row_id == 7
    assert captured['params'][1] == 'pause_auto_trading'
    assert captured['params'][2] == 'test'
    assert captured['params'][3] == 0


def test_resume_allows_time_window_only_blockers(monkeypatch):
    monkeypatch.setattr(app, 'run_preflight', lambda: {'auto_trade_readiness': {'blocking_reasons': ['outside_morning_scan_window', 'buy_window_closed']}})
    monkeypatch.setattr(app.config, 'AUTO_TRADE_ENABLED', True)
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    monkeypatch.setattr(app, 'set_operator_pause', lambda paused, reason=None: None)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {})
    app.RUNTIME_STATE['emergency_stop_active'] = False
    payload = app.api_control_resume_auto_trading().json
    assert payload['ok'] is True


def test_resume_blocks_non_time_window_blockers(monkeypatch):
    monkeypatch.setattr(app, 'run_preflight', lambda: {'auto_trade_readiness': {'blocking_reasons': ['paper_trading_not_detected']}})
    monkeypatch.setattr(app.config, 'AUTO_TRADE_ENABLED', True)
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    app.RUNTIME_STATE['emergency_stop_active'] = False
    resp, status = app.api_control_resume_auto_trading()
    assert status == 409
    assert resp.json['ok'] is False


def test_bot_status_control_state_has_recent_operator_actions(monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {})
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [{'action': 'resume_auto_trading'}])
    payload = app.api_bot_status().json
    assert isinstance(payload['data']['control_state']['recent_operator_actions'], list)


def test_bot_status_includes_latest_best_pick(monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'last_scan_at': '2026-05-11T12:00:00Z'})
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app.config, 'SIMULATION_MODE', False)
    app.LATEST_SCAN = {'scan_id': 'scan-1', 'best_pick': {'symbol': 'AAPL', 'decision': 'BUY'}}
    payload = app.api_bot_status().json
    data = payload['data']
    assert data['latest_best_pick']['symbol'] == 'AAPL'
    assert data['latest_scan_id'] == 'scan-1'
    assert data['latest_scan_at'] == '2026-05-11T12:00:00Z'


def test_emergency_stop_blocks_without_paper_trading(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', False)
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    app.RUNTIME_STATE['last_operator_action_error'] = None
    resp, status = app.api_control_emergency_stop()
    assert status == 409
    assert resp.json['ok'] is False
    assert app.RUNTIME_STATE['last_operator_action_error'] == 'not_paper_trading'


def test_emergency_stop_close_positions_payload(monkeypatch):
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {})
    app.request = types.SimpleNamespace(headers={}, get_json=lambda silent=True: {'close_positions': True, 'reason': 'test'})
    called = {}
    def _fake_emergency_cancel_and_flatten(close_positions=False, reason=None):
        called['args'] = (close_positions, reason)
        return {'ok': True, 'errors': [], 'canceled_symbols': [], 'closed_positions': []}
    monkeypatch.setattr(app, 'emergency_cancel_and_flatten', _fake_emergency_cancel_and_flatten)
    resp, status = app.api_control_emergency_stop()
    assert status == 200
    assert resp.json['ok'] is True
    assert called['args'] == (True, 'test')


def test_clear_emergency_stop_runs_preflight_and_pauses(monkeypatch):
    monkeypatch.setattr(app, 'run_preflight', lambda: {'auto_trade_readiness': {'blocking_reasons': []}})
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    called = {'pause': False}
    app.RUNTIME_STATE['emergency_stop_active'] = True
    monkeypatch.setattr(app, 'set_emergency_stop', lambda active, reason=None: None)
    monkeypatch.setattr(app, 'set_operator_pause', lambda active, reason=None: called.__setitem__('pause', active))
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'operator_auto_trade_paused': True})
    payload = app.api_control_clear_emergency_stop().json
    assert payload['ok'] is True
    assert called['pause'] is True


def test_clear_emergency_stop_ignores_time_window_only_blockers(monkeypatch):
    monkeypatch.setattr(app, 'run_preflight', lambda: {'auto_trade_readiness': {'blocking_reasons': ['outside_morning_scan_window', 'buy_window_closed']}})
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    app.RUNTIME_STATE['emergency_stop_active'] = True
    app.RUNTIME_STATE['emergency_stop_active'] = True
    monkeypatch.setattr(app, 'set_emergency_stop', lambda active, reason=None: None)
    monkeypatch.setattr(app, 'set_operator_pause', lambda active, reason=None: None)
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {})
    payload = app.api_control_clear_emergency_stop().json
    assert payload['ok'] is True


def test_clear_emergency_stop_requires_active_emergency(monkeypatch):
    monkeypatch.setattr(app, 'run_preflight', lambda: {'auto_trade_readiness': {'blocking_reasons': []}})
    monkeypatch.setattr(app.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    app.RUNTIME_STATE['emergency_stop_active'] = False
    resp, status = app.api_control_clear_emergency_stop()
    assert status == 409
    assert 'emergency_stop_not_active' in resp.json['details']['blocking_reasons']
