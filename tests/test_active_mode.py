import types, sys
sys.modules.setdefault('dotenv', types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault('requests', types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None, patch=lambda *a, **k: None, delete=lambda *a, **k: None))
sys.modules.setdefault('websockets', types.SimpleNamespace(connect=lambda *a, **k: []))

aps_mod = types.ModuleType('apscheduler')
schedulers_mod = types.ModuleType('apscheduler.schedulers')
bg_mod = types.ModuleType('apscheduler.schedulers.background')
class _DummyScheduler:
    def __init__(self,*a,**k): self.running=False
    def add_job(self,*a,**k): pass
    def start(self): self.running=True
    def get_jobs(self): return []
bg_mod.BackgroundScheduler = _DummyScheduler
sys.modules['apscheduler']=aps_mod
sys.modules['apscheduler.schedulers']=schedulers_mod
sys.modules['apscheduler.schedulers.background']=bg_mod
import filters, execution_service
from models import SymbolMarketStats


def test_gatekeeper_uses_config_price_bounds(monkeypatch):
    monkeypatch.setattr(filters.config, 'SCAN_MAX_PRICE', 20.0)
    stats = SymbolMarketStats(symbol='ABC', price=10.0, daily_dollar_volume=2_000_000, spread_pct=0.001)
    assert filters.hard_reject_reason(stats) == ''


def _cand(**kw):
    c={'symbol':'ABC','setup_grade':'WATCH','score_total':30,'decision':'WATCH FOR BREAKOUT','current_price':1.01,'buy_upper':1.02,'qty':10,'entry_price':1.01,'stop_price':0.99,'details':{'spread_pct':0.001,'momentum_continuation':True}}
    c.update(kw)
    return c


def test_watch_grade_active_mode_allowed(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    v=execution_service.validate_trade_candidate(_cand(), auto=True)
    assert v['ok'] and v['fallback_used']


def test_auto_blocks_spread_qty_risk_duplicate_and_max(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: {'id':1})
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 999)
    v=execution_service.validate_trade_candidate(_cand(qty=10, details={'spread_pct':0.5}, entry_price=2, stop_price=0.5), auto=True)
    for r in ['wide_spread','oversized_risk','duplicate_symbol_trade_blocked','max_auto_trades_reached']:
        assert r in v['skip_reasons']
    v2 = execution_service.validate_trade_candidate(_cand(qty=0), auto=True)
    assert 'qty_zero' in v2['skip_reasons']
