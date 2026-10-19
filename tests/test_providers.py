"""Tests providers.py against a LOCAL fake HTTP server (proves our request/response/error handling,
NOT the real Gemini/Groq APIs)."""
import json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
import providers
from reviewer.llm import LLMClient, LLMUnavailable, TransientError


def serve(script):
    """script: list of (status, headers, body_dict). Returns (server, seen_requests)."""
    seen = []
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(self.rfile.read(n))})
            status, headers, body = script[min(len(seen) - 1, len(script) - 1)]
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": '{"findings": []}'}]}}]}
OPENAI_OK = {"choices": [{"message": {"content": '{"matches_root_cause": true}'}}]}


def test_gemini_success_request_shape(monkeypatch):
    srv, seen = serve([(200, {}, GEMINI_OK)])
    monkeypatch.setenv("GEMINI_API_KEY", "k123")
    b = providers.gemini_backend(f"http://127.0.0.1:{srv.server_port}")
    assert b("gemini-x", "SYS", "USER", 0.0) == '{"findings": []}'
    r = seen[0]
    assert r["path"] == "/v1beta/models/gemini-x:generateContent" and r["headers"]["x-goog-api-key"] == "k123"
    assert r["body"]["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert r["body"]["contents"][0]["parts"][0]["text"] == "USER" and r["body"]["generationConfig"]["temperature"] == 0.0
    srv.shutdown()


def test_gemini_empty_candidates_returns_empty_text(monkeypatch):
    srv, _ = serve([(200, {}, {"promptFeedback": {"blockReason": "SAFETY"}})])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert providers.gemini_backend(f"http://127.0.0.1:{srv.server_port}")("m", "s", "u", 0.0) == ""
    srv.shutdown()


def test_openai_compat_success(monkeypatch):
    srv, seen = serve([(200, {}, OPENAI_OK)])
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    b = providers.openai_compat_backend(f"http://127.0.0.1:{srv.server_port}", "GROQ_API_KEY")
    assert b("llama", "SYS", "USER", 0.0) == '{"matches_root_cause": true}'
    assert seen[0]["headers"]["authorization"] == "Bearer gk" and seen[0]["path"] == "/chat/completions"
    assert seen[0]["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    srv.shutdown()


def test_429_is_transient_with_retry_after_and_client_recovers(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    srv, _ = serve([(429, {"Retry-After": "3"}, {"error": "slow down"})])
    with pytest.raises(TransientError) as ei:
        providers.gemini_backend(f"http://127.0.0.1:{srv.server_port}")("m", "s", "u", 0.0)
    assert ei.value.retry_after == 3.0
    srv.shutdown()
    srv2, _ = serve([(429, {"Retry-After": "3"}, {}), (200, {}, GEMINI_OK)])
    sleeps = []
    c = LLMClient(providers.gemini_backend(f"http://127.0.0.1:{srv2.server_port}"), "m", tmp_path, sleep=sleeps.append)
    assert c.complete("s", "u") == '{"findings": []}' and sleeps == [3.0]
    srv2.shutdown()


def test_daily_quota_429_fails_immediately_without_retries(monkeypatch, tmp_path):
    srv, seen = serve([(429, {}, {"error": "Resource exhausted: PerDay quota reached"})])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sleeps = []
    c = LLMClient(providers.gemini_backend(f"http://127.0.0.1:{srv.server_port}"), "m", tmp_path,
                  max_attempts=5, sleep=sleeps.append)
    with pytest.raises(providers.DailyQuotaError):
        c.complete("s", "u")
    assert len(seen) == 1
    assert c.network_calls == 1
    assert c.retries == 0
    assert sleeps == []
    srv.shutdown()


def test_persistent_503_ends_in_llm_unavailable(monkeypatch, tmp_path):
    srv, _ = serve([(503, {}, {"error": "down"})])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    c = LLMClient(providers.gemini_backend(f"http://127.0.0.1:{srv.server_port}"), "m", tmp_path,
                  max_attempts=3, sleep=lambda s: None)
    with pytest.raises(LLMUnavailable):
        c.complete("s", "u")
    srv.shutdown()


def test_auth_error_is_loud_not_retried(monkeypatch):
    srv, seen = serve([(401, {}, {"error": "bad key"})])
    monkeypatch.setenv("GEMINI_API_KEY", "bad")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        providers.gemini_backend(f"http://127.0.0.1:{srv.server_port}")("m", "s", "u", 0.0)
    assert len(seen) == 1
    srv.shutdown()


def test_connection_refused_is_transient(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with pytest.raises(TransientError):
        providers.gemini_backend("http://127.0.0.1:9")("m", "s", "u", 0.0)


def test_missing_key_and_same_family_guard(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    srv, _ = serve([(200, {}, GEMINI_OK)])
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        providers.gemini_backend(f"http://127.0.0.1:{srv.server_port}")("m", "s", "u", 0.0)
    srv.shutdown()
    monkeypatch.setenv("REVIEWER_PROVIDER", "groq"); monkeypatch.setenv("JUDGE_PROVIDER", "groq")
    with pytest.raises(RuntimeError, match="different"):
        providers.make_clients()
