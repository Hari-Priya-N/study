# Adaptive Syllabus Learning

Upload a course syllabus (PDF) and get a personalized, adaptive learning
flow: the app extracts the topics, diagnostically quizzes you on each one,
then walks you through study material and quizzes tailored to your level —
looping you back to re-study any topic you score below 60% on.

## How it works

1. **Upload** — you upload a syllabus PDF. Its text is extracted with `pypdf`.
2. **Topic extraction** — an LLM call (Claude) reads the syllabus text and
   returns a structured, ordered list of topics with short descriptions.
3. **Initial assessment** — for every topic, you take a short 5-question
   diagnostic MCQ quiz (not tied to a level yet). Your score maps to a
   starting level:
   - `< 40%` → beginner
   - `40–75%` → intermediate
   - `≥ 75%` → advanced
4. **Learning path** — topics are reordered so the ones you scored weakest
   on come first. Each topic shows its status, level, and best score.
5. **Study loop, per topic**:
   - Read LLM-generated study material pitched at your assessed level for
     that topic.
   - Take a 5-question practice quiz at that level.
   - **Score ≥ 60%** → topic marked *mastered*, move to the next one.
   - **Score < 60%** → topic marked *needs review*, you're sent back to the
     material for that same topic and quizzed again (with new questions).
6. Repeat until every topic is mastered.

All progress (topics, levels, scores, attempt counts, quiz history) is
persisted in a local SQLite database (`adaptive_learning.db`), so you can
close the app and resume a course later from the upload screen's "resume a
previous course" list.

## Project layout

```
adaptive-learning/
├── app.py            Streamlit UI and the stage-by-stage flow
├── db.py             SQLite schema + persistence helpers
├── pdf_parser.py      PDF text extraction
├── llm_client.py      Anthropic API calls: extract_topics, generate_material, generate_quiz
├── requirements.txt
└── .env.example
```

## Setup

1. Create and activate a virtual environment, then install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Set your Anthropic API key. Either export it directly:

   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

   or copy `.env.example` to `.env` and fill it in (the app loads `.env`
   automatically via `python-dotenv`):

   ```bash
   cp .env.example .env
   # then edit .env
   ```

   Get a key from https://console.anthropic.com/.

3. Run the app:

   ```bash
   streamlit run app.py
   ```

   It opens in your browser, typically at http://localhost:8501.

## Notes & tuning

- **Model**: defaults to `claude-sonnet-4-5-20250929`; override with the
  `ANTHROPIC_MODEL` environment variable if you want a different model.
- **Pass threshold**: 60%, defined as `db.MASTERY_THRESHOLD`. Change it
  there if you want a stricter or looser bar.
- **Questions per quiz**: 5 for both the diagnostic and practice quizzes —
  see `INITIAL_QUIZ_QUESTIONS` / `PRACTICE_QUIZ_QUESTIONS` at the top of
  `app.py`.
- **Repeat questions**: each retake asks the model to avoid repeating
  previously-seen questions for that topic (tracked in-session), so
  re-quizzes aren't just memorized answers.
- **Scanned PDFs**: `pdf_parser.py` extracts embedded text only — a
  syllabus that's a scanned image with no text layer won't work. Run it
  through OCR first (e.g. `ocrmypdf`) if needed.
- **Cost**: every topic extraction, quiz, and material generation is a
  live API call. A 10-topic syllabus taken through once is roughly
  1 (topic extraction) + 10 (diagnostic quizzes) + 10 (material) + 10+
  (practice quizzes, more if topics need re-study) calls.

## Extending it

- **Multiple learners**: the schema is single-learner per course today.
  Add a `users` table and a `user_id` column on `topics`/`quiz_attempts`
  to support multiple learners per course.
- **Editable topic list**: currently the extracted topics are used as-is;
  you could add a review/edit step after extraction before assessment
  starts.
- **Question bank caching**: quizzes are generated fresh via the API each
  time. If cost/latency matters more than freshness, cache a pool of
  questions per topic+level in `material_cache`-style table and sample
  from it instead.
