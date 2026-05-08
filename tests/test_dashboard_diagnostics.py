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
    def test_client(self):
        class C:
            def get(self, path):
                class R:
                    status_code=200
                    def get_json(self2):
                        return {'ok': True, 'data': app.api_bot_status().json['data']}
                return R()
        return C()

def _jsonify(payload):
    return types.SimpleNamespace(json=payload)
flask_mod.Flask = _DummyFlask
flask_mod.jsonify = _jsonify
flask_mod.render_template = lambda *a, **k: ''
flask_mod.request = types.SimpleNamespace(headers={})
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


def test_bot_status_runtime_fields(monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {
        'engine_started': True,
        'scheduler_running': True,
        'trade_stream_thread_alive': True,
        'last_scan_at': '2026-01-01T12:00:00',
        'last_auto_trade_skip_reasons': [],
    })
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [{'id': 1}])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [{'id': 2}])
    c = app.app.test_client()
    r = c.get('/api/bot-status')
    payload = r.get_json()
    assert r.status_code == 200
    assert payload['ok'] is True
    data = payload['data']
    assert 'engine_started' in data
    assert 'scheduled_jobs' in data or 'scheduler_running' in data
    assert 'recent_scans' in data
    assert 'recent_trades' in data
    for key in ['AUTO_TRADE_ENABLED', 'AUTO_SCAN_INTERVAL_SECONDS', 'POSITION_MONITOR_INTERVAL_SECONDS', 'MORNING_SCAN_START_ET', 'MORNING_SCAN_END_ET', 'NO_BUY_BEFORE_ET', 'MAX_AUTO_TRADES_PER_DAY', 'MAX_FAILED_TRADES_PER_DAY', 'SCAN_MIN_PRICE', 'SCAN_MAX_PRICE', 'QUICK_PROFIT_TAKE_PCT', 'BREAKEVEN_TRIGGER_PCT']:
        assert key in data['config_summary']


def test_template_has_runtime_markers():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'Automation Status' in html
    assert 'botRuntimePanel' in html
    assert 'Why it bought / why it did not buy' in html
    assert 'rejectedBody' in html


def test_template_js_handles_missing_diagnostics_markers():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'asList = (v) => Array.isArray(v)' in html
    assert "show = (v) => (v === undefined || v === null || v === '')" in html
    assert 'payload.data.best_pick||{}' in html


def test_api_preflight_returns_inner_ok(monkeypatch):
    monkeypatch.setattr(app, 'run_preflight', lambda: {
        'ok': False,
        'overall_status': 'BLOCKED',
        'checks': [{'name': 'x', 'status': 'fail'}],
        'auto_trade_readiness': {'ready': False},
    })
    payload = app.api_preflight().json
    assert payload['ok'] is True
    assert payload['data']['ok'] is False
    assert payload['data']['overall_status'] == 'BLOCKED'
    assert isinstance(payload['data']['checks'], list)
    assert isinstance(payload['data']['auto_trade_readiness'], dict)



def test_api_preflight_handles_unexpected_exception(monkeypatch):
    def _boom():
        raise RuntimeError('boom')

    monkeypatch.setattr(app, 'run_preflight', _boom)
    payload = app.api_preflight().json
    assert payload['ok'] is True
    data = payload['data']
    assert data['ok'] is False
    assert data['overall_status'] == 'BLOCKED'
    assert data['checks'][0]['name'] == 'preflight_exception'
    assert data['checks'][0]['status'] == 'FAIL'
    assert data['checks'][0]['message'].startswith('Preflight crashed: boom')
    assert data['auto_trade_readiness']['can_auto_trade_now'] is False
    assert data['auto_trade_readiness']['blocking_reasons'] == ['preflight_exception']
    assert data['auto_trade_readiness']['warning_reasons'] == []

def test_template_has_preflight_markers():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'Bot Preflight' in html
    assert 'preflightBtn' in html
    assert 'preflightBody' in html
    assert "fetch('/api/preflight')" in html
    assert 'function runPreflight()' in html
