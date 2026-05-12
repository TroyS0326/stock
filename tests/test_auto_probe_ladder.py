import execution_service
import app
import config


def _base_candidate(**kw):
    c={"symbol":"XYZ","setup_grade":"A","decision":"BUY NOW","score_total":35,"scores":{"catalyst":3},"details":{"spread_pct":0.002,"entry_trigger":"ORB_BREAKOUT","momentum_continuation":True},"current_price":10,"entry_price":10,"stop_price":9,"target_1":10.5,"target_2":11,"buy_lower":9.8,"buy_upper":10.2,"qty":2,"hard_reject_reasons":[],"why_not_buying":[]}
    c.update(kw)
    return c


def _patch_common(monkeypatch):
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'estimated_daily_loss_risk_used_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'has_active_user_symbol_trade', lambda u,s: False)
    monkeypatch.setattr(execution_service, 'has_active_symbol_exposure', lambda s: False)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(execution_service, 'get_runtime_trade_blocks', lambda: [])


def test_watch_probe_from_zero_qty(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    c=_base_candidate(setup_grade='WATCH', decision='WATCH FOR BREAKOUT', qty=0, score_total=20, details={'spread_pct':0.004,'entry_trigger':'NO_TRIGGER','momentum_continuation':True})
    v=execution_service.validate_trade_candidate(c, auto=True)
    assert v['ok'] and v['probe_trade'] and v['probe_qty'] == 1


def test_probe_rejects_wide_spread(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    c=_base_candidate(setup_grade='WATCH', decision='WATCH FOR BREAKOUT', qty=1, score_total=20, details={'spread_pct':0.02,'entry_trigger':'NO_TRIGGER','momentum_continuation':True})
    v=execution_service.validate_trade_candidate(c, auto=True)
    assert not v['ok']


def test_auto_cycle_tries_later_candidate(monkeypatch):
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True,'ok'))
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick':_base_candidate(symbol='AAA', setup_grade='WATCH', decision='WAIT'),'watchlist':[_base_candidate(symbol='BBB')]})
    monkeypatch.setattr(app, 'insert_scan', lambda r: 1)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda x: None)
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': c['symbol']=='BBB', 'skip_reasons': ([] if c['symbol']=='BBB' else ['auto_decision_not_actionable'])})
    calls=[]
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda c, source='auto': calls.append(c['symbol']) or {'trade_id':1,'order':{'id':'o1','status':'filled'}})
    app.run_scan_and_maybe_auto_trade()
    assert calls==['BBB']
