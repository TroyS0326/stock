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



def test_quick_profit_partial_sell_creates_trailing_protection_order(monkeypatch):
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
    assert raw['quick_profit_original_qty'] == 10
    assert raw['quick_profit_sell_qty'] == 5
    assert raw['quick_profit_remaining_qty'] == 5
    assert raw['quick_profit_protection_order_id'] == 'tp1'
    assert raw['quick_profit_protection_type'] == 'trailing_stop'
    assert 'quick_profit_forced_flatten_order_id' not in raw


def test_quick_profit_fallback_stop_when_trailing_fails(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{'target_1_order_id':'t1'}}}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'120'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':120})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'status':'new'})
    monkeypatch.setattr(execution, 'get_open_orders', lambda symbol:[{'id':'s1','side':'sell','qty':'5'}])
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda *a, **k:['s1'])
    monkeypatch.setattr(execution, 'submit_market_sell', lambda *a, **k:{'id':'m1'})
    monkeypatch.setattr(execution, 'submit_trailing_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('trail failed')))
    monkeypatch.setattr(execution, 'submit_stop_sell', lambda *a, **k:{'id':'sp1'})
    updates=[]
    monkeypatch.setattr(execution, 'update_trade_status', lambda oid, payload: updates.append(payload))
    execution.monitor_positions_job()
    raw = updates[0]['raw_json']
    assert raw['quick_profit_protection_order_id'] == 'sp1'
    assert raw['quick_profit_protection_type'] == 'stop'
    assert raw['quick_profit_protection_failed_reason'] is None


def test_quick_profit_forced_flatten_when_all_protection_orders_fail(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{'target_1_order_id':'t1'}}}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'120'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':120})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'status':'new'})
    monkeypatch.setattr(execution, 'get_open_orders', lambda symbol:[{'id':'s1','side':'sell','qty':'5'}])
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda *a, **k:['s1'])
    sells=[]
    monkeypatch.setattr(execution, 'submit_market_sell', lambda symbol, qty: sells.append((symbol, qty)) or {'id': f'm{len(sells)}'})
    monkeypatch.setattr(execution, 'submit_trailing_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('trail failed')))
    monkeypatch.setattr(execution, 'submit_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('stop failed')))
    updates=[]
    monkeypatch.setattr(execution, 'update_trade_status', lambda oid, payload: updates.append(payload))
    execution.RUNTIME_STATE['last_position_monitor_error'] = None
    execution.monitor_positions_job()
    raw = updates[0]['raw_json']
    assert sells == [('ABC', 5), ('ABC', 5)]
    assert raw['quick_profit_protection_type'] == 'forced_flatten'
    assert raw['quick_profit_forced_flatten_order_id'] == 'm2'
    assert raw['quick_profit_forced_flatten_reason'] == 'protection_order_failed'
    assert raw['quick_profit_remaining_qty_after_forced_flatten'] == 0
    assert raw['quick_profit_protection_failed_reason'].startswith('trailing:trail failed;stop:stop failed')
    assert execution.RUNTIME_STATE['last_position_monitor_error'] is not None


def test_quick_profit_forced_flatten_failure_sets_runtime_error(monkeypatch):
    trade={'symbol':'ABC','order_id':'o1','filled_avg_price':100,'raw_json':{'order_bundle':{'target_1_order_id':'t1'}}}
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'10','avg_entry_price':'100','current_price':'120'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[trade])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':120})
    monkeypatch.setattr(execution, 'get_order', lambda oid:{'status':'new'})
    monkeypatch.setattr(execution, 'get_open_orders', lambda symbol:[{'id':'s1','side':'sell','qty':'5'}])
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda *a, **k:['s1'])
    def _sell(symbol, qty):
        if qty == 5 and not getattr(_sell, 'did_partial', False):
            _sell.did_partial = True
            return {'id': 'm1'}
        raise execution.BrokerError('forced flatten failed')
    monkeypatch.setattr(execution, 'submit_market_sell', _sell)
    monkeypatch.setattr(execution, 'submit_trailing_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('trail failed')))
    monkeypatch.setattr(execution, 'submit_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('stop failed')))
    updates=[]
    monkeypatch.setattr(execution, 'update_trade_status', lambda oid, payload: updates.append(payload))
    execution.RUNTIME_STATE['last_position_monitor_error'] = None
    execution.monitor_positions_job()
    raw = updates[0]['raw_json']
    assert raw['quick_profit_protection_type'] == 'failed'
    assert raw['quick_profit_forced_flatten_failed_reason'] == 'forced flatten failed'
    assert execution.RUNTIME_STATE['last_position_monitor_error'] == 'forced flatten failed'



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


def test_grade_allows_orb_only_a_grade():
    grade, _ = scanner.classify_setup_grade(
        score_total=85,
        entry_trigger='ORB_BREAKOUT',
        hard_reject_reasons=[],
        component_scores={'premarket_gap_score': 4, 'premarket_dollar_volume_score': 4, 'relative_volume_score': 4, 'opening_strength_score': 4},
        catalyst_score=4,
        spread_safe=True,
        liquidity_score=3,
        qty=10,
    )
    assert grade == 'A'


def test_grade_allows_vwap_only_a_grade():
    grade, _ = scanner.classify_setup_grade(
        score_total=86,
        entry_trigger='VWAP_RECLAIM',
        hard_reject_reasons=[],
        component_scores={'premarket_gap_score': 4, 'premarket_dollar_volume_score': 4, 'relative_volume_score': 4, 'opening_strength_score': 4},
        catalyst_score=4,
        spread_safe=True,
        liquidity_score=3,
        qty=5,
    )
    assert grade == 'A'


def test_grade_watch_without_trigger():
    grade, _ = scanner.classify_setup_grade(
        score_total=80, entry_trigger='NO_TRIGGER', hard_reject_reasons=[], component_scores={}, catalyst_score=4, spread_safe=True, liquidity_score=3, qty=5
    )
    assert grade == 'WATCH'


def test_grade_no_trade_on_wide_spread():
    grade, _ = scanner.classify_setup_grade(
        score_total=90, entry_trigger='ORB_BREAKOUT', hard_reject_reasons=['spread_too_wide'], component_scores={}, catalyst_score=5, spread_safe=False, liquidity_score=1, qty=5
    )
    assert grade == 'NO TRADE'


def test_auto_execution_requires_buy_now(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_morning_scan_window', lambda: True)
    monkeypatch.setattr(execution_service.config, 'AUTO_TRADE_ENABLED', True)
    blocked = execution_service.validate_trade_candidate(candidate(decision='WAIT'), auto=True)
    assert 'auto_decision_not_actionable' in blocked['skip_reasons']


def test_run_scan_includes_rejected_candidates(monkeypatch):
    monkeypatch.setattr(scanner, 'get_refined_universe', lambda: (['SPY', 'ABC'], [{'symbol': 'ZZZ', 'price': 0.8, 'hard_reject_reasons': ['below_min_price'], 'soft_warning_reasons': [], 'why_not_buying': ['outside_scan_price_range']}]))
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols: {'SPY': {'prevDailyBar': {'c': 100}, 'dailyBar': {'c': 101}}, 'ABC': {'dailyBar': {'c': 1.0}, 'minuteBar': {'c': 1.0}, 'prevDailyBar': {'c': 0.95}}})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols: {'ABC': {'ap': 1.0, 'bp': 0.999}})
    monkeypatch.setattr(scanner, 'get_bars', lambda symbols, timeframe, start, end, limit: {s: [{'t': '2026-01-02T14:31:00Z', 'o': 1.0, 'h': 1.1, 'l': 0.9, 'c': 1.0, 'v': 10000}] * 40 for s in symbols})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(scanner, 'get_alpaca_asset', lambda symbol: {})
    monkeypatch.setattr(scanner, 'get_market_internals_bias', lambda: {'longs_blocked': False})
    monkeypatch.setattr(scanner, 'get_stock_chart_pack', lambda symbol: {'symbol': symbol, 'daily': [], 'intraday': []})
    monkeypatch.setattr(scanner, 'analyze_symbol', lambda *args, **kwargs: {'symbol': 'ABC', 'setup_grade': 'WATCH', 'decision': 'WATCH FOR BREAKOUT', 'score_total': 70, 'scores': {'catalyst': 4, 'sector_sympathy': 3}, 'details': {'open_relative_strength': {'edge': 1}, 'liquidity': {'spread': 0.001}}})
    result = scanner.run_scan()
    assert result['rejected_candidates'][0]['symbol'] == 'ZZZ'

def test_run_scan_attempts_multiple_candidates(monkeypatch):
    import app
    app.RUNTIME_STATE.clear()
    monkeypatch.setattr(app, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol':'AAA'}, 'watchlist':[{'symbol':'BBB'}]})
    monkeypatch.setattr(app, 'insert_scan', lambda r: 7)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'now_et', lambda: datetime(2026,1,1,10,0,0))
    def _validate(c, auto=False):
        return {'ok': c['symbol']=='BBB', 'skip_reasons': [] if c['symbol']=='BBB' else ['blocked']}
    calls=[]
    monkeypatch.setattr(app, 'validate_trade_candidate', _validate)
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda c, source='auto': calls.append(c['symbol']))
    app.run_scan_and_maybe_auto_trade()
    assert calls == ['BBB']
    assert [a['symbol'] for a in app.RUNTIME_STATE['last_auto_trade_attempts']] == ['AAA','BBB']
