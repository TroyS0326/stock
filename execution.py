import asyncio, json, logging, threading
from datetime import datetime
from zoneinfo import ZoneInfo
import requests, websockets
from apscheduler.schedulers.background import BackgroundScheduler

import config
from broker import maybe_activate_runner_trailing
from db import get_trade_by_target1_id, update_trade_status

logger=logging.getLogger(__name__)
ALPACA_WSS_URL = config.ALPACA_PAPER_BASE.replace('https', 'wss') + '/stream'
RUNTIME_STATE={
 'engine_started':False,'scheduler_running':False,'trade_stream_thread_alive':False,
 'last_scan_at':None,'last_scan_error':None,'last_auto_trade_at':None,'last_auto_trade_error':None,
 'last_position_monitor_at':None,'last_position_monitor_error':None,'auto_trade_enabled':config.AUTO_TRADE_ENABLED,
}
_scheduler=None
_ws_thread=None

def _alpaca_headers(): return {'accept':'application/json','APCA-API-KEY-ID':config.ALPACA_API_KEY,'APCA-API-SECRET-KEY':config.ALPACA_API_SECRET}

def flatten_book():
    try:
        requests.delete(f'{config.ALPACA_PAPER_BASE}/v2/orders', headers=_alpaca_headers(), timeout=10)
        requests.delete(f'{config.ALPACA_PAPER_BASE}/v2/positions', headers=_alpaca_headers(), timeout=10)
    except Exception as e: logger.error('Kill switch error: %s', e)

async def handle_fill_event(order):
    trade=get_trade_by_target1_id(order.get('id'))
    if not trade: return
    raw=trade.get('raw_json') or {}
    if isinstance(raw,str):
        try: raw=json.loads(raw)
        except: raw={}
    bundle=raw.get('order_bundle',{})
    updated=maybe_activate_runner_trailing(bundle, breakeven_price=float(trade.get('entry_price') or 0))
    raw['order_bundle']=updated
    update_trade_status(trade['order_id'], {'raw_json':raw})

async def alpaca_trade_listener():
    async for websocket in websockets.connect(ALPACA_WSS_URL):
        try:
            await websocket.send(json.dumps({'action':'auth','key':config.ALPACA_API_KEY,'secret':config.ALPACA_API_SECRET}))
            await websocket.recv(); await websocket.send(json.dumps({'action':'listen','data':{'streams':['trade_updates']}})); await websocket.recv()
            async for message in websocket:
                data=json.loads(message)
                if data.get('stream')=='trade_updates' and data.get('data',{}).get('event') in ('fill','partial_fill'):
                    await handle_fill_event(data.get('data',{}).get('order',{}))
        except Exception:
            await asyncio.sleep(1)

def run_async_loop_in_thread():
    loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop); loop.run_until_complete(alpaca_trade_listener())

def monitor_positions_job():
    RUNTIME_STATE['last_position_monitor_at']=datetime.utcnow().isoformat(); RUNTIME_STATE['last_position_monitor_error']=None

def start_execution_engine():
    global _scheduler,_ws_thread
    if RUNTIME_STATE['engine_started']:
        RUNTIME_STATE['trade_stream_thread_alive']=bool(_ws_thread and _ws_thread.is_alive())
        return RUNTIME_STATE
    _scheduler=BackgroundScheduler(timezone=ZoneInfo(config.TIMEZONE_LABEL))
    hh,mm=[int(x) for x in config.HARD_EXIT_TIME_ET.split(':',1)]
    _scheduler.add_job(flatten_book,'cron',day_of_week='mon-fri',hour=hh,minute=mm,id='flatten_book',replace_existing=True)
    _scheduler.add_job(monitor_positions_job,'interval',seconds=config.POSITION_MONITOR_INTERVAL_SECONDS,id='position_monitor',replace_existing=True)
    _scheduler.start()
    _ws_thread=threading.Thread(target=run_async_loop_in_thread,daemon=True,name='alpaca-trade-stream'); _ws_thread.start()
    RUNTIME_STATE.update({'engine_started':True,'scheduler_running':True,'trade_stream_thread_alive':True})
    return RUNTIME_STATE

def get_runtime_state():
    state=dict(RUNTIME_STATE)
    state['scheduler_running']=bool(_scheduler and _scheduler.running)
    state['trade_stream_thread_alive']=bool(_ws_thread and _ws_thread.is_alive())
    state['scheduled_jobs']=[j.id for j in (_scheduler.get_jobs() if _scheduler else [])]
    return state
