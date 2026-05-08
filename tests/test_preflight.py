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

import preflight


def _stub_check(name, passed, message, details=None, warn=False):
    status = 'PASS' if passed and not warn else ('WARN' if warn else 'FAIL')
    return {'name': name, 'status': status, 'message': message, 'details': details}


def test_run_preflight_blocks_when_outside_windows(monkeypatch):
    monkeypatch.setattr(preflight, '_check', _stub_check)
    monkeypatch.setattr(preflight.config, 'AUTO_TRADE_ENABLED', True)
    monkeypatch.setattr(preflight.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(preflight.config, 'ALPACA_API_KEY', 'k')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_SECRET', 's')
    monkeypatch.setattr(preflight.config, 'SCAN_MIN_PRICE', 1)
    monkeypatch.setattr(preflight.config, 'SCAN_MAX_PRICE', 10)
    monkeypatch.setattr(preflight.config, 'MAX_FAILED_TRADES_PER_DAY', 3)
    monkeypatch.setattr(preflight.config, 'MAX_AUTO_TRADES_PER_DAY', 5)
    monkeypatch.setattr(preflight, 'within_morning_scan_window', lambda: False)
    monkeypatch.setattr(preflight, 'buy_window_open', lambda: False)
    monkeypatch.setattr(preflight, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(preflight, 'count_trades_today', lambda source='auto': 0)
    monkeypatch.setattr(preflight, 'get_runtime_state', lambda: {'scheduler_running': True, 'engine_started': True, 'trade_stream_thread_alive': True, 'scheduled_jobs': ['auto_scan_loop', 'flatten_book', 'position_monitor']})
    monkeypatch.setattr(preflight, 'get_account', lambda: {})
    monkeypatch.setattr(preflight, 'get_clock', lambda: {})
    monkeypatch.setattr(preflight, 'get_open_positions', lambda: [])
    monkeypatch.setattr(preflight, 'get_open_orders', lambda: [])
    monkeypatch.setattr(preflight, 'get_latest_quote', lambda _symbol: {'ap': 1})
    monkeypatch.setattr(preflight, 'init_db', lambda: None)
    monkeypatch.setattr(preflight, '_ensure_preflight_table', lambda: None)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, q):
            class _Result:
                def fetchall(self):
                    return [('scans',), ('trades',)]

                def fetchone(self):
                    return ('ok',)

            return _Result()

    monkeypatch.setattr(preflight, 'get_conn', lambda: _Conn())
    monkeypatch.setattr(preflight, '_record_preflight', lambda result: None)

    result = preflight.run_preflight()
    readiness = result['auto_trade_readiness']
    assert readiness['can_auto_trade_now'] is False
    assert 'outside_morning_scan_window' in readiness['blocking_reasons']
    assert 'buy_window_closed' in readiness['blocking_reasons']
    assert 'outside_morning_scan_window' in readiness['warning_reasons']
    assert 'buy_window_closed' in readiness['warning_reasons']
