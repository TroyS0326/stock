import sys
import types
from datetime import datetime

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

import db
import execution
import execution_service
import scanner


def candidate(**kw):
    c = {'symbol':'ABC','setup_grade':'A','score_total':40,'scores':{'catalyst':5},'details':{'spread_pct':0.001,'opening_range_confirmation':{'breakout_confirmed':True},'vwap_hold_reclaim':{'reclaimed_vwap':False,'held_vwap':False}},'decision':'BUY NOW','current_price':1.0,'buy_upper':1.1,'qty':10,'entry_price':1.0,'stop_price':0.9,'target_1':1.1,'target_2':1.2}
    c.update(kw)
    return c

# existing

def test_trigger_or_logic_orb_and_vwap(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    assert execution_service.validate_trade_candidate(candidate(), auto=False)['ok']

def test_db_et_logic_helpers(monkeypatch):
    monkeypatch.setattr(db, 'today_et_prefix', lambda: '2026-01-02')
    rows = [
        {'created_at':'2026-01-03T02:00:00+00:00','symbol':'ABC','raw_json':'{"source":"auto"}','outcome':'failed'},
        {'created_at':'2026-01-02T03:00:00+00:00','symbol':'ABC','raw_json':'{"source":"manual"}','outcome':'failed'},
    ]
    class C:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def execute(self, q, p=()):
            class R:
                def fetchall(self2): return rows
            return R()
    monkeypatch.setattr(db, 'get_conn', lambda: C())
    assert db.count_trades_today(source='auto') == 1
    assert db.get_trade_by_symbol_today('ABC')['created_at'] == '2026-01-03T02:00:00+00:00'
    assert db.get_failed_trades_today() == 1


def test_monitor_no_action_below_threshold(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{}}}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'100'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':100})
    calls=[]
    monkeypatch.setattr(execution, 'update_trade_status', lambda *a, **k: calls.append(1))
    execution.monitor_positions_job()
    assert not calls


def test_monitor_breakeven_only_if_runner_open(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{'runner_stop_order_id':'r1'}}}
    monkeypatch.setattr(execution.config, 'QUICK_PROFIT_TAKE_PCT', 99)
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'110'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':110})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'id':oid,'status':'canceled'})
    rep=[]
    monkeypatch.setattr(execution, 'replace_order', lambda *a, **k: rep.append(1))
    updates=[]
    monkeypatch.setattr(execution, 'update_trade_status', lambda oid, payload: updates.append(payload))
    execution.monitor_positions_job()
    assert not rep
    assert 'breakeven_blocked_reason' in updates[0]['raw_json']


def test_monitor_quick_profit_reconcile_and_no_duplicate(monkeypatch):
    raw={'order_bundle':{'target_1_order_id':'t1'}, 'quick_profit_action_taken':False}
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':raw}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'120'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':120})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'status':'new'})
    monkeypatch.setattr(execution, 'get_open_orders', lambda symbol:[{'id':'s1','side':'sell','qty':'5'}])
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda *a, **k:['s1'])
    sells=[]
    monkeypatch.setattr(execution, 'submit_market_sell', lambda *a, **k: sells.append(1) or {'id':'m1'})
    monkeypatch.setattr(execution, 'submit_trailing_stop_sell', lambda *a, **k: {'id':'p1'})
    monkeypatch.setattr(execution, 'update_trade_status', lambda *a, **k: None)
    execution.monitor_positions_job()
    assert len(sells)==1
    trade['raw_json']['quick_profit_action_taken']=True
    execution.monitor_positions_job()
    assert len(sells)==1


def test_monitor_quick_profit_blocked_if_reconcile_fails(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{'target_1_order_id':'t1'}}}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'120'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':120})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'status':'new'})
    monkeypatch.setattr(execution, 'get_open_orders', lambda symbol:[{'id':'s1','side':'sell','qty':'5'}])
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('boom')))
    sells=[]
    monkeypatch.setattr(execution, 'submit_market_sell', lambda *a, **k: sells.append(1) or {'id':'m1'})
    updates=[]
    monkeypatch.setattr(execution, 'update_trade_status', lambda oid, payload: updates.append(payload))
    execution.monitor_positions_job()
    assert not sells
    assert updates[0]['raw_json']['quick_profit_blocked_reason'].startswith('cancel_failed:')
    assert execution.RUNTIME_STATE['last_position_monitor_error'].startswith('cancel_failed:')


def test_quick_profit_partial_sell_creates_protection_order(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{'target_1_order_id':'t1'}}}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'120'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':120})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'status':'new'})
    monkeypatch.setattr(execution, 'get_open_orders', lambda symbol:[{'id':'s1','side':'sell','qty':'5'}])
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda *a, **k:['s1'])
    monkeypatch.setattr(execution, 'submit_market_sell', lambda *a, **k:{'id':'m1'})
    monkeypatch.setattr(execution, 'submit_trailing_stop_sell', lambda *a, **k:{'id':'tp1'})
    updates=[]
    monkeypatch.setattr(execution, 'update_trade_status', lambda oid, payload: updates.append(payload))
    execution.monitor_positions_job()
    raw = updates[0]['raw_json']
    assert raw['quick_profit_sell_order_id'] == 'm1'
    assert raw['quick_profit_remaining_qty'] == 5
    assert raw['quick_profit_protection_order_id'] == 'tp1'
    assert raw['quick_profit_protection_type'] == 'trailing_stop'


def test_quick_profit_protection_failure_no_duplicate_market_sell_next_run(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{'target_1_order_id':'t1'}}}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'120'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':120})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'status':'new'})
    monkeypatch.setattr(execution, 'get_open_orders', lambda symbol:[{'id':'s1','side':'sell','qty':'5'}])
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda *a, **k:['s1'])
    sells=[]
    monkeypatch.setattr(execution, 'submit_market_sell', lambda *a, **k: sells.append(1) or {'id':'m1'})
    monkeypatch.setattr(execution, 'submit_trailing_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('trail failed')))
    monkeypatch.setattr(execution, 'submit_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('stop failed')))
    def _update(oid, payload):
        trade['raw_json'] = payload['raw_json']
    monkeypatch.setattr(execution, 'update_trade_status', _update)
    execution.monitor_positions_job()
    assert len(sells) == 1
    assert 'quick_profit_protection_failed_reason' in trade['raw_json']
    assert execution.RUNTIME_STATE['last_position_monitor_error'] is not None
    execution.monitor_positions_job()
    assert len(sells) == 1


def test_validate_candidate_adds_buy_window_closed(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: False)
    verdict = execution_service.validate_trade_candidate(candidate(), auto=False)
    assert 'buy_window_closed' in verdict['skip_reasons']


def test_scanner_trigger_orb_breakout():
    trigger, _ = scanner.detect_entry_trigger_name({'breakout_confirmed': True}, {'reclaimed_vwap': False, 'holds_last5': 1}, {'low_holds_vwap': False}, 0.2)
    assert trigger == 'ORB_BREAKOUT'


def test_scanner_trigger_vwap_reclaim():
    trigger, _ = scanner.detect_entry_trigger_name({'breakout_confirmed': False}, {'reclaimed_vwap': True, 'holds_last5': 4}, {'low_holds_vwap': True}, 0.4)
    assert trigger == 'VWAP_RECLAIM'


def test_no_trigger_blocks_auto_execution(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    c = candidate(details={'spread_pct': 0.001, 'entry_trigger': 'NO_TRIGGER'})
    verdict = execution_service.validate_trade_candidate(c, auto=True)
    assert 'no_valid_entry_trigger' in verdict['skip_reasons']


def test_scanner_trigger_preferred_by_execution():
    c = candidate(details={'spread_pct': 0.001, 'entry_trigger': 'VWAP_PULLBACK_BOUNCE', 'opening_range_confirmation': {'breakout_confirmed': True}})
    assert execution_service.detect_entry_trigger(c) == 'VWAP_PULLBACK_BOUNCE'
