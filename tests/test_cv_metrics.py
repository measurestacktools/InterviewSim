"""CV-tailored plans + speech metrics. Offline except via mocked Groq.

Covers: metrics pure functions, CV upload/parse/caps, plan generation
(local fallback + mocked-Groq), answer metrics incl. duration_ms,
report aggregates, and quick-start-without-CV backward compatibility.
"""
from unittest import mock

from fastapi.testclient import TestClient

import app as server
from app import app

client = TestClient(app)


def setup_function(_):
    server._session_key = None
    server.reset_interview()
    server.reset_cv()


def make_pdf(line: str) -> bytes:
    esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = b"BT /F1 12 Tf 72 720 Td (" + esc.encode("latin-1") + b") Tj ET"
    objs = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj",
        (b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj"),
        b"4 0 obj << /Length " + str(len(content)).encode() + b" >> stream\n" + content + b"\nendstream endobj",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for o in objs:
        offs.append(len(out))
        out += o + b"\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for o in offs:
        out += ("%010d 00000 n \n" % o).encode()
    out += (b"trailer << /Size " + str(len(objs) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF")
    return bytes(out)


# ---------- metrics math (pure) ----------

def test_count_words_basic():
    assert server.count_words("") == 0
    assert server.count_words("hello world") == 2
    assert server.count_words("  spaced   out\ttokens\n") == 3


def test_count_fillers_known_list():
    text = "Um, I think uh like you know basically actually literally kind of sort of kinda sorta I mean done"
    assert server.count_fillers(text) == 12
    assert server.count_fillers("Clean answer with no disfluencies here.") == 0
    assert server.count_fillers("") == 0


def test_compute_wpm_needs_duration():
    assert server.compute_wpm(120, None) is None
    assert server.compute_wpm(120, 500) is None  # <1s too noisy
    assert server.compute_wpm(0, 60000) is None
    assert server.compute_wpm(150, 60000) == 150  # 150 words in 60s
    assert server.compute_wpm(75, 30000) == 150


def test_star_coverage_heuristic():
    m = server.star_coverage(
        "The situation was a failing project. My task was the API goal. "
        "My action: I built and implemented the fix. The result improved latency 20%."
    )
    assert m["count"] == 4
    assert m["heuristic"] is True
    empty = server.star_coverage("I like coding very much indeed.")
    assert empty["count"] == 0


def test_compute_answer_metrics_shape():
    m = server.compute_answer_metrics("Um hello world", 30000)
    assert m["words"] == 3
    assert m["fillers"] == 1
    assert m["wpm"] == 6
    assert m["duration_ms"] == 30000
    assert m["star"]["heuristic"] is True
    m2 = server.compute_answer_metrics("Hello world")
    assert m2["wpm"] is None and m2["duration_ms"] is None


# ---------- CV upload ----------

def test_cv_upload_txt():
    body = b"Jane Doe\nSenior Python Engineer\nBuilt FastAPI services on AWS with Docker."
    r = client.post("/api/cv/upload", files={"file": ("cv.txt", body, "text/plain")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["chars"] == len(body.decode())
    assert j["truncated"] is False
    assert "python" in j["skills"]
    assert "Jane Doe" in j["summary"]
    s = client.get("/api/cv").json()
    assert s["has_cv"] is True and s["filename"] == "cv.txt"


def test_cv_upload_pdf_parsed():
    pdf = make_pdf("John Doe Python Engineer with Docker and AWS experience")
    r = client.post("/api/cv/upload", files={"file": ("cv.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert "Python Engineer" in j["summary"]
    assert "docker" in j["skills"]


def test_cv_upload_bad_extension():
    r = client.post("/api/cv/upload", files={"file": ("cv.docx", b"junk", "application/octet-stream")})
    assert r.status_code == 400


def test_cv_upload_too_large():
    big = b"x" * (server.MAX_CV_BYTES + 1)
    r = client.post("/api/cv/upload", files={"file": ("cv.txt", big, "text/plain")})
    assert r.status_code == 413


def test_cv_upload_truncates_long_text():
    big = ("Python engineer line.\n" * 2000).encode()  # ~44k chars, under 2MB
    r = client.post("/api/cv/upload", files={"file": ("cv.txt", big, "text/plain")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["truncated"] is True
    assert j["chars"] == server.MAX_CV_CHARS


def test_cv_delete_clears():
    client.post("/api/cv/upload", files={"file": ("cv.txt", b"Python dev", "text/plain")})
    assert client.get("/api/cv").json()["has_cv"] is True
    assert client.delete("/api/cv").json()["has_cv"] is False
    assert client.get("/api/cv").json()["has_cv"] is False


# ---------- plans ----------

def test_plan_local_fallback_no_key():
    r = client.post("/api/plan", json={"topic": "Python", "difficulty": "Mid", "count": 3,
                                       "role": "Backend Engineer", "company": "Acme", "round": "Screen"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and len(j["plan"]) == 3
    assert j["source"] == "local" and j["cv_used"] is False
    kinds = {p["kind"] for p in j["plan"]}
    assert kinds == {"background probe", "skill gap", "behavioral"}
    assert [p["n"] for p in j["plan"]] == [1, 2, 3]


def test_plan_uses_cv_when_present():
    client.post("/api/cv/upload", files={"file": ("cv.txt", b"Python Docker dev", "text/plain")})
    j = client.post("/api/plan", json={"topic": "Python", "difficulty": "Junior", "count": 3}).json()
    assert j["cv_used"] is True


def test_plan_bad_topic():
    r = client.post("/api/plan", json={"topic": "Cobol", "difficulty": "Junior", "count": 3})
    assert r.status_code == 400


def test_plan_groq_path_mocked():
    server._session_key = "test-key"
    canned = [{"n": 1, "question": "Groq Q1?", "kind": "behavioral"}]
    with mock.patch.object(server, "groq_generate_plan", return_value=canned):
        j = client.post("/api/plan", json={"topic": "Python", "difficulty": "Junior", "count": 3}).json()
    assert j["source"] == "groq" and j["plan"] == canned


# ---------- mocked end-to-end: plan -> start -> answer -> report ----------

def _mocked_start(**kw):
    server._session_key = "test-key"
    args = {"topic": "Python", "difficulty": "Junior", "count": 3}
    args.update(kw)
    with mock.patch.object(server, "groq_generate_question", return_value="Generic Q?") as g:
        r = client.post("/api/start", json=args)
    return r, g


def _eval(**kw):
    d = {"score": 70, "strengths": ["clear"], "missing": ["depth"],
         "feedback": "Solid.", "followup": None}
    d.update(kw)
    return d


def test_quick_start_without_cv_unchanged():
    r, g = _mocked_start()
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["current"]["question"] == "Generic Q?"
    assert j["cv_used"] is False and "plan" not in j
    assert g.call_count == 1  # classic Groq Q1 path


def test_cv_plan_flows_into_start_and_answers():
    client.post("/api/cv/upload", files={"file": ("cv.txt", b"Python Docker dev", "text/plain")})
    client.post("/api/plan", json={"topic": "Python", "difficulty": "Junior", "count": 3,
                                   "role": "Backend Engineer", "company": "Acme"})
    server._session_key = "test-key"
    with mock.patch.object(server, "groq_generate_question", return_value="SHOULD NOT BE USED") as g:
        r = client.post("/api/start", json={"topic": "Python", "difficulty": "Junior",
                                            "count": 3, "use_plan": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["cv_used"] is True and len(j["plan"]) == 3
    assert g.call_count == 0  # Q1 came from the plan
    q1, q2 = j["plan"][0]["question"], j["plan"][1]["question"]
    assert j["current"]["question"] == q1
    with mock.patch.object(server, "groq_evaluate", return_value=_eval()):
        a1 = client.post("/api/answer", json={"answer": "Um first answer here", "duration_ms": 30000}).json()
    assert a1["next"]["question"] == q2  # plan order honored
    assert a1["evaluation"]["metrics"]["words"] == 4
    assert a1["evaluation"]["metrics"]["fillers"] == 1


def test_answer_metrics_with_and_without_duration():
    _mocked_start()
    with mock.patch.object(server, "groq_evaluate", return_value=_eval()), \
         mock.patch.object(server, "groq_generate_question", return_value="Next Q?"):
        with_duration = client.post("/api/answer",
                                    json={"answer": "Um hello world test", "duration_ms": 40000}).json()
    assert with_duration["evaluation"]["metrics"]["wpm"] == 6  # 4 words / 40s
    assert with_duration["evaluation"]["metrics"]["duration_ms"] == 40000
    # backward compat: no duration -> wpm None, old fields intact
    with mock.patch.object(server, "groq_evaluate", return_value=_eval()), \
         mock.patch.object(server, "groq_generate_question", return_value="Next Q?"):
        r = client.post("/api/answer", json={"answer": "Another plain answer here"})
    j = r.json()
    assert j["evaluation"]["metrics"]["wpm"] is None
    assert j["evaluation"]["score"] == 70 and "feedback" in j["evaluation"]


def test_report_includes_speech_metrics():
    _mocked_start()
    fake_report = {"per_question": [{"n": 1, "score": 70}], "overall": 70,
                   "concepts_demonstrated": ["x"], "missing_concepts": ["y"],
                   "study_topics": ["z"], "stronger_answer_example": "ex",
                   "verdict": "HIRE"}
    with mock.patch.object(server, "groq_evaluate", return_value=_eval()), \
         mock.patch.object(server, "groq_generate_question", return_value="Next Q?"):
        client.post("/api/answer", json={"answer": "Um first answer words here", "duration_ms": 30000})
    with mock.patch.object(server, "groq_report", return_value=dict(fake_report)):
        rep = client.post("/api/report").json()
    assert rep["report"]["verdict"] == "HIRE"  # old shape intact
    sm = rep["speech_metrics"]
    assert sm["total_answers"] == 1 and sm["total_words"] == 5
    assert sm["total_fillers"] == 1 and sm["measurable_answers"] == 1
    assert sm["avg_wpm"] == 10  # 5 words / 30s
    assert len(sm["per_answer"]) == 1
