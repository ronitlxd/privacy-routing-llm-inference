"""Strict Router v2 model contract and local reference-span validation."""
from __future__ import annotations
import re


LOCAL_SUITABILITY = ("suitable", "needs_stronger_model", "uncertain")
REASON_CODES = ("routine_request", "complex_reasoning", "unclear_request", "outside_profile")
REFERENCE_TYPES = (
    "person", "contact_detail", "location", "preference", "personal_detail",
    "session_reference", "other_private",
)
CONTEXT_SOURCES = ("none", "private_db", "session", "uncertain")


class InvalidAssessment(ValueError):
    pass


REFERENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 200},
        "type": {"type": "string", "enum": list(REFERENCE_TYPES)},
        "context_source": {"type": "string", "enum": list(CONTEXT_SOURCES)},
    },
    "required": ["text", "type", "context_source"],
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "references": {"type": "array", "items": REFERENCE_SCHEMA, "maxItems": 16},
        "local_suitability": {"type": "string", "enum": list(LOCAL_SUITABILITY)},
        "reason_code": {"type": "string", "enum": list(REASON_CODES)},
    },
    "required": ["references", "local_suitability", "reason_code"],
}


def validate_assessment(value, prompt):
    """Validate exact model fields and derive zero-based Unicode code-point spans."""
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        raise InvalidAssessment("Assessment must contain exactly references, local_suitability, and reason_code")
    if value["local_suitability"] not in LOCAL_SUITABILITY:
        raise InvalidAssessment("Invalid local_suitability")
    if value["reason_code"] not in REASON_CODES:
        raise InvalidAssessment("Invalid reason_code")
    if not isinstance(value["references"], list) or len(value["references"]) > 16:
        raise InvalidAssessment("references must be an array of at most 16 items")

    unique = []
    seen = set()
    for reference in value["references"]:
        if not isinstance(reference, dict) or set(reference) != set(REFERENCE_SCHEMA["required"]):
            raise InvalidAssessment("Each reference must contain exactly text, type, and context_source")
        text = reference["text"]
        ref_type = reference["type"]
        source = reference["context_source"]
        if not isinstance(text, str) or not text or len(text) > 200:
            raise InvalidAssessment("Reference text must contain 1-200 characters")
        if ref_type not in REFERENCE_TYPES:
            raise InvalidAssessment("Invalid reference type")
        if source not in CONTEXT_SOURCES:
            raise InvalidAssessment("Invalid context_source")
        key = (text, ref_type, source)
        if key not in seen:
            seen.add(key)
            unique.append(key)

    located = []
    for text, ref_type, source in unique:
        if _excluded_public_parameter(text, source):
            continue
        # The words must come from the prompt. A capitalisation-only difference is accepted and
        # replaced by the prompt's own text; added, removed or changed words are still rejected.
        matches = list(re.finditer(re.escape(text), prompt)) or list(re.finditer(re.escape(text), prompt, re.I))
        if not matches:
            raise InvalidAssessment(f"Reference text is not an exact prompt substring: {text!r}")
        for match in matches:
            located_text, located_start, located_end = _expand_session_phrase(
                prompt, match.group(), match.start(), match.end(), source)
            located.append({
                "text": located_text,
                "start": located_start,
                "end": located_end,
                "type": ref_type,
                "context_source": source,
            })
    located.sort(key=lambda item: (item["start"], item["end"], item["type"], item["context_source"]))
    return {
        "references": located,
        "local_suitability": value["local_suitability"],
        "reason_code": value["reason_code"],
    }


def _excluded_public_parameter(text, source):
    """Remove explicit time and public live-data parameters that require no private/session lookup."""
    if source != "none":
        return False
    normalized = text.strip().lower()
    temporal = re.search(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|"
        r"tonight|morning|afternoon|evening)\b", normalized)
    public_live_data = re.fullmatch(r"(?:current|live|today(?:'s)?)\s+(?:traffic|weather)", normalized)
    return bool(temporal or public_live_data)


def _expand_session_phrase(prompt, text, start, end, source):
    """Restore the governing noun when a model returns only a session-relative clause."""
    if source != "session" or not re.match(
            r"^(?:we|i|you)\s+(?:discussed|mentioned|chose|selected|reviewed)\b", text, re.I):
        return text, start, end
    prefix_start = max(0, start - 80)
    prefix = prompt[prefix_start:start]
    match = re.search(r"\b(?:the|those|these)\s+(?:[\w'-]+\s+){1,4}$", prefix, re.I)
    if not match:
        return text, start, end
    expanded_start = prefix_start + match.start()
    return prompt[expanded_start:end], expanded_start, end
