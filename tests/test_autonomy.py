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

import config
import execution
import execution_service


def candidate(**kw):
    c = {'symbol':'ABC','setup_grade':'A','score_total':40,'scores':{'catalyst':5},'details':{'spread_pct':0.001,'opening_range_confirmation':{'breakout_confirmed':True},'vwap_hold_reclaim':{'reclaimed_vwap':False,'held_vwap':False}},'decision':'BUY NOW','current_price':1.0,'buy_upper':1.1,'qty':10,'entry_price':1.0,'stop_price':0.9,'target_1':1.1,'target_2':1.2}
    c.update(kw)
    return c

def test_engine_idempotent(monkeypatch):
    class DummyJob:
        def __init__(self, id): self.id = id
    class DummyScheduler:
        def __init__(self, *a, **k): self.jobs={}; self.running=False
        def add_job(self, fn, *a, id=None, replace_existing=False, **k): self.jobs[id]=fn
        def start(self): self.running=True
        def get_jobs(self): return [DummyJob(i) for i in self.jobs]
    monkeypatch.setattr(execution, 'BackgroundScheduler', DummyScheduler)
    monkeypatch.setattr(execution, 'run_async_loop_in_thread', lambda: None)
    execution._scheduler=None; execution._ws_thread=None
    state2 = execution.start_execution_engine(auto_scan_callback=lambda: None)
    execution.start_execution_engine(auto_scan_callback=lambda: None)
    assert set(state2['scheduled_jobs']) == {'flatten_book','position_monitor','auto_scan_loop'}

def test_trigger_or_logic_orb_and_vwap(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    assert execution_service.validate_trade_candidate(candidate(), auto=False)['ok']
    assert execution_service.validate_trade_candidate(candidate(details={'spread_pct':0.001,'opening_range_confirmation':{'breakout_confirmed':False},'vwap_hold_reclaim':{'reclaimed_vwap':True}}), auto=False)['ok']

def test_no_trigger_blocked(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    v = execution_service.validate_trade_candidate(candidate(details={'spread_pct':0.001}), auto=False)
    assert 'no_valid_entry_trigger' in v['skip_reasons']
