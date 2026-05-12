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
import sys
import types
try:
    import flask  # type: ignore
except Exception:
    flask_stub = types.ModuleType("flask")
    flask_stub.__file__ = __file__
    class _DummyFlask:
        def __init__(self, *a, **k):
            self.testing = False
            self.config = {}
        def route(self, *a, **k):
            def deco(fn): return fn
            return deco
        def errorhandler(self, *a, **k):
            def deco(fn): return fn
            return deco
        def test_client(self):
            class _C:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def get(self, *a, **k): return types.SimpleNamespace(status_code=200, get_json=lambda: {}, data=b"")
                def post(self, *a, **k): return types.SimpleNamespace(status_code=200, get_json=lambda: {}, data=b"")
            return _C()
        def app_context(self):
            class _Ctx:
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return _Ctx()
    flask_stub.Flask = _DummyFlask
    flask_stub.jsonify = lambda *a, **k: {}
    flask_stub.render_template = lambda *a, **k: ""
    flask_stub.request = types.SimpleNamespace(args={}, json={})
    sys.modules["flask"] = flask_stub
if "werkzeug.exceptions" not in sys.modules:
    wz = types.ModuleType("werkzeug")
    wz_exc = types.ModuleType("werkzeug.exceptions")
    class HTTPException(Exception):
        pass
    wz_exc.HTTPException = HTTPException
    sys.modules["werkzeug"] = wz
    sys.modules["werkzeug.exceptions"] = wz_exc
if "flask_sock" not in sys.modules:
    fs = types.ModuleType("flask_sock")
    class Sock:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k):
            def deco(fn): return fn
            return deco
        def errorhandler(self, *a, **k):
            def deco(fn): return fn
            return deco
    fs.Sock = Sock
    sys.modules["flask_sock"] = fs
if "dotenv" not in sys.modules:
    dm = types.ModuleType("dotenv")
    dm.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dm
if "requests" not in sys.modules:
    rm = types.ModuleType("requests")
    class _Resp:
        status_code = 200
        text = ""
        def json(self): return {}
    rm.get = rm.post = rm.patch = rm.delete = lambda *a, **k: _Resp()
    sys.modules["requests"] = rm
if "websockets" not in sys.modules:
    wm = types.ModuleType("websockets")
    wm.connect = lambda *a, **k: None
    sys.modules["websockets"] = wm
if "apscheduler" not in sys.modules:
    aps_mod = types.ModuleType("apscheduler")
    sched_mod = types.ModuleType("apscheduler.schedulers")
    bg_mod = types.ModuleType("apscheduler.schedulers.background")
    class _DummyScheduler:
        def __init__(self,*a,**k): self.running=False
        def add_job(self,*a,**k): pass
        def start(self): self.running=True
        def get_jobs(self): return []
    bg_mod.BackgroundScheduler = _DummyScheduler
    sys.modules["apscheduler"] = aps_mod
    sys.modules["apscheduler.schedulers"] = sched_mod
    sys.modules["apscheduler.schedulers.background"] = bg_mod

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
