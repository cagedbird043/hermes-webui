"""User-defined ``providers.<id>`` endpoints discover their catalog from ``/v1/models``.

Hermes Agent's picker probes ``base_url``/``url``/``api`` of such entries with the
``key_env`` credential; hermes_cli.provider_model_ids() only knows built-in
catalogs, so the WebUI must probe the endpoint itself.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import api.config as config

_KEY = "test-relay-key"
_IDS = ["relay-model-a", "relay-model-b", "relay-model-c"]


@pytest.fixture
def relay(monkeypatch):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append((self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != f"Bearer {_KEY}":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"object": "list", "data": [{"id": i, "object": "model"} for i in _IDS]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_RELAY_KEY", _KEY)
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", hits
    finally:
        server.shutdown()
        server.server_close()


def _models_with_providers(providers, active_provider="myrelay", default_model="relay-model-a"):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.invalidate_models_cache()
    config.cfg.clear()
    config.cfg["model"] = {"provider": active_provider, "default": default_model}
    config.cfg["providers"] = providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    try:
        return config.get_available_models(force_refresh=True)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


def _group_ids(result, provider_id):
    for group in result.get("groups", []):
        if group.get("provider_id") == provider_id:
            return [str(m["id"]).rsplit(":", 1)[-1] for m in group.get("models", [])]
    return None


def test_自定义provider按api地址和key_env实时发现模型(relay):
    base, hits = relay
    result = _models_with_providers({
        "myrelay": {
            "name": "myrelay",
            "api": base,
            "transport": "chat_completions",
            "key_env": "TEST_RELAY_KEY",
            "default_model": "relay-model-a",
        },
    })
    assert _group_ids(result, "myrelay") == _IDS
    assert hits and hits[0] == ("/v1/models", f"Bearer {_KEY}")


def test_写死模型或关闭发现的自定义provider不探测(relay):
    base, hits = relay
    result = _models_with_providers({
        "myrelay": {
            "name": "myrelay",
            "api": base,
            "key_env": "TEST_RELAY_KEY",
            "discover_models": False,
            "models": {"pinned-model": {"context_length": 1000}},
        },
    }, default_model="pinned-model")
    assert _group_ids(result, "myrelay") == ["pinned-model"]
    assert hits == []
