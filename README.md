# InterviewSim — AI Interview Simulator

Dark interview-stage UI (crimson/charcoal). Real AI interviewer via Groq (`openai/gpt-oss-120b`), text answers only (no voice — limitation).

## Run
```powershell
pip install -r requirements.txt
uvicorn app:app --port 8009
# open http://127.0.0.1:8009
```

## Flow
setup (topic: JavaScript/Python/Java/C++/React/AI-ML/SQL/DSA/General · Junior/Mid/Senior · 3/5/8)
→ `POST /api/start` → Q1 → `POST /api/answer {answer}` → Groq JSON `{score, strengths, missing, feedback, followup?}`
→ follow-up asked next (marked, max 2) else fresh question → after N `POST /api/report`
→ `{per-question scores, overall, concepts_demonstrated, missing_concepts, study_topics, stronger_answer_example, verdict HIRE/LEAN NO-HIRE}`.
Quit midway (`POST /api/quit`) → partial report. Double-start resets.

## API
- `GET /api/status`, `GET /api/key/status`, `POST /api/key` (verified via `models.list`, session memory), `DELETE /api/key`
- `POST /api/start`, `POST /api/answer`, `POST /api/report`, `POST /api/quit`, `GET /api/session`

## Key
Settings modal → session memory, else `.env` `GROQ_API_KEY`. Status pill shows readiness. Friendly errors for Groq 401/429/404/503.

## Tests
```powershell
pip install -r requirements-test.txt
pytest -q
```
`tests/test_api.py` covers validation + no-AI transitions only; AI paths covered live.
