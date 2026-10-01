"""AI Interview Simulator — FastAPI backend (port 8009).

State machine (server-side, single active session):
  setup -> POST /api/start -> Q1 -> POST /api/answer -> ... -> POST /api/report
Text answers only (no voice). Groq via OpenAI SDK, model openai/gpt-oss-120b.
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

MODEL = "openai/gpt-oss-120b"
BASE_URL = "https://api.groq.com/openai/v1"

TOPICS = ["JavaScript", "Python", "Java", "C++", "React", "AI-ML", "SQL", "DSA", "General"]
DIFFICULTIES = ["Junior", "Mid", "Senior"]
COUNTS = [3, 5, 8]
MAX_FOLLOWUPS = 2
MAX_ANSWER = 2000

app = FastAPI(title="AI Interview Simulator")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ---- in-memory state ----
_session_key: Optional[str] = None  # settings-modal key (session memory only)

interview: Dict[str, Any] = {
    "active": False,
    "topic": None,
    "difficulty": None,
    "total": 0,
    "asked": [],          # [{n, question, is_followup}]
    "evaluations": [],    # [{n, question, answer, score, strengths, missing, feedback}]
    "current": None,      # {n, question, is_followup}
    "followups_used": 0,
    "ready_for_report": False,
    "finished": False,
    "last_report": None,
    "started_at": None,
    "question_started_at": None,
}


def reset_interview() -> None:
    interview.update({
        "active": False, "topic": None, "difficulty": None, "total": 0,
        "asked": [], "evaluations": [], "current": None,
        "followups_used": 0, "ready_for_report": False,
        "finished": False, "last_report": None,
        "started_at": None, "question_started_at": None,
    })


def get_api_key() -> Optional[str]:
    if _session_key:
        return _session_key
    for name in ("GROQ_API_KEY", "GROQ_TEST_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return None


def key_source() -> str:
    if _session_key:
        return "session"
    for name in ("GROQ_API_KEY", "GROQ_TEST_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            return "env"
    return "none"


def get_client():
    from openai import OpenAI
    key = get_api_key()
    if not key:
        raise NoKeyError("No Groq API key configured. Open Settings and paste a key, or set GROQ_API_KEY in .env.")
    return OpenAI(api_key=key, base_url=BASE_URL)


class NoKeyError(RuntimeError):
    pass


def groq_error_response(exc: Exception) -> JSONResponse:
    msg = str(exc)
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    code = ""
    try:
        code = type(exc).__name__
    except Exception:
        pass
    if status == 401 or "401" in msg or "invalid_api_key" in msg.lower() or "invalid api key" in msg.lower():
        return JSONResponse({"ok": False, "error": "Groq rejected the API key (401). Open Settings, check the key, and try again."}, status_code=502)
    if status == 429 or "429" in msg or "rate" in msg.lower():
        return JSONResponse({"ok": False, "error": "Groq rate limit hit (429). Wait ~30s and retry."}, status_code=502)
    if status == 404 or "404" in msg or "model" in msg.lower() and "not" in msg.lower():
        return JSONResponse({"ok": False, "error": f"Groq model not found (404): {MODEL}. It may be renamed — check Settings status."}, status_code=502)
    if status in (500, 502, 503) or "503" in msg or "overloaded" in msg.lower() or "connection" in msg.lower():
        return JSONResponse({"ok": False, "error": "Groq is temporarily unavailable (5xx / connection). Retry in a bit."}, status_code=502)
    if isinstance(exc, NoKeyError):
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    return JSONResponse({"ok": False, "error": f"AI request failed ({code or 'error'}): {msg[:300]}"}, status_code=502)


def extract_json(text: str) -> Dict[str, Any]:
    """Defensive JSON parse: direct load, else first {...} block, else {}."""
    if not text:
        return {}
    t = text.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # strip code fences
    t2 = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t2)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t2, flags=re.DOTALL)
    if m:
        for cand in (m.group(0), m.group(0) + "}"):
            try:
                return json.loads(cand)
            except Exception:
                continue
    return {}


def clamp_score(v: Any) -> int:
    try:
        s = int(float(v))
    except Exception:
        return 50
    return max(0, min(100, s))


def as_list(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x)[:200] for x in v if str(x).strip()][:6]
    if isinstance(v, str) and v.strip():
        return [v.strip()[:200]]
    return []


def groq_generate_question(topic: str, difficulty: str, n: int, total: int, previous: List[str]) -> str:
    client = get_client()
    prev = "\n".join(f"- {q}" for q in previous[-6:]) or "(none yet)"
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.7,
        messages=[
            {"role": "system", "content": "You are a strict but fair senior technical interviewer. Output JSON only."},
            {"role": "user", "content": (
                f"Write interview question {n} of {total} for a {difficulty} {topic} interview.\n"
                f"Previous questions (do NOT repeat):\n{prev}\n"
                f"Difficulty guidance: Junior=fundamentals, Mid=applied/debugging, Senior=design/trade-offs.\n"
                'Return JSON exactly: {"question": "<one clear question, 1-3 sentences>"}'
            )},
        ],
    )
    data = extract_json(resp.choices[0].message.content or "")
    q = str(data.get("question", "")).strip()
    if not q:
        # fallback: raw text
        raw = (resp.choices[0].message.content or "").strip()
        q = raw[:400] if raw else f"Explain a key {topic} concept appropriate for a {difficulty} engineer."
    return q[:600]


def groq_evaluate(topic: str, difficulty: str, question: str, answer: str) -> Dict[str, Any]:
    client = get_client()
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.3,
        messages=[
            {"role": "system", "content": "You are a strict but fair senior technical interviewer. Output JSON only."},
            {"role": "user", "content": (
                f"Topic: {topic}. Level: {difficulty}.\nQuestion: {question}\nCandidate answer: {answer}\n\n"
                "Score 0-100 (Junior: reward fundamentals; Senior: demand depth/trade-offs). "
                "List concrete strengths and what's missing. Give 2-3 sentence feedback. "
                "Optionally propose ONE sharp follow-up question string, or null if the answer was complete/off-topic.\n"
                'Return JSON exactly: {"score": <int>, "strengths": [<str>], "missing": [<str>], '
                '"feedback": "<str>", "followup": "<str or null>"}'
            )},
        ],
    )
    data = extract_json(resp.choices[0].message.content or "")
    followup = data.get("followup")
    if isinstance(followup, str):
        followup = followup.strip()[:600] or None
    else:
        followup = None
    return {
        "score": clamp_score(data.get("score", 50)),
        "strengths": as_list(data.get("strengths")) or ["Attempted an answer."],
        "missing": as_list(data.get("missing")),
        "feedback": str(data.get("feedback", "")).strip()[:800] or "No detailed feedback.",
        "followup": followup,
    }


def groq_report(topic: str, difficulty: str, evals: List[Dict[str, Any]], partial: bool) -> Dict[str, Any]:
    client = get_client()
    blob = "\n".join(
        f"Q{e['n']}: {e['question']}\nA: {e['answer'][:500]}\nScore: {e['score']}/100. Feedback: {e['feedback'][:300]}"
        for e in evals
    )
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.3,
        messages=[
            {"role": "system", "content": "You are a hiring-panel summarizer. Output JSON only."},
            {"role": "user", "content": (
                f"{'PARTIAL' if partial else 'FINAL'} interview report. Topic: {topic}. Level: {difficulty}. "
                f"Answered {len(evals)} question(s):\n{blob}\n\n"
                "Compute overall = rounded mean of scores. List concepts demonstrated, missing concepts, "
                "3-5 study topics, one stronger-answer example (4-6 sentences answering the weakest question well), "
                'and verdict exactly "HIRE" or "LEAN NO-HIRE".\n'
                'Return JSON exactly: {"per_question": [{"n": <int>, "score": <int>}], "overall": <int>, '
                '"concepts_demonstrated": [<str>], "missing_concepts": [<str>], "study_topics": [<str>], '
                '"stronger_answer_example": "<str>", "verdict": "HIRE or LEAN NO-HIRE"}'
            )},
        ],
    )
    data = extract_json(resp.choices[0].message.content or "")
    per = data.get("per_question")
    if not isinstance(per, list) or not per:
        per = [{"n": e["n"], "score": e["score"]} for e in evals]
    overall_vals = [clamp_score(p.get("score") if isinstance(p, dict) else p) for p in per]
    overall = round(sum(e["score"] for e in evals) / max(1, len(evals)))
    try:
        overall = clamp_score(data.get("overall", overall))
    except Exception:
        pass
    verdict = str(data.get("verdict", "")).upper()
    verdict = "HIRE" if "HIRE" in verdict and "NO" not in verdict else "LEAN NO-HIRE"
    return {
        "per_question": [{"n": e["n"], "score": e["score"]} for e in evals],
        "overall": overall,
        "concepts_demonstrated": as_list(data.get("concepts_demonstrated")),
        "missing_concepts": as_list(data.get("missing_concepts")),
        "study_topics": as_list(data.get("study_topics")) or [f"Review {topic} fundamentals."],
        "stronger_answer_example": str(data.get("stronger_answer_example", "")).strip()[:1500] or "No example generated.",
        "verdict": verdict,
        "_": overall_vals,  # internal, stripped before response
    }


# ---------- models ----------
class StartReq(BaseModel):
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    count: Optional[int] = None


class AnswerReq(BaseModel):
    answer: Optional[str] = None


class KeyReq(BaseModel):
    key: Optional[str] = None


# ---------- routes ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/status")
def status():
    src = key_source()
    return {
        "ok": True,
        "key_configured": src != "none",
        "key_source": src,
        "model": MODEL,
        "base_url": BASE_URL,
        "interview_active": interview["active"],
    }


@app.get("/api/key/status")
def key_status():
    src = key_source()
    return {"ok": True, "configured": src != "none", "source": src}


@app.post("/api/key")
def save_key(body: KeyReq):
    global _session_key
    k = (body.key or "").strip()
    if not k:
        return JSONResponse({"ok": False, "error": "Key is empty."}, status_code=400)
    # verify via models.list
    try:
        from openai import OpenAI
        c = OpenAI(api_key=k, base_url=BASE_URL)
        c.models.list()
    except Exception as exc:
        return groq_error_response(exc)
    _session_key = k
    return {"ok": True, "source": "session"}


@app.delete("/api/key")
def delete_key():
    global _session_key
    _session_key = None
    return {"ok": True, "source": key_source()}


@app.post("/api/start")
def start(body: StartReq):
    topic = (body.topic or "").strip()
    difficulty = (body.difficulty or "").strip()
    count = body.count
    if topic not in TOPICS:
        return JSONResponse({"ok": False, "error": f"Bad topic. Choose one of: {', '.join(TOPICS)}."}, status_code=400)
    if difficulty not in DIFFICULTIES:
        return JSONResponse({"ok": False, "error": f"Bad difficulty. Choose one of: {', '.join(DIFFICULTIES)}."}, status_code=400)
    if isinstance(count, bool):
        return JSONResponse({"ok": False, "error": "Bad question count. Choose 3, 5, or 8."}, status_code=400)
    if isinstance(count, str):
        s = count.strip()
        if not s.isdigit():
            return JSONResponse({"ok": False, "error": "Bad question count. Choose 3, 5, or 8."}, status_code=400)
        count = int(s)
    if not isinstance(count, int) or count not in COUNTS:
        return JSONResponse({"ok": False, "error": "Bad question count. Choose 3, 5, or 8."}, status_code=400)
    if not get_api_key():
        return JSONResponse({"ok": False, "error": "No Groq API key configured. Open Settings and paste a key, or set GROQ_API_KEY in .env."}, status_code=400)
    # double-start resets
    reset_interview()
    try:
        q1 = groq_generate_question(topic, difficulty, 1, count, [])
    except Exception as exc:
        return groq_error_response(exc)
    now = time.time()
    interview.update({
        "active": True, "topic": topic, "difficulty": difficulty, "total": count,
        "asked": [{"n": 1, "question": q1, "is_followup": False}],
        "evaluations": [], "current": {"n": 1, "question": q1, "is_followup": False},
        "followups_used": 0, "ready_for_report": False, "finished": False,
        "last_report": None, "started_at": now, "question_started_at": now,
    })
    return {
        "ok": True, "topic": topic, "difficulty": difficulty, "total": count,
        "current": {"n": 1, "question": q1, "is_followup": False},
        "progress": f"Q1/{count}",
    }


@app.post("/api/answer")
def answer(body: AnswerReq):
    ans = body.answer if isinstance(body.answer, str) else ""
    if not ans.strip():
        return JSONResponse({"ok": False, "error": "Answer is empty. Type something (max 2000 chars)."}, status_code=400)
    if len(ans.strip()) > MAX_ANSWER:
        return JSONResponse({"ok": False, "error": f"Answer too long ({len(ans.strip())} chars). Max {MAX_ANSWER}."}, status_code=400)
    if not interview["active"] or interview["current"] is None:
        if interview.get("last_report") and interview.get("finished"):
            return JSONResponse({"ok": False, "error": "Interview is finished. Request a report or start a new interview."}, status_code=400)
        return JSONResponse({"ok": False, "error": "No active interview. Start one first (POST /api/start)."}, status_code=400)
    if interview.get("ready_for_report"):
        return JSONResponse({"ok": False, "error": "All questions answered. Request the report (POST /api/report)."}, status_code=400)
    cur = interview["current"]
    try:
        ev = groq_evaluate(interview["topic"], interview["difficulty"], cur["question"], ans.strip())
    except Exception as exc:
        return groq_error_response(exc)
    record = {
        "n": cur["n"], "question": cur["question"], "answer": ans.strip()[:MAX_ANSWER],
        "score": ev["score"], "strengths": ev["strengths"], "missing": ev["missing"],
        "feedback": ev["feedback"], "is_followup": cur.get("is_followup", False),
    }
    interview["evaluations"].append(record)
    done = len(interview["evaluations"]) >= interview["total"]
    if done:
        interview["current"] = None
        interview["ready_for_report"] = True
        return {"ok": True, "done": True, "evaluation": record, "progress": f"Q{len(interview['evaluations'])}/{interview['total']}"}
    # decide next: follow-up or fresh
    nxt_q, is_fu = None, False
    if ev.get("followup") and interview["followups_used"] < MAX_FOLLOWUPS:
        nxt_q, is_fu = ev["followup"], True
        interview["followups_used"] += 1
    else:
        try:
            prev = [q["question"] for q in interview["asked"]]
            nxt_q = groq_generate_question(interview["topic"], interview["difficulty"], len(interview["evaluations"]) + 1, interview["total"], prev)
        except Exception as exc:
            return groq_error_response(exc)
    nxt_n = len(interview["evaluations"]) + 1
    interview["asked"].append({"n": nxt_n, "question": nxt_q, "is_followup": is_fu})
    interview["current"] = {"n": nxt_n, "question": nxt_q, "is_followup": is_fu}
    interview["question_started_at"] = time.time()
    out = dict(record)
    if is_fu:
        out["followup_next"] = True
    return {
        "ok": True, "done": False, "evaluation": out,
        "next": {"n": nxt_n, "question": nxt_q, "is_followup": is_fu},
        "progress": f"Q{nxt_n}/{interview['total']}",
        "followups_used": interview["followups_used"],
    }


def _build_report(partial: bool):
    evals = interview["evaluations"]
    try:
        rep = groq_report(interview["topic"], interview["difficulty"], evals, partial)
    except Exception as exc:
        raise exc
    rep.pop("_", None)
    return {
        "ok": True, "partial": partial,
        "topic": interview["topic"], "difficulty": interview["difficulty"],
        "answered": len(evals), "total": interview["total"],
        "report": rep,
    }


@app.post("/api/report")
def report():
    if not interview["active"] and not interview["evaluations"]:
        return JSONResponse({"ok": False, "error": "No active interview. Start one first."}, status_code=400)
    if not interview["evaluations"]:
        return JSONResponse({"ok": False, "error": "No answers yet. Answer at least one question first."}, status_code=400)
    partial = len(interview["evaluations"]) < interview["total"]
    try:
        out = _build_report(partial)
    except Exception as exc:
        return groq_error_response(exc)
    interview["last_report"] = out
    interview["active"] = False
    interview["finished"] = True
    interview["current"] = None
    interview["ready_for_report"] = False
    return out


@app.post("/api/quit")
def quit_interview():
    if not interview["active"]:
        return JSONResponse({"ok": False, "error": "No active interview to quit."}, status_code=400)
    if not interview["evaluations"]:
        reset_interview()
        return {"ok": True, "partial": True, "report": None, "message": "Quit. No answers given, so no report."}
    partial = len(interview["evaluations"]) < interview["total"]
    try:
        out = _build_report(partial=True)
    except Exception as exc:
        return groq_error_response(exc)
    out["quit"] = True
    interview["last_report"] = out
    interview["active"] = False
    interview["finished"] = True
    interview["current"] = None
    return out


@app.get("/api/session")
def session():
    return {
        "ok": True,
        "active": interview["active"],
        "topic": interview["topic"],
        "difficulty": interview["difficulty"],
        "total": interview["total"],
        "answered": len(interview["evaluations"]),
        "followups_used": interview["followups_used"],
        "ready_for_report": interview["ready_for_report"],
    }
