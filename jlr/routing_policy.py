"""Pure two-destination Router v2 policy."""
from __future__ import annotations


POLICY_VERSION = "routing-only-v2"

REASONS = {
    "local_suitable": "A routine request fits the configured local profile.",
    "local_uncertain": "Local suitability is uncertain, so the request remains local for clarification or handling.",
    "local_policy_override": "Server policy requires local handling; the stronger-model recommendation was not followed.",
    "stronger_model": "The request requires reasoning beyond the configured local profile and is routed to privacy review.",
}


def decide(assessment, *, local_only=False):
    suitability = assessment["local_suitability"]
    if local_only:
        code = "local_policy_override" if suitability == "needs_stronger_model" else (
            "local_uncertain" if suitability == "uncertain" else "local_suitable")
        return _decision("local_slm", code, overridden=suitability == "needs_stronger_model")
    if suitability == "needs_stronger_model":
        return _decision("privacy_layer", "stronger_model")
    if suitability == "uncertain":
        return _decision("local_slm", "local_uncertain")
    return _decision("local_slm", "local_suitable")


def routing_control_attempt(prompt):
    """User text cannot authorize or force a route."""
    import re
    return bool(re.search(
        r"\b(?:ignore|bypass|override|change)\b.{0,50}\b(?:routing|route|policy)\b|"
        r"\b(?:choose|route|send)\b.{0,35}\b(?:cloud|privacy_layer|local_slm)\b",
        prompt, re.I))


def _decision(route, code, *, overridden=False):
    return {
        "routing_status": "selected",
        "next_route": route,
        "reason_code": code,
        "reason": REASONS[code],
        "policy_override": overridden,
        "cloud_authorized": False,
        "execution_ready": False,
        "downstream_invoked": False,
    }
