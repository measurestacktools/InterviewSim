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
from fastapi import FastAPI, File, UploadFile
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
MAX_CV_BYTES = 2 * 1024 * 1024  # ~2MB cap for CV uploads
MAX_CV_CHARS = 20000  # stored-text cap for CV uploads

app = FastAPI(title="AI Interview Simulator")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve the app icon so browsers never 404 on /favicon.ico."""
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")


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
    # CV-tailored plan state (additive; existing flows ignore these)
    "role": None,
    "company": None,
    "round": None,
    "cv_used": False,
    "plan_queue": [],     # upcoming planned questions [{question, kind}]
    "pending_plan": [],   # last generated plan from POST /api/plan
    "plan_meta": None,    # {role, company, round, cv_used, source}
}


def reset_interview() -> None:
    interview.update({
        "active": False, "topic": None, "difficulty": None, "total": 0,
        "asked": [], "evaluations": [], "current": None,
        "followups_used": 0, "ready_for_report": False,
        "finished": False, "last_report": None,
        "started_at": None, "question_started_at": None,
        "role": None, "company": None, "round": None,
        "cv_used": False, "plan_queue": [], "pending_plan": [], "plan_meta": None,
    })


# ---- CV store (server-side, single session slot) ----
cv_store: Dict[str, Any] = {
    "filename": None,
    "text": "",
    "summary": "",
    "skills": [],
    "chars": 0,
    "truncated": False,
    "uploaded_at": None,
}


def reset_cv() -> None:
    cv_store.update({
        "filename": None, "text": "", "summary": "",
        "skills": [], "chars": 0, "truncated": False, "uploaded_at": None,
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


# ---------- CV parsing / summarization (local, no AI needed) ----------
SKILL_KEYWORDS = [
    "python", "javascript", "typescript", "java", "c++", "c#", "go", "rust",
    "react", "node", "fastapi", "django", "flask", "sql", "postgres",
    "mongodb", "redis", "docker", "kubernetes", "aws", "gcp", "azure",
    "ci/cd", "git", "linux", "ml", "ai", "pytorch", "tensorflow",
    "pandas", "spark", "rest", "graphql", "testing", "agile",
]


def extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes via pypdf. Raises on unreadable input."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts: List[str] = []
    for page in reader.pages[:30]:  # bound work for adversarial files
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def extract_skills(text: str) -> List[str]:
    low = text.lower()
    found = []
    for kw in SKILL_KEYWORDS:
        if kw in low and kw not in found:
            found.append(kw)
    return found[:20]


def summarize_cv(text: str) -> str:
    """Extractive local summary: first non-empty lines, capped. No AI call."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = "; ".join(lines[:12])
    return head[:1200]


# ---------- speech / answer metrics (pure functions) ----------
# Filler definition: case-insensitive whole-phrase matches of common speech
# disfluencies. "like"/"well"-style words are counted only in this fixed list
# context (see regex); counts are a heuristic, not a diagnosis.
FILLER_RE = re.compile(
    r"\b(um+|uh+|er+|ah+|hmm+|basically|actually|literally|kinda|sorta)\b"
    r"|\blike\b"
    r"|\byou know\b|\bkind of\b|\bsort of\b|\bi mean\b",
    re.IGNORECASE,
)

STAR_KEYWORDS = {
    "situation": ["situation", "context", "background", "when ", "where ",
                  "team", "project", "company", "role"],
    "task": ["task", "goal", "objective", "needed", "responsible",
             "assigned", "challenge", "problem"],
    "action": ["action", "did ", "implemented", "built", "decided", "led ",
              "created", "steps", "approach", "designed", "wrote", "fixed"],
    "result": ["result", "outcome", "impact", "improved", "increased",
               "decreased", "delivered", "learned", "reduced", "shipped",
               "success", "%"],
}


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z0-9]+)?", text or ""))


def count_fillers(text: str) -> int:
    if not text:
        return 0
    return len(FILLER_RE.findall(text))


def compute_wpm(words: int, duration_ms: Any) -> Optional[int]:
    """Estimated WPM, only when client sends a usable duration.

    Returns None when no duration was sent or it is <1s (too noisy).
    This measures answer-production rate (meaningful for dictated answers);
    typed answers will read slow — labelled 'estimated' everywhere.
    """
    try:
        dur = int(duration_ms) if duration_ms is not None else None
    except Exception:
        return None
    if not dur or dur < 1000 or not words:
        return None
    return round(words * 60000 / dur)


def star_coverage(text: str) -> Dict[str, Any]:
    """STAR coverage heuristic (keyword check, NOT a real STAR analysis)."""
    low = f" {(text or '').lower()} "
    flags = {}
    for letter, kws in STAR_KEYWORDS.items():
        flags[letter] = any(kw in low for kw in kws)
    return {
        "situation": flags["situation"],
        "task": flags["task"],
        "action": flags["action"],
        "result": flags["result"],
        "count": sum(1 for v in flags.values() if v),
        "heuristic": True,
    }


def compute_answer_metrics(answer: str, duration_ms: Any = None) -> Dict[str, Any]:
    """Pure: per-answer speech metrics. duration_ms is client-measured."""
    try:
        dur = int(duration_ms) if duration_ms is not None else None
    except Exception:
        dur = None
    if dur is not None and dur < 0:
        dur = None
    words = count_words(answer)
    return {
        "words": words,
        "fillers": count_fillers(answer),
        "wpm": compute_wpm(words, dur),
        "duration_ms": dur,
        "star": star_coverage(answer),
    }


def aggregate_metrics(evals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-answer metrics for the final report (pure)."""
    per = []
    tot_words = 0
    tot_fillers = 0
    wpms: List[int] = []
    star_counts: List[int] = []
    for e in evals:
        m = e.get("metrics") or compute_answer_metrics(e.get("answer", ""))
        tot_words += m.get("words", 0)
        tot_fillers += m.get("fillers", 0)
        if m.get("wpm") is not None:
            wpms.append(m["wpm"])
        sc = (m.get("star") or {}).get("count", 0)
        star_counts.append(sc)
        per.append({
            "n": e.get("n"), "words": m.get("words", 0),
            "fillers": m.get("fillers", 0), "wpm": m.get("wpm"),
            "star_count": sc,
        })
    return {
        "total_answers": len(evals),
        "total_words": tot_words,
        "total_fillers": tot_fillers,
        "measurable_answers": len(wpms),
        "avg_wpm": round(sum(wpms) / len(wpms)) if wpms else None,
        "avg_star_count": round(sum(star_counts) / len(star_counts), 2) if star_counts else 0,
        "per_answer": per,
        "wpm_note": "estimated production-rate WPM (client-measured durations); None when not measurable",
        "star_note": "STAR coverage is a keyword heuristic, not a real STAR analysis",
    }


# ---------- CV-tailored question plans ----------
PLAN_KINDS = ["background probe", "skill gap", "behavioral"]


def build_local_plan(topic: str, difficulty: str, count: int,
                     role: str = "", company: str = "", round_: str = "",
                     cv_summary: str = "", skills: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Deterministic offline plan: cycles background probe / skill gap / behavioral."""
    skills = skills or []
    role_txt = role or f"{difficulty} {topic} engineer"
    co_txt = f" at {company}" if company else ""
    anchor = skills[0] if skills else topic
    questions = [
        (f"Walk me through your experience with {anchor} from your background — "
         f"what did you personally build or own, and what was the hardest part?"),
        (f"Which part of {topic} do you feel least confident in, and how would you "
         f"approach closing that gap in your first 90 days as {role_txt}{co_txt}?"),
        ("Tell me about a time you disagreed with a teammate or stakeholder — "
         "what was the situation, what action did you take, and what was the outcome?"),
        (f"Describe a {topic} project end-to-end from your past work: the context, "
         f"your specific contribution, and the measurable result."),
        (f"For a {role_txt} role{co_txt}, what adjacent skill outside your CV would matter "
         f"most, and how have you ramped on something unfamiliar before?"),
        (f"Tell me about a time you owned a failure or production incident — "
         f"what happened, what did you do, and what changed afterwards?"),
        (f"Explain a key {topic} trade-off you have made in a real system "
         f"({difficulty} level): what options did you weigh and why?"),
        (f"Why {company or 'this role'} for your next step as {role_txt}, "
         f"and how does your background map to it?"),
    ]
    plan = []
    for i in range(count):
        plan.append({
            "n": i + 1,
            "question": questions[i % len(questions)],
            "kind": PLAN_KINDS[i % len(PLAN_KINDS)],
        })
    return plan


def groq_generate_plan(topic: str, difficulty: str, count: int,
                       role: str, company: str, round_: str,
                       cv_summary: str, skills: List[str]) -> List[Dict[str, str]]:
    """Ask Groq for a tailored plan; raises on any failure (caller falls back)."""
    client = get_client()
    cv_txt = cv_summary or "(no CV provided — use generic role-based questions)"
    skill_txt = ", ".join(skills) if skills else "(none extracted)"
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.6,
        messages=[
            {"role": "system", "content": "You are a hiring manager designing interview plans. Output JSON only."},
            {"role": "user", "content": (
                f"Candidate CV summary: {cv_txt}\nSkills observed: {skill_txt}\n"
                f"Target role: {role or topic}. Company: {company or 'unspecified'}. "
                f"Round: {round_ or 'unspecified'}. Topic: {topic}. Level: {difficulty}.\n"
                f"Produce exactly {count} questions mixing: background probes (their experience), "
                f"skill gaps (missing/adjacent skills), behavioral (STAR-style teamwork/conflict/ownership).\n"
                'Return JSON exactly: {"questions": ['
                '{"question": "<1-3 sentences>", "kind": "background probe|skill gap|behavioral"}]}'
            )},
        ],
    )
    data = extract_json(resp.choices[0].message.content or "")
    raw = data.get("questions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("empty plan from model")
    allowed = set(PLAN_KINDS)
    plan = []
    for i, item in enumerate(raw[:count]):
        q = item.get("question", "").strip() if isinstance(item, dict) else str(item).strip()
        k = item.get("kind", "").strip().lower() if isinstance(item, dict) else ""
        if k not in allowed:
            k = PLAN_KINDS[i % len(PLAN_KINDS)]
        if q:
            plan.append({"n": len(plan) + 1, "question": q[:600], "kind": k})
    # fill shortfalls deterministically so count is always honored
    if len(plan) < count:
        for extra in build_local_plan(topic, difficulty, count, role, company, round_, cv_summary, skills)[len(plan):count]:
            extra["n"] = len(plan) + 1
            plan.append(extra)
    return plan


def make_plan(topic: str, difficulty: str, count: int,
              role: str = "", company: str = "", round_: str = "") -> tuple:
    """Build a plan, preferring Groq when a key exists; else local fallback.

    Returns (plan, source, cv_used). Never raises for missing key.
    """
    cv_used = bool(cv_store.get("text"))
    if get_api_key():
        try:
            plan = groq_generate_plan(topic, difficulty, count, role, company,
                                      round_, cv_store.get("summary", ""),
                                      cv_store.get("skills", []))
            return plan, "groq", cv_used
        except Exception:
            pass  # fall through to local
    plan = build_local_plan(topic, difficulty, count, role, company, round_,
                            cv_store.get("summary", ""), cv_store.get("skills", []))
    return plan, "local", cv_used


# ---------- models ----------
class StartReq(BaseModel):
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    count: Optional[int] = None
    role: Optional[str] = None       # additive: CV-tailored target role
    company: Optional[str] = None    # additive: CV-tailored target company
    round: Optional[str] = None      # additive: interview round name
    use_plan: Optional[bool] = None  # additive: ask from tailored plan when available


class AnswerReq(BaseModel):
    answer: Optional[str] = None
    duration_ms: Optional[int] = None  # additive: client-measured answer capture time


class PlanReq(BaseModel):
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    count: Optional[int] = None
    role: Optional[str] = None
    company: Optional[str] = None
    round: Optional[str] = None


def _validate_setup(topic: Any, difficulty: Any, count: Any):
    """Shared topic/difficulty/count validation. Returns ((t, d, c), err_response)."""
    t = (topic or "").strip() if isinstance(topic, str) else ""
    d = (difficulty or "").strip() if isinstance(difficulty, str) else ""
    if t not in TOPICS:
        return None, JSONResponse({"ok": False, "error": f"Bad topic. Choose one of: {', '.join(TOPICS)}."}, status_code=400)
    if d not in DIFFICULTIES:
        return None, JSONResponse({"ok": False, "error": f"Bad difficulty. Choose one of: {', '.join(DIFFICULTIES)}."}, status_code=400)
    c = count
    if isinstance(c, bool):
        return None, JSONResponse({"ok": False, "error": "Bad question count. Choose 3, 5, or 8."}, status_code=400)
    if isinstance(c, str):
        s = c.strip()
        if not s.isdigit():
            return None, JSONResponse({"ok": False, "error": "Bad question count. Choose 3, 5, or 8."}, status_code=400)
        c = int(s)
    if not isinstance(c, int) or c not in COUNTS:
        return None, JSONResponse({"ok": False, "error": "Bad question count. Choose 3, 5, or 8."}, status_code=400)
    return (t, d, c), None


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


@app.post("/api/cv/upload")
async def cv_upload(file: UploadFile = File(...)):
    """Upload a CV (.pdf/.txt, ~2MB / 20k chars cap). Stored in session memory."""
    name = (file.filename or "").strip() or "upload"
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".pdf", ".txt"):
        return JSONResponse({"ok": False, "error": "Unsupported file type. Upload .pdf or .txt."}, status_code=400)
    data = await file.read()
    if len(data) > MAX_CV_BYTES:
        return JSONResponse(
            {"ok": False, "error": f"File too large ({len(data)} bytes). Max ~2MB."},
            status_code=413,
        )
    try:
        if ext == ".pdf":
            text = extract_pdf_text(data)
        else:
            text = data.decode("utf-8", errors="replace")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Could not parse file: {exc}"}, status_code=400)
    text = (text or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "No readable text found in file."}, status_code=400)
    truncated = len(text) > MAX_CV_CHARS
    text = text[:MAX_CV_CHARS]
    cv_store.update({
        "filename": os.path.basename(name),
        "text": text,
        "summary": summarize_cv(text),
        "skills": extract_skills(text),
        "chars": len(text),
        "truncated": truncated,
        "uploaded_at": time.time(),
    })
    return {
        "ok": True,
        "filename": cv_store["filename"],
        "chars": cv_store["chars"],
        "truncated": truncated,
        "summary": cv_store["summary"],
        "skills": cv_store["skills"],
    }


@app.get("/api/cv")
def cv_status():
    return {
        "ok": True,
        "has_cv": bool(cv_store.get("text")),
        "filename": cv_store.get("filename"),
        "chars": cv_store.get("chars", 0),
        "truncated": cv_store.get("truncated", False),
        "summary": cv_store.get("summary", ""),
        "skills": cv_store.get("skills", []),
    }


@app.delete("/api/cv")
def cv_clear():
    reset_cv()
    interview["pending_plan"] = []
    interview["plan_meta"] = None
    return {"ok": True, "has_cv": False}


@app.post("/api/plan")
def make_question_plan(body: PlanReq):
    """Generate a CV-tailored question plan (numbered list with kinds).

    Uses Groq when a key is configured, else a deterministic local fallback.
    The plan is stored server-side; pass {"use_plan": true} to /api/start.
    """
    vals, err = _validate_setup(body.topic, body.difficulty, body.count)
    if err is not None:
        return err
    topic, difficulty, count = vals
    role = (body.role or "").strip()[:200]
    company = (body.company or "").strip()[:200]
    round_ = (body.round or "").strip()[:200]
    plan, source, cv_used = make_plan(topic, difficulty, count, role, company, round_)
    interview["pending_plan"] = plan
    interview["plan_meta"] = {
        "role": role, "company": company, "round": round_,
        "cv_used": cv_used, "source": source,
        "topic": topic, "difficulty": difficulty, "total": count,
    }
    return {
        "ok": True,
        "plan": plan,
        "cv_used": cv_used,
        "source": source,
        "role": role, "company": company, "round": round_,
        "topic": topic, "difficulty": difficulty, "total": count,
    }


@app.post("/api/start")
def start(body: StartReq):
    vals, err = _validate_setup(body.topic, body.difficulty, body.count)
    if err is not None:
        return err
    topic, difficulty, count = vals
    if not get_api_key():
        return JSONResponse({"ok": False, "error": "No Groq API key configured. Open Settings and paste a key, or set GROQ_API_KEY in .env."}, status_code=400)
    role = ((body.role or "").strip()[:200]) if isinstance(body.role, str) else ""
    company = ((body.company or "").strip()[:200]) if isinstance(body.company, str) else ""
    round_ = ((body.round or "").strip()[:200]) if isinstance(body.round, str) else ""
    use_plan = bool(body.use_plan)
    # capture any stored plan BEFORE reset (reset clears plan state)
    stored_plan = list(interview.get("pending_plan") or [])
    stored_meta = dict(interview.get("plan_meta") or {})
    # double-start resets
    reset_interview()
    planned: List[Dict[str, str]] = []
    cv_used = False
    plan_source = None
    if use_plan:
        if (len(stored_plan) == count and stored_meta.get("topic") == topic
                and stored_meta.get("difficulty") == difficulty):
            planned = stored_plan
            cv_used = bool(stored_meta.get("cv_used"))
            plan_source = stored_meta.get("source")
        else:
            # No matching stored plan: build one locally so use_plan still works.
            # (Groq may fail offline; local fallback never does.)
            planned, plan_source, cv_used = make_plan(topic, difficulty, count, role, company, round_)
    try:
        if planned:
            q1 = planned[0]["question"]
            q1kind = planned[0].get("kind")
        else:
            q1 = groq_generate_question(topic, difficulty, 1, count, [])
            q1kind = None
    except Exception as exc:
        return groq_error_response(exc)
    now = time.time()
    interview.update({
        "active": True, "topic": topic, "difficulty": difficulty, "total": count,
        "asked": [{"n": 1, "question": q1, "is_followup": False, "kind": q1kind}],
        "evaluations": [], "current": {"n": 1, "question": q1, "is_followup": False, "kind": q1kind},
        "followups_used": 0, "ready_for_report": False, "finished": False,
        "last_report": None, "started_at": now, "question_started_at": now,
        "role": role, "company": company, "round": round_,
        "cv_used": cv_used,
        "plan_queue": [{"question": p["question"], "kind": p.get("kind")} for p in planned[1:]] if planned else [],
        "pending_plan": planned if planned else [],
        "plan_meta": {"role": role, "company": company, "round": round_,
                      "cv_used": cv_used, "source": plan_source,
                      "topic": topic, "difficulty": difficulty, "total": count} if planned else None,
    })
    out = {
        "ok": True, "topic": topic, "difficulty": difficulty, "total": count,
        "current": {"n": 1, "question": q1, "is_followup": False},
        "progress": f"Q1/{count}",
        "role": role, "company": company, "round": round_,
        "cv_used": cv_used,
    }
    if planned:
        out["current"]["kind"] = q1kind
        out["plan"] = planned
        out["plan_source"] = plan_source
    return out


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
    metrics = compute_answer_metrics(ans.strip(), getattr(body, "duration_ms", None))
    record = {
        "n": cur["n"], "question": cur["question"], "answer": ans.strip()[:MAX_ANSWER],
        "score": ev["score"], "strengths": ev["strengths"], "missing": ev["missing"],
        "feedback": ev["feedback"], "is_followup": cur.get("is_followup", False),
        "metrics": metrics,
    }
    if cur.get("kind"):
        record["kind"] = cur["kind"]
    interview["evaluations"].append(record)
    done = len(interview["evaluations"]) >= interview["total"]
    if done:
        interview["current"] = None
        interview["ready_for_report"] = True
        return {"ok": True, "done": True, "evaluation": record, "progress": f"Q{len(interview['evaluations'])}/{interview['total']}"}
    # decide next: follow-up or fresh
    nxt_q, is_fu, nxt_kind = None, False, None
    if ev.get("followup") and interview["followups_used"] < MAX_FOLLOWUPS:
        nxt_q, is_fu = ev["followup"], True
        interview["followups_used"] += 1
    elif interview.get("plan_queue"):
        nxt = interview["plan_queue"].pop(0)
        nxt_q, is_fu, nxt_kind = nxt["question"], False, nxt.get("kind")
    else:
        try:
            prev = [q["question"] for q in interview["asked"]]
            nxt_q = groq_generate_question(interview["topic"], interview["difficulty"], len(interview["evaluations"]) + 1, interview["total"], prev)
        except Exception as exc:
            return groq_error_response(exc)
    nxt_n = len(interview["evaluations"]) + 1
    interview["asked"].append({"n": nxt_n, "question": nxt_q, "is_followup": is_fu, "kind": nxt_kind})
    interview["current"] = {"n": nxt_n, "question": nxt_q, "is_followup": is_fu, "kind": nxt_kind}
    interview["question_started_at"] = time.time()
    out = dict(record)
    if is_fu:
        out["followup_next"] = True
    nxt_out: Dict[str, Any] = {"n": nxt_n, "question": nxt_q, "is_followup": is_fu}
    if nxt_kind:
        nxt_out["kind"] = nxt_kind
    return {
        "ok": True, "done": False, "evaluation": out,
        "next": nxt_out,
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
        "speech_metrics": aggregate_metrics(evals),
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
        "has_cv": bool(cv_store.get("text")),
        "cv_filename": cv_store.get("filename"),
        "cv_used": interview.get("cv_used", False),
        "role": interview.get("role"),
        "company": interview.get("company"),
        "round": interview.get("round"),
        "plan_remaining": len(interview.get("plan_queue") or []),
    }
