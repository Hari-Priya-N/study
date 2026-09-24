"""
Thin wrapper around the Google Gemini API for the three pieces of generated
content this app needs:

  1. extract_topics   -- turn raw syllabus text into a structured topic list
  2. generate_material -- an explanation of one topic, pitched at a level
  3. generate_quiz     -- multiple-choice questions for one topic/level

extract_topics and generate_quiz ask Gemini for JSON via response_mime_type
="application/json" (native JSON mode), and still parse defensively as a
safety net in case a response comes back malformed or wrapped in prose.
"""

import json
import os
import re

from google import genai
from google.genai import types

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# Cap how much raw syllabus text we send per call — keeps cost/latency sane
# for long PDFs while still giving the model plenty of context.
MAX_SYLLABUS_CHARS = 15000


class LLMConfigError(RuntimeError):
    """Raised when no API key is configured."""


class LLMResponseError(RuntimeError):
    """Raised when the model's response can't be parsed as the expected JSON."""


_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "GOOGLE_API_KEY is not set. Get one for free at "
            "https://aistudio.google.com/apikey and add it to your "
            "environment or a .env file (see .env.example) before "
            "generating content."
        )
    _client = genai.Client(api_key=api_key)
    return _client


def _extract_json(text: str):
    """Pull a JSON value out of a model response that may include stray
    prose or ```json fences around the actual payload."""
    text = (text or "").strip()
    # Strip a fenced code block if present.
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    candidate = fence_match.group(1) if fence_match else text

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to grabbing the first balanced {...} or [...] block.
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMResponseError(f"Could not parse JSON from model response:\n{text[:500]}")


def _call(
    system: str,
    user: str,
    max_tokens: int = 2000,
    temperature: float = 0.4,
    json_mode: bool = False,
) -> str:
    client = get_client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json" if json_mode else "text/plain",
    )
    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=user,
        config=config,
    )
    text = response.text
    if not text:
        raise LLMResponseError(
            "The model returned an empty response (it may have been blocked "
            "by a safety filter). Please try again."
        )
    return text


# ------------------------------------------------------------ topic list --

def extract_topics(syllabus_text: str) -> list[dict]:
    """Return a list of {"name": str, "description": str} extracted from a
    syllabus, in the order they should be taught."""
    system = (
        "You are an expert curriculum designer. You read course syllabi and "
        "extract a clean, ordered list of the distinct topics/units a "
        "student needs to learn. Respond with ONLY valid JSON — a list of "
        "objects, each with a 'name' (short topic title, max ~6 words) and "
        "a 'description' (one sentence on what the topic covers). Aim for "
        "6-15 topics: merge trivial sub-points, split out topics that are "
        "clearly substantial. No markdown, no commentary, JSON only."
    )
    user = (
        "Here is the syllabus text. Extract the ordered topic list as "
        "described.\n\n---\n\n" + syllabus_text[:MAX_SYLLABUS_CHARS]
    )
    raw = _call(system, user, max_tokens=2000, temperature=0.2, json_mode=True)
    data = _extract_json(raw)

    if not isinstance(data, list) or not data:
        raise LLMResponseError("Model did not return a non-empty topic list.")

    topics = []
    for item in data:
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if name:
            topics.append({"name": name, "description": description})

    if not topics:
        raise LLMResponseError("Model returned a topic list with no usable entries.")
    return topics


# ------------------------------------------------------------- material --

_LEVEL_GUIDANCE = {
    "beginner": (
        "Assume no prior knowledge of this specific topic. Define terms "
        "before using them, use simple concrete examples, and keep jargon "
        "to a minimum."
    ),
    "intermediate": (
        "Assume the learner knows the basics already. Focus on how the "
        "concept works, common pitfalls, and how it connects to related "
        "ideas. Light use of precise terminology is fine."
    ),
    "advanced": (
        "Assume solid foundational knowledge. Go into nuance, edge cases, "
        "and deeper 'why', and reference how this topic connects to more "
        "advanced applications. Don't re-explain basics."
    ),
}


def generate_material(
    topic_name: str, topic_description: str, level: str, course_context: str = ""
) -> str:
    """Return markdown study material for one topic, pitched at `level`."""
    guidance = _LEVEL_GUIDANCE.get(level, _LEVEL_GUIDANCE["intermediate"])
    system = (
        "You are a patient, clear subject-matter tutor writing self-contained "
        "study material for one topic in a course. Write in markdown: a short "
        "intro, then the core explanation with headers/sub-points as useful, "
        "then a brief worked example, then a 2-3 bullet summary titled "
        "'Key takeaways'. Keep it focused and readable in 2-4 minutes — do "
        "not pad it out. No preamble like 'Sure, here is...'."
    )
    user = (
        f"Course context: {course_context[:500] or 'N/A'}\n"
        f"Topic: {topic_name}\n"
        f"Topic description: {topic_description}\n"
        f"Target learner level: {level}. {guidance}\n\n"
        "Write the study material now."
    )
    return _call(system, user, max_tokens=1500, temperature=0.5, json_mode=False).strip()


# ------------------------------------------------------------------ quiz --

def generate_quiz(
    topic_name: str,
    topic_description: str,
    level: str,
    num_questions: int = 5,
    avoid_questions: list[str] | None = None,
) -> list[dict]:
    """Return a list of MCQ dicts:
       {"question": str, "options": [str, str, str, str],
        "correct_index": int, "explanation": str}
    """
    guidance = _LEVEL_GUIDANCE.get(level, _LEVEL_GUIDANCE["intermediate"])
    avoid_note = ""
    if avoid_questions:
        joined = "\n".join(f"- {q}" for q in avoid_questions[:15])
        avoid_note = (
            "\nDo NOT repeat these previously-used questions (write new ones "
            f"testing the same topic differently):\n{joined}\n"
        )

    system = (
        "You write multiple-choice quiz questions to assess understanding of "
        "one topic. Respond with ONLY valid JSON: a list of objects, each "
        "with 'question' (str), 'options' (list of exactly 4 strings), "
        "'correct_index' (int, 0-3, index into options), and 'explanation' "
        "(one sentence on why the correct answer is right). Exactly one "
        "option must be correct; the other three should be plausible "
        "distractors, not silly. No markdown, no commentary, JSON only."
    )
    user = (
        f"Topic: {topic_name}\n"
        f"Topic description: {topic_description}\n"
        f"Difficulty/level to write questions at: {level}. {guidance}\n"
        f"Number of questions: {num_questions}\n"
        f"{avoid_note}\n"
        "Write the quiz now."
    )
    raw = _call(system, user, max_tokens=2200, temperature=0.7, json_mode=True)
    data = _extract_json(raw)

    if not isinstance(data, list) or not data:
        raise LLMResponseError("Model did not return a non-empty question list.")

    questions = []
    for item in data:
        options = item.get("options")
        correct_index = item.get("correct_index")
        question = str(item.get("question", "")).strip()
        if (
            question
            and isinstance(options, list)
            and len(options) == 4
            and isinstance(correct_index, int)
            and 0 <= correct_index < 4
        ):
            questions.append(
                {
                    "question": question,
                    "options": [str(o) for o in options],
                    "correct_index": correct_index,
                    "explanation": str(item.get("explanation", "")).strip(),
                }
            )

    if not questions:
        raise LLMResponseError("Model returned a quiz with no valid, well-formed questions.")
    return questions
