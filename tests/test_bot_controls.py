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
    assert 'Bot Controls' in html
    assert 'botControlsPanel' in html
    assert "fetch('/api/control/state')" in html


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
