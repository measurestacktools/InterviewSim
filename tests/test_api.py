"""Validation + state-transition tests that need NO AI calls.

AI paths (start/answer/report happy paths) are covered by the live test,
not here — so this suite runs offline with no Groq key.
"""
from fastapi.testclient import TestClient

import app as server
from app import app

client = TestClient(app)


def setup_function(_):
    server._session_key = None
    server.reset_interview()


def test_status():
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    assert j["model"] == "openai/gpt-oss-120b"
    assert "key_configured" in j


def test_key_status_and_delete():
    r = client.get("/api/key/status")
    assert r.status_code == 200
    client.delete("/api/key")
    r = client.get("/api/key/status")
    assert r.json()["source"] in ("env", "none")


def test_key_empty_rejected():
    r = client.post("/api/key", json={"key": ""})
    assert r.status_code == 400


def test_start_bad_topic():
    r = client.post("/api/start", json={"topic": "Cobol", "difficulty": "Junior", "count": 3})
    assert r.status_code == 400
    assert "topic" in r.json()["error"].lower()


def test_start_bad_difficulty():
    r = client.post("/api/start", json={"topic": "Python", "difficulty": "Expert", "count": 3})
    assert r.status_code == 400


def test_start_bad_count():
    for bad in (0, 4, 7, 99, None):
        r = client.post("/api/start", json={"topic": "Python", "difficulty": "Junior", "count": bad})
        assert r.status_code == 400, bad


def test_answer_without_start():
    r = client.post("/api/answer", json={"answer": "A perfectly valid answer string."})
    assert r.status_code == 400
    assert "no active" in r.json()["error"].lower()


def test_answer_empty_rejected():
    for bad in ("", "   ", None):
        r = client.post("/api/answer", json={"answer": bad})
        assert r.status_code == 400, repr(bad)


def test_answer_too_long():
    r = client.post("/api/answer", json={"answer": "x" * 2001})
    assert r.status_code == 400
    assert "too long" in r.json()["error"].lower()


def test_report_without_start():
    r = client.post("/api/report")
    assert r.status_code == 400


def test_quit_without_active():
    r = client.post("/api/quit")
    assert r.status_code == 400


def test_session_shape():
    r = client.get("/api/session")
    assert r.status_code == 200
    assert r.json()["active"] is False
