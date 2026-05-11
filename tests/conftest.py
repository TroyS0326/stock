import os

os.environ.setdefault("DISABLE_AUTO_START_FOR_TESTS", "1")
os.environ.setdefault("AUTO_START_EXECUTION_ENGINE", "0")
os.environ.setdefault("AUTO_TRADE_ENABLED", "1")
os.environ.setdefault("ACTIVE_PAPER_TRADING_MODE", "1")
os.environ.setdefault("SIMULATION_MODE", "1")
os.environ.setdefault("AUTO_CYCLE_REQUIRE_MARKET_OPEN", "0")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_SECRET", "test-secret")
os.environ.setdefault("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets")

import pytest
import app as app_module


def _ensure_global_test_client() -> None:
    """Normalize module-level Flask app so tests can always call app.app.test_client()."""
    flask_app = getattr(app_module, "app", None)
    if flask_app is None:
        raise RuntimeError("app module is missing Flask app instance")

    flask_app.testing = True
    test_client = getattr(flask_app, "test_client", None)
    if not callable(test_client):
        raise RuntimeError("Flask app instance does not expose a callable test_client")


_ensure_global_test_client()


@pytest.fixture
def flask_app():
    _ensure_global_test_client()
    return app_module.app


@pytest.fixture
def client(flask_app):
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def app_context(flask_app):
    with flask_app.app_context():
        yield


@pytest.fixture(autouse=True)
def reset_runtime_state():
    try:
        import execution
        execution.RUNTIME_STATE.clear()
    except Exception:
        pass
    try:
        import app
        app.RUNTIME_STATE.clear()
    except Exception:
        pass
    yield
