import config
import broker
import sim_broker

BrokerError = broker.BrokerError

def _backend():
    return sim_broker if config.SIMULATION_MODE else broker


def place_managed_entry_order(*a, **k): return _backend().place_managed_entry_order(*a, **k)
def submit_market_sell(*a, **k): return _backend().submit_market_sell(*a, **k)
def submit_stop_sell(*a, **k): return _backend().submit_stop_sell(*a, **k)
def submit_trailing_stop_sell(*a, **k): return _backend().submit_trailing_stop_sell(*a, **k)
def get_open_positions(*a, **k): return _backend().get_open_positions(*a, **k)
def get_open_orders(*a, **k): return _backend().get_open_orders(*a, **k)
def get_order(*a, **k): return _backend().get_order(*a, **k)
def cancel_open_orders_for_symbol(*a, **k): return _backend().cancel_open_orders_for_symbol(*a, **k)
def close_position(*a, **k): return _backend().close_position(*a, **k)
def get_account(*a, **k): return _backend().get_account(*a, **k) if config.SIMULATION_MODE else broker.get_account(*a, **k)
def get_clock(*a, **k): return {'is_open': True, 'timestamp': ''} if config.SIMULATION_MODE else broker.get_clock(*a, **k)
def get_latest_quote(*a, **k): return broker.get_latest_quote(*a, **k)
def maybe_activate_runner_trailing(*a, **k): return broker.maybe_activate_runner_trailing(*a, **k)
def replace_order(*a, **k):
    if not config.SIMULATION_MODE:
        return broker.replace_order(*a, **k)
    order_id = a[0]
    updates = a[1] if len(a) > 1 else (k.get('patch') or {})
    return sim_broker.replace_order(order_id, updates)
def replace_order_qty(*a, **k):
    if not config.SIMULATION_MODE:
        return broker.replace_order_qty(*a, **k)
    order_id = a[0]
    qty = a[1] if len(a) > 1 else k.get('qty')
    return sim_broker.replace_order_qty(order_id, qty)
