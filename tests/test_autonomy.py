import config
from execution import start_execution_engine
from execution_service import validate_trade_candidate

def candidate(**kw):
    c={'symbol':'ABC','setup_grade':'A','score_total':40,'scores':{'catalyst':5},'details':{'spread_pct':0.001,'opening_range_confirmation':{'breakout_confirmed':True},'vwap_hold_reclaim':{'reclaimed_vwap':False}},'current_price':1.0,'buy_upper':1.1,'qty':10,'entry_price':1.0,'stop_price':0.9,'target_1':1.1,'target_2':1.2}
    c.update(kw);return c

def test_engine_idempotent():
    a=start_execution_engine(); b=start_execution_engine(); assert a['engine_started'] and b['engine_started']

def test_trigger_or_logic():
    v=validate_trade_candidate(candidate(), auto=False)
    assert v['ok']

def test_auto_trade_disabled_block():
    orig=config.AUTO_TRADE_ENABLED
    config.AUTO_TRADE_ENABLED=False
    v=validate_trade_candidate(candidate(), auto=True)
    assert not v['ok'] and 'auto_trade_disabled' in v['skip_reasons']
    config.AUTO_TRADE_ENABLED=orig

def test_scan_price_config():
    assert config.SCAN_MIN_PRICE == float(config.SCAN_MIN_PRICE)
