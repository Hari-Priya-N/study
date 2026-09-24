"""
Adaptive Syllabus Learning App
===============================
Upload a course syllabus (PDF) -> the app extracts topics -> the learner
takes a short diagnostic MCQ quiz on every topic -> a personalised learning
path is built (weakest topics first, material pitched at the learner's
level) -> for each topic: read material, take a quiz; score < 60% sends the
learner back to the material for that topic, >= 60% marks it mastered.

Run with:  streamlit run app.py
"""

import os

import streamlit as st
from dotenv import load_dotenv

import db
import llm_client
import pdf_parser

load_dotenv()

st.set_page_config(page_title="Adaptive Syllabus Learning", page_icon="📚", layout="centered")

PASS_THRESHOLD = db.MASTERY_THRESHOLD  # 0.60
INITIAL_QUIZ_QUESTIONS = 5
PRACTICE_QUIZ_QUESTIONS = 5


# --------------------------------------------------------------- helpers --

@st.cache_resource
def get_conn():
    conn = db.get_connection()
    db.init_db(conn)
    return conn


def friendly_error(exc: Exception) -> None:
    if isinstance(exc, llm_client.LLMConfigError):
        st.error(str(exc))
    elif isinstance(exc, llm_client.LLMResponseError):
        st.error(
            "The model's response couldn't be understood, which can happen "
            "occasionally. Please try again."
        )
        with st.expander("Details"):
            st.code(str(exc))
    else:
        st.error(f"Something went wrong: {exc}")


def status_badge(status: str) -> str:
    return {
        "not_started": "⚪ Not started",
        "assessed": "🟡 Assessed — ready to learn",
        "learning": "🟡 Learning",
        "needs_review": "🟠 Needs review",
        "mastered": "🟢 Mastered",
    }.get(status, status)


def level_badge(level: str | None) -> str:
    if not level:
        return "—"
    return {"beginner": "🌱 Beginner", "intermediate": "🌿 Intermediate", "advanced": "🌳 Advanced"}.get(
        level, level
    )


def reset_topic_runtime_state(topic_id: int) -> None:
    """Clear in-session quiz/material scratch state for one topic."""
    for key in list(st.session_state.keys()):
        if key.endswith(f"_{topic_id}") or f"_{topic_id}_" in key:
            del st.session_state[key]


# ------------------------------------------------------------ quiz widget --

def render_quiz_form(conn, topic: dict, phase: str, level: str, form_key: str):
    """Generates (once, cached in session_state) and renders an MCQ quiz.
    Returns True if the quiz was just submitted this run (result already
    recorded), else False."""
    questions_key = f"questions_{form_key}_{topic['id']}"
    result_key = f"result_{form_key}_{topic['id']}"
    asked_key = f"asked_{topic['id']}"

    if questions_key not in st.session_state:
        avoid = st.session_state.get(asked_key, [])
        num_q = INITIAL_QUIZ_QUESTIONS if phase == "initial_assessment" else PRACTICE_QUIZ_QUESTIONS
        try:
            with st.spinner("Generating quiz questions..."):
                questions = llm_client.generate_quiz(
                    topic["name"], topic["description"], level, num_questions=num_q, avoid_questions=avoid
                )
        except Exception as exc:
            friendly_error(exc)
            if st.button("Retry generating quiz", key=f"retry_{form_key}_{topic['id']}"):
                st.rerun()
            return False
        st.session_state[questions_key] = questions
        st.session_state.setdefault(asked_key, [])
        st.session_state[asked_key].extend(q["question"] for q in questions)

    questions = st.session_state[questions_key]

    if result_key in st.session_state:
        return True  # already submitted; caller will render the result

    with st.form(key=f"form_{form_key}_{topic['id']}"):
        answers = {}
        for i, q in enumerate(questions):
            st.markdown(f"**{i + 1}. {q['question']}**")
            answers[i] = st.radio(
                "Select one:",
                options=list(range(4)),
                format_func=lambda idx, opts=q["options"]: opts[idx],
                key=f"radio_{form_key}_{topic['id']}_{i}",
                index=None,
                label_visibility="collapsed",
            )
            st.write("")
        submitted = st.form_submit_button("Submit Quiz", type="primary")

    if submitted:
        unanswered = [i for i, v in answers.items() if v is None]
        if unanswered:
            st.warning("Please answer every question before submitting.")
            return False

        correct_count = sum(
            1 for i, q in enumerate(questions) if answers[i] == q["correct_index"]
        )
        total = len(questions)
        db.record_quiz_attempt(conn, topic["id"], phase, correct_count, total)
        st.session_state[result_key] = {
            "correct": correct_count,
            "total": total,
            "score": correct_count / total,
            "answers": answers,
            "questions": questions,
        }
        return True

    return False


def render_quiz_result(topic: dict, form_key: str) -> dict:
    result_key = f"result_{form_key}_{topic['id']}"
    result = st.session_state[result_key]
    pct = result["score"] * 100
    passed = result["score"] >= PASS_THRESHOLD

    if passed:
        st.success(f"Score: {result['correct']}/{result['total']} ({pct:.0f}%) — nice work!")
    else:
        st.error(
            f"Score: {result['correct']}/{result['total']} ({pct:.0f}%) — below the "
            f"{int(PASS_THRESHOLD * 100)}% pass mark. Let's revisit this topic."
        )

    with st.expander("Review answers"):
        for i, q in enumerate(result["questions"]):
            chosen = result["answers"][i]
            correct = q["correct_index"]
            mark = "✅" if chosen == correct else "❌"
            st.markdown(f"{mark} **{i + 1}. {q['question']}**")
            st.markdown(f"- Your answer: {q['options'][chosen]}")
            if chosen != correct:
                st.markdown(f"- Correct answer: {q['options'][correct]}")
            if q.get("explanation"):
                st.caption(q["explanation"])

    return result


# ---------------------------------------------------------------- stages --

def render_upload_stage():
    st.title("📚 Adaptive Syllabus Learning")
    st.write(
        "Upload a course syllabus PDF. The app will identify the topics, "
        "test your starting knowledge of each one, then walk you through "
        "a learning path tailored to what you already know."
    )

    if not os.environ.get("GOOGLE_API_KEY"):
        st.warning("No GOOGLE_API_KEY found. Set it in your .env file.")

    uploaded = st.file_uploader("Syllabus PDF", type=["pdf"])
    course_name = st.text_input("Course name (optional)", placeholder="e.g. Introduction to Statistics")

    if uploaded is not None and st.button("Upload & analyze syllabus", type="primary"):
        try:
            with st.spinner("Reading PDF..."):
                text = pdf_parser.extract_text_from_pdf(uploaded.getvalue())
        except Exception as exc:
            friendly_error(exc)
            return
        conn = get_conn()
        name = course_name.strip() or uploaded.name.rsplit(".", 1)[0]
        course_id = db.create_course(conn, name, uploaded.name, text)
        st.session_state.course_id = course_id
        st.rerun()

    conn = get_conn()
    existing = db.list_courses(conn)
    if existing:
        st.divider()
        st.subheader("Or resume a previous course")
        for c in existing:
            cols = st.columns([4, 2, 1])
            cols[0].write(f"**{c['name']}**")
            cols[1].caption(c["phase"])
            if cols[2].button("Open", key=f"open_{c['id']}"):
                st.session_state.course_id = c["id"]
                st.rerun()


def render_extract_topics_stage(conn, course):
    st.title(course["name"])
    st.info("Syllabus uploaded. Next, the app extracts the list of topics to learn.")
    if st.button("Extract topics from syllabus", type="primary"):
        try:
            with st.spinner("Reading syllabus and identifying topics..."):
                topics = llm_client.extract_topics(course["raw_text"])
        except Exception as exc:
            friendly_error(exc)
            return
        for i, t in enumerate(topics):
            db.add_topic(conn, course["id"], i, t["name"], t["description"])
        db.set_course_phase(conn, course["id"], "assessing")
        st.rerun()


def render_assessing_stage(conn, course):
    topics = db.get_topics(conn, course["id"])
    pending = [t for t in topics if t["status"] == "not_started"]

    st.title(course["name"])
    done = len(topics) - len(pending)
    st.progress(done / len(topics) if topics else 0, text=f"Initial assessment: {done}/{len(topics)} topics")

    if not pending:
        db.reorder_topics_by_score(conn, course["id"])
        db.set_course_phase(conn, course["id"], "learning")
        st.rerun()
        return

    topic = pending[0]
    st.subheader(f"Diagnostic quiz: {topic['name']}")
    st.caption(topic["description"])
    st.write(
        "Answer these questions as best you can — this just finds your "
        "starting point for this topic, it isn't graded."
    )

    submitted = render_quiz_form(conn, topic, phase="initial_assessment", level="intermediate", form_key="iq")
    result_key = f"result_iq_{topic['id']}"
    if result_key in st.session_state:
        result = render_quiz_result(topic, form_key="iq")
        level = db.score_to_level(result["score"])
        st.write(f"Starting level for this topic: **{level_badge(level)}**")
        if st.button("Next topic ➜", type="primary"):
            db.update_topic(conn, topic["id"], status="assessed", level=level)
            reset_topic_runtime_state(topic["id"])
            st.rerun()


def render_learning_path_overview(conn, course, topics):
    st.title(course["name"])
    st.caption("Your personalised learning path — weakest topics first.")

    mastered = sum(1 for t in topics if t["status"] == "mastered")
    st.progress(mastered / len(topics) if topics else 0, text=f"{mastered}/{len(topics)} topics mastered")

    next_topic = next((t for t in topics if t["status"] != "mastered"), None)

    for t in topics:
        cols = st.columns([4, 2, 2, 2])
        cols[0].markdown(f"**{t['order_index'] + 1}. {t['name']}**")
        cols[1].write(status_badge(t["status"]))
        cols[2].write(level_badge(t["level"]))
        score_txt = f"{t['best_score'] * 100:.0f}%" if t["best_score"] is not None else "—"
        cols[3].write(score_txt)

    st.divider()
    if next_topic:
        label = "Continue learning" if next_topic["status"] == "needs_review" else "Start next topic"
        if st.button(f"{label}: {next_topic['name']} ➜", type="primary"):
            st.session_state.active_topic_id = next_topic["id"]
            st.rerun()
    else:
        st.success("🎉 All topics mastered! Great work.")

    with st.expander("Jump to a specific topic"):
        for t in topics:
            if st.button(f"{t['name']} ({status_badge(t['status'])})", key=f"jump_{t['id']}"):
                st.session_state.active_topic_id = t["id"]
                st.rerun()


def render_topic_stage(conn, course, topic):
    st.title(topic["name"])
    st.caption(topic["description"])
    st.write(f"Level: {level_badge(topic['level'])} · Status: {status_badge(topic['status'])}")

    if st.button("⟵ Back to learning path"):
        st.session_state.active_topic_id = None
        st.rerun()

    mode_key = f"mode_{topic['id']}"
    mode = st.session_state.get(mode_key, "material")

    # ---- material ----
    if mode == "material":
        level = topic["level"] or "intermediate"
        material = db.get_cached_material(conn, topic["id"], level)
        if material is None:
            try:
                with st.spinner("Preparing study material..."):
                    material = llm_client.generate_material(
                        topic["name"], topic["description"], level, course_context=course["name"]
                    )
                db.cache_material(conn, topic["id"], level, material)
            except Exception as exc:
                friendly_error(exc)
                return
        st.markdown(material)
        st.divider()
        if st.button("I've read this — take the quiz ➜", type="primary"):
            st.session_state[mode_key] = "quiz"
            st.rerun()
        return

    # ---- quiz ----
    if mode == "quiz":
        level = topic["level"] or "intermediate"
        submitted = render_quiz_form(conn, topic, phase="practice", level=level, form_key="pq")
        result_key = f"result_pq_{topic['id']}"
        if result_key in st.session_state:
            result = render_quiz_result(topic, form_key="pq")
            passed = result["score"] >= PASS_THRESHOLD
            processed_key = f"processed_pq_{topic['id']}"
            if submitted and not st.session_state.get(processed_key, False):
                db.update_topic(
                    conn,
                    topic["id"],
                    status="mastered" if passed else "needs_review",
                    increment_attempts=True,
                )
                st.session_state[processed_key] = True
            if passed:
                if st.button("Continue to learning path ➜", type="primary"):
                    for k in (mode_key, result_key, f"questions_pq_{topic['id']}", processed_key):
                        st.session_state.pop(k, None)
                    st.session_state.active_topic_id = None
                    st.rerun()
            else:
                st.warning("Let's go over this topic again before retrying.")
                if st.button("Study this topic again", type="primary"):
                    for k in (mode_key, result_key, f"questions_pq_{topic['id']}", processed_key):
                        st.session_state.pop(k, None)
                    st.session_state[mode_key] = "material"
                    st.rerun()
        return


def render_sidebar(conn, course):
    with st.sidebar:
        st.subheader(course["name"])
        st.caption(f"Phase: {course['phase']}")
        if st.button("⟵ Switch course"):
            st.session_state.course_id = None
            st.session_state.active_topic_id = None
            st.rerun()


# ------------------------------------------------------------------ main --

def main():
    st.session_state.setdefault("course_id", None)
    st.session_state.setdefault("active_topic_id", None)

    conn = get_conn()

    if not st.session_state.course_id:
        render_upload_stage()
        return

    course = db.get_course(conn, st.session_state.course_id)
    if course is None:
        st.session_state.course_id = None
        st.rerun()
        return

    render_sidebar(conn, course)

    if course["phase"] == "new":
        render_extract_topics_stage(conn, course)
    elif course["phase"] == "assessing":
        render_assessing_stage(conn, course)
    else:  # 'learning' or 'done'
        topics = db.get_topics(conn, course["id"])
        active_id = st.session_state.get("active_topic_id")
        active_topic = db.get_topic(conn, active_id) if active_id else None
        if active_topic:
            render_topic_stage(conn, course, active_topic)
        else:
            render_learning_path_overview(conn, course, topics)


if __name__ == "__main__":
    main()
