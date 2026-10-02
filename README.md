# InterviewSim — AI Interview Simulator

## What
Practice interview game (AI evaluation for training only — NOT a real employment assessment or hiring decision). Dark interview-stage UI (crimson/charcoal). Real AI interviewer via Groq (`openai/gpt-oss-120b`), text answers with optional 🎤 voice dictation.

## Tech
Python + FastAPI backend (`app.py`), vanilla HTML/CSS/JS frontend (`static/`), Groq OpenAI-compatible chat API (`https://api.groq.com/openai/v1`), local PDF parsing via `pypdf`. No TTS, no server-side transcription — mic uses the browser's built-in SpeechRecognition only.

## Features
- Topic/difficulty/question-count setup → AI-generated questions → per-answer scoring → final report (HIRE / LEAN NO-HIRE)
- 🎤 Voice input: tap the mic button next to the answer box to dictate; transcript appends into the answer box for editing before submit (text stays primary)
- Follow-up questions (max 2, marked FOLLOW-UP), partial reports on quit, session-memory key via Settings modal

## Requirements
- Python 3.10+
- A free Groq API key ([console.groq.com/keys](https://console.groq.com/keys))
- Internet (AI calls go to Groq)

## Setup
```powershell
python -m venv .venv
# Windows: .venv\Scripts\activate | macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env   # add GROQ_API_KEY (or paste the key in Settings later)
```

## Run
```powershell
pip install -r requirements.txt
uvicorn app:app --port 8009
# open http://127.0.0.1:8009
```

## Flow (How to use)
setup (topic: JavaScript/Python/Java/C++/React/AI-ML/SQL/DSA/General · Junior/Mid/Senior · 3/5/8)
→ `POST /api/start` → Q1 → `POST /api/answer {answer}` → Groq JSON `{score, strengths, missing, feedback, followup?}`
→ follow-up asked next (marked, max 2) else fresh question → after N `POST /api/report`
→ `{per-question scores, overall, concepts_demonstrated, missing_concepts, study_topics, stronger_answer_example, verdict HIRE/LEAN NO-HIRE}`.
Quit midway (`POST /api/quit`) → partial report. Double-start resets.

## API
- `GET /api/status`, `GET /api/key/status`, `POST /api/key` (verified via `models.list`, session memory), `DELETE /api/key`
- `POST /api/start`, `POST /api/answer`, `POST /api/report`, `POST /api/quit`, `GET /api/session`

## Key (Groq key)
Settings modal → session memory, else `.env` `GROQ_API_KEY`. Status pill shows readiness. Friendly errors for Groq 401/429/404/503.

## Voice input
- How it works: mic button uses the browser's built-in `webkitSpeechRecognition || SpeechRecognition` (`lang: en-US`, interim results shown live in the answer box). Tap 🎤 to start (pulsing ⏺ + "Listening…"), tap again or pause to stop — the final transcript is **appended** to existing text (capped at 2000 chars), then you edit and hit Submit manually. Frontend-only: no new dependencies, no extra API key, no cost, nothing sent to any server beyond the normal typed-answer flow.
- Browser support: Chrome / Edge (desktop + Android) — full support. Safari: partial (newer versions). Firefox: **not supported** (no SpeechRecognition API) — the button shows "Voice input not supported… please type instead."
- Errors handled gracefully: mic denied ("allow mic access…"), no microphone, no-speech/empty result ("try again"), network/service unavailable, aborted — partial text is always kept and text input keeps working.

## Limitations
- Scores/verdicts (HIRE / LEAN NO-HIRE) are AI practice-game feedback for training only — NOT a real employment assessment or hiring decision.
- Voice dictation needs Chrome/Edge + microphone permission + network (browser cloud speech service); accuracy varies with accent/noise, so always review the transcript before submitting. No server-side transcription or TTS.

## Troubleshooting
- `No Groq API key configured` → open Settings and paste a key, or set `GROQ_API_KEY` in `.env`.
- `Groq rejected the API key (401)` → re-copy the key from console.groq.com/keys.
- `Groq rate limit hit (429)` → wait ~30s and retry.
- `Groq model not found (404)` → the configured model may be renamed; check `/api/status`.
- `Groq is temporarily unavailable (5xx)` → retry in a bit.
- Voice button says unsupported → use Chrome/Edge; Firefox has no SpeechRecognition API, type instead.

## Tests
```powershell
pip install -r requirements-test.txt
pytest -q
```
`tests/test_api.py` covers validation + no-AI transitions only; AI paths covered live.
