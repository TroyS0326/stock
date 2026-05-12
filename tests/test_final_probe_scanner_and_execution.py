import pytest

import app
import broker
import config
import execution_service
import scanner
import sim_broker


def _patch_common_validation(monkeypatch):
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'estimated_daily_loss_risk_used_today', lambda: 0.0)
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'has_active_user_symbol_trade', lambda u, s: False)
    monkeypatch.setattr(execution_service, 'has_active_symbol_exposure', lambda s: False)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(execution_service, 'get_runtime_trade_blocks', lambda: [])


def _candidate(**kw):
    c = {
        'symbol': 'XYZ', 'setup_grade': 'A', 'decision': 'BUY NOW', 'score_total': 45,
        'scores': {'catalyst': 3}, 'details': {'spread_pct': 0.002, 'entry_trigger': 'BREAKOUT', 'momentum_continuation': True},
        'current_price': 10, 'entry_price': 10, 'stop_price': 9.5, 'target_1': 10.5, 'target_2': 11,
        'buy_lower': 9.8, 'buy_upper': 10.2, 'qty': 2, 'hard_reject_reasons': [], 'why_not_buying': []
    }
    c.update(kw)
    return c


def test_probe_ladder_blockers_and_soft_overrides(monkeypatch):
    _patch_common_validation(monkeypatch)
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(config, 'PAPER_TRADING_DETECTED', True)
    strict = execution_service.validate_trade_candidate(_candidate(), auto=True)
    assert strict['ok'] and not strict['probe_trade']

    watch = execution_service.validate_trade_candidate(_candidate(setup_grade='WATCH', decision='WATCH FOR BREAKOUT', qty=0), auto=True)
    assert watch['ok'] and watch['probe_trade'] and watch['probe_qty'] == 1 and watch['probe_qty_from_zero']
    assert 'aggressive_paper_probe' in watch['probe_reasons']

    invalid_risk = execution_service.validate_trade_candidate(_candidate(stop_price=10.1), auto=True)
    assert not invalid_risk['ok'] and 'invalid_risk' in invalid_risk['skip_reasons']

    for reason, patch in [
        ('market_data_unavailable', {'hard_reject_reasons': ['market_data_unavailable']}),
        ('no_quote', {'hard_reject_reasons': ['no_quote']}),
        ('duplicate_symbol_trade_blocked', {'hard_reject_reasons': ['duplicate_symbol_trade_blocked']}),
        ('daily_loss_limit_reached', None),
        ('max_auto_trades_reached', None),
    ]:
        if reason == 'daily_loss_limit_reached':
            monkeypatch.setattr(execution_service, 'estimated_daily_loss_risk_used_today', lambda: config.CURRENT_BANKROLL)
            v = execution_service.validate_trade_candidate(_candidate(), auto=True)
            monkeypatch.setattr(execution_service, 'estimated_daily_loss_risk_used_today', lambda: 0.0)
        elif reason == 'max_auto_trades_reached':
            monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: config.MAX_AUTO_TRADES_PER_DAY)
            v = execution_service.validate_trade_candidate(_candidate(), auto=True)
            monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
        else:
            v = execution_service.validate_trade_candidate(_candidate(**patch), auto=True)
        assert not v['ok']
        assert any(reason in s for s in v['skip_reasons'])

    monkeypatch.setattr(config, 'PROBE_MAX_SPREAD_PCT', 0.005)
    wide = execution_service.validate_trade_candidate(_candidate(setup_grade='WATCH', decision='WATCH FOR BREAKOUT', details={'spread_pct': 0.02, 'momentum_continuation': True}), auto=True)
    assert not wide['ok'] and 'probe_spread_too_wide' in wide['probe_reasons']

    monkeypatch.setattr(config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(config, 'PAPER_TRADING_DETECTED', False)
    nonpaper = execution_service.validate_trade_candidate(_candidate(setup_grade='WATCH', decision='WATCH FOR BREAKOUT'), auto=True)
    assert not nonpaper['ok'] and 'not_paper_or_simulation' in nonpaper['skip_reasons']


def test_scanner_normalization_and_validation(monkeypatch):
    good = scanner._ensure_candidate_execution_fields(_candidate(setup_grade='WATCH', decision='WATCH FOR BREAKOUT', qty=0))
    for key in ['symbol', 'setup_grade', 'decision', 'score_total', 'scores', 'details', 'qty', 'hard_reject_reasons', 'why_not_buying']:
        assert key in good
    _patch_common_validation(monkeypatch)
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(config, 'PAPER_TRADING_DETECTED', True)
    verdict = execution_service.validate_trade_candidate(good, auto=True)
    assert verdict['ok'] and verdict['probe_trade']

    bad = scanner._ensure_candidate_execution_fields(_candidate(current_price=0, entry_price=0, stop_price=0, target_1=0, target_2=-1, buy_upper=0))
    for reason in ['invalid_current_price', 'invalid_entry_price', 'invalid_stop_price', 'invalid_buy_upper', 'invalid_targets']:
        assert reason in bad['hard_reject_reasons'] and reason in bad['why_not_buying']
    vbad = execution_service.validate_trade_candidate(bad, auto=True)
    assert not vbad['ok']


def test_sim_child_orders_and_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'sim.sqlite3'))
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 0.0)
    order = sim_broker.place_managed_entry_order('ABCD', 1, 10.0, 9.0, 11.0, 12.0)
    assert order['runner_stop_order_id']
    with_child = sim_broker.get_open_orders('ABCD', include_child_orders=True)
    no_child = sim_broker.get_open_orders('ABCD')
    assert any(o.get('role') == 'runner_stop' for o in with_child)
    assert all(o.get('parent_order_id') is None for o in no_child)
    canceled = sim_broker.cancel_open_orders_for_symbol('ABCD', side='sell')
    assert canceled


def test_broker_retry_and_protection(monkeypatch):
    monkeypatch.setattr(broker, 'ALPACA_PAPER_BASE', 'https://paper-api.alpaca.markets')
    calls = {'poll': 0, 'max_prices': [], 'types': []}
    def fake_entry(**kwargs):
        calls['max_prices'].append(kwargs.get('max_limit_price'))
        calls['types'].append('limit')
        return {'id': f"o{len(calls['max_prices'])}"}
    def fake_poll(order_id, timeout):
        calls['poll'] += 1
        if calls['poll'] == 1:
            raise broker.BrokerError('timeout')
        return {'filled_qty': '1', 'filled_avg_price': '10.0'}
    monkeypatch.setattr(broker, '_pegged_limit_entry', fake_entry)
    monkeypatch.setattr(broker, '_poll_for_fill', fake_poll)
    monkeypatch.setattr(broker, 'submit_stop_sell', lambda s, q, p: {'id': 'st1'})
    out = broker.place_managed_entry_order('AAA', 1, 10, 9, 11, 12, max_entry_price=10.2)
    assert calls['poll'] == 2 and calls['max_prices'] == [10.2, 10.2] and out['runner_stop_order_id'] == 'st1'

    monkeypatch.setattr(broker, 'ALPACA_PAPER_BASE', 'https://api.alpaca.markets')
    calls['poll'] = 0
    with pytest.raises(broker.BrokerError):
        broker.place_managed_entry_order('AAA', 1, 10, 9, 11, 12, max_entry_price=10.2)

    monkeypatch.setattr(broker, 'ALPACA_PAPER_BASE', 'https://paper-api.alpaca.markets')
    monkeypatch.setattr(broker, '_poll_for_fill', lambda *a, **k: {'filled_qty': '1', 'filled_avg_price': '10.0'})
    monkeypatch.setattr(broker, 'submit_stop_sell', lambda *a, **k: (_ for _ in ()).throw(broker.BrokerError('stop fail')))
    flattened = []
    monkeypatch.setattr(broker, 'submit_market_sell', lambda s, q: flattened.append((s, q)) or {'id': 'm1'})
    with pytest.raises(broker.BrokerError):
        broker.place_managed_entry_order('AAA', 1, 10, 9, 11, 12)
    assert flattened == [('AAA', 1)]


def test_auto_cycle_attempt_state(monkeypatch):
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'ok'))
    monkeypatch.setattr(app, 'insert_scan', lambda _r: 1)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'AAA'}, 'watchlist': [{'symbol': 'BBB'}]})
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': True, 'skip_reasons': []})
    n = {'k': 0}
    def exec_c(c, source='auto'):
        n['k'] += 1
        if n['k'] == 1:
            raise app.BrokerError('entry_timeout_after_retry')
        return {'trade_id': 2, 'order': {'id': 'o2', 'status': 'filled'}}
    monkeypatch.setattr(app, 'execute_trade_candidate', exec_c)
    app.run_scan_and_maybe_auto_trade()
    assert app.RUNTIME_STATE['last_auto_trade_error'] is None

    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'CCC'}, 'watchlist': []})
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *a, **k: (_ for _ in ()).throw(app.BrokerError('entry_timeout_after_retry')))
    app.run_scan_and_maybe_auto_trade()
    assert 'entry_timeout_after_retry' in (app.RUNTIME_STATE['last_auto_trade_error'] or '')
    assert 'execution_failed' in (app.RUNTIME_STATE['last_auto_trade_skip_reasons'] or [])

    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'DDD'}, 'watchlist': [{'symbol': 'EEE'}]})
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': c['symbol'] == 'EEE', 'skip_reasons': ([] if c['symbol'] == 'EEE' else ['auto_decision_not_actionable'])})
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *a, **k: (_ for _ in ()).throw(app.BrokerError('entry_timeout_after_retry')))
    app.run_scan_and_maybe_auto_trade()
    syms = [a['symbol'] for a in app.RUNTIME_STATE['last_auto_trade_attempts']]
    assert 'DDD' in syms and 'EEE' in syms
