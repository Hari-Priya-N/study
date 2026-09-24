"""
Modified LLM Client using Google Gemini (Free Tier)
"""

import json
import os
import re
import google.generativeai as genai

# Gemini 1.5 Flash is fast, free (within rate limits), and great at JSON
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

MAX_SYLLABUS_CHARS = 15000

class LLMConfigError(RuntimeError):
    """Raised when no API key is configured."""

class LLMResponseError(RuntimeError):
    """Raised when the model's response can't be parsed as the expected JSON."""

def _get_model(system_instruction: str):
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "GOOGLE_API_KEY is not set. Get one for free at https://aistudio.google.com/"
        )
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=DEFAULT_MODEL,
        system_instruction=system_instruction
    )

def _extract_json(text: str):
    text = text.strip()
    # Strip markdown fences
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    candidate = fence_match.group(1) if fence_match else text

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMResponseError(f"Could not parse JSON from model response:\n{text[:500]}")

def _call(system: str, user: str, temperature: float = 0.4) -> str:
    model = _get_model(system)
    # Gemini uses a generation_config for temperature
    response = model.generate_content(
        user,
        generation_config=genai.types.GenerationConfig(
            temperature=temperature,
        )
    )
    return response.text

# --- The functions below remain logically identical to your original code ---

def extract_topics(syllabus_text: str) -> list[dict]:
    system = (
        "You are an expert curriculum designer. Respond with ONLY valid JSON — a list of "
        "objects, each with a 'name' (short title) and a 'description' (one sentence). "
        "No markdown, no commentary, JSON only."
    )
    user = f"Extract the ordered topic list from this syllabus:\n\n{syllabus_text[:MAX_SYLLABUS_CHARS]}"
    raw = _call(system, user, temperature=0.2)
    data = _extract_json(raw)
    
    if not isinstance(data, list) or not data:
        raise LLMResponseError("Model did not return a valid topic list.")
    return [{"name": str(item.get("name", "")), "description": str(item.get("description", ""))} for item in data if item.get("name")]

_LEVEL_GUIDANCE = {
    "beginner": "Assume no prior knowledge. Define terms, use simple examples.",
    "intermediate": "Assume basic knowledge. Focus on how concepts connect.",
    "advanced": "Assume foundational knowledge. Focus on nuance and edge cases."
}

def generate_material(topic_name: str, topic_description: str, level: str, course_context: str = "") -> str:
    guidance = _LEVEL_GUIDANCE.get(level, _LEVEL_GUIDANCE["intermediate"])
    system = (
        "You are a patient tutor. Write study material in Markdown: intro, core explanation, "
        "a worked example, and 'Key takeaways'. No preamble."
    )
    user = (
        f"Course: {course_context}\nTopic: {topic_name}\nDescription: {topic_description}\n"
        f"Level: {level}. {guidance}"
    )
    return _call(system, user, temperature=0.5).strip()

def generate_quiz(topic_name: str, topic_description: str, level: str, num_questions: int = 5, avoid_questions: list[str] | None = None) -> list[dict]:
    guidance = _LEVEL_GUIDANCE.get(level, _LEVEL_GUIDANCE["intermediate"])
    avoid_note = f"\nDo NOT repeat: {avoid_questions}" if avoid_questions else ""
    system = (
        "Respond with ONLY valid JSON: a list of objects with 'question' (str), "
        "'options' (list of 4 strings), 'correct_index' (int 0-3), and 'explanation' (str)."
    )
    user = (
        f"Topic: {topic_name}\nLevel: {level}. {guidance}\nQuestions: {num_questions}{avoid_note}"
    )
    raw = _call(system, user, temperature=0.7)
    return _extract_json(raw)