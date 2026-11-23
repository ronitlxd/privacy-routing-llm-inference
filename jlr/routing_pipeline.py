"""Prompt -> local assessment -> Python policy -> two-destination route. Stops there."""
from __future__ import annotations

import os
import time
import uuid
from functools import lru_cache

from .routing_classifier import RoutingBusy, RoutingClassifier
from .routing_contract import InvalidAssessment
from .routing_policy import POLICY_VERSION, REASONS, decide, routing_control_attempt


@lru_cache(maxsize=4)
def get_routing_classifier(profile_path="", num_gpu=-1, threads=4, deadline_seconds=12, role=None):
    return RoutingClassifier(profile_path or None, num_gpu=num_gpu, threads=threads,
                             deadline_seconds=deadline_seconds, role=role)


def analyze_routing_only(prompt, classifier, *, local_only=False):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    assessment = None
    detail = {}
    try:
        phase = time.perf_counter()
        model_output, detail = classifier.classify(prompt)
        references = model_output["references"]
        assessment = {key: model_output[key] for key in ("local_suitability", "reason_code")}
        inference_ms = round((time.perf_counter() - phase) * 1000, 3)
        phase = time.perf_counter()
        decision = decide(assessment, local_only=local_only)
        control_attempt = routing_control_attempt(prompt)
        if control_attempt:
            decision.update(next_route="local_slm", reason_code="local_policy_override",
                            reason=REASONS["local_policy_override"], policy_override=True)
        policy_ms = round((time.perf_counter() - phase) * 1000, 3)
        total_ms = round((time.perf_counter() - started) * 1000, 3)
        trace = _trace(prompt, classifier, references, assessment, detail, decision,
                       local_only=local_only, control_attempt=control_attempt)
        return {
            "schema_version": "routing-only-response-v2",
            "request_id": request_id,
            "routing_mode": "routing_only",
            "routing_status": decision["routing_status"],
            "next_route": decision["next_route"],
            "references": references,
            "assessment": assessment,
            "reason_code": decision["reason_code"],
            "reason": decision["reason"],
            "policy_override": decision["policy_override"],
            "policy_version": POLICY_VERSION,
            "capability_profile_version": classifier.profile["version"],
            "profile_hash": classifier.profile_hash,
            "cloud_authorized": False,
            "execution_ready": False,
            "downstream_invoked": False,
            "timing_ms": {"inference": inference_ms, "policy": policy_ms, "total": total_ms},
            "model_timing": detail.get("timing", {}),
            "trace_version": "routing-only-trace-v2",
            "trace": trace,
        }
    except RoutingBusy as exc:
        return _error(request_id, started, classifier, prompt, "router_busy", str(exc), assessment)
    except TimeoutError:
        return _error(request_id, started, classifier, prompt, "routing_timeout", "Local routing classifier timed out.", assessment)
    except OSError:
        return _error(request_id, started, classifier, prompt, "model_unavailable", "Local routing classifier is unavailable.", assessment)
    except (InvalidAssessment, TypeError, KeyError, ValueError) as exc:
        return _error(request_id, started, classifier, prompt, "invalid_assessment", "Routing assessment failed validation: " + str(exc), assessment)


def _trace(prompt, classifier, references, assessment, detail, decision, *, local_only, control_attempt):
    return [
        {"id": "driver_prompt", "label": "Driver Prompt", "status": "complete",
         "input": {"prompt": prompt}, "output": {"normalized_prompt": prompt}},
        {"id": "local_classifier", "label": "Local Routing Classifier", "status": "complete",
         "input": {"prompt": prompt, "profile_version": classifier.profile["version"]},
         "output": {"classifier_path": detail.get("path", "slm"), "model": detail.get("model"),
                    "references": references, "assessment": assessment}},
        {"id": "routing_profile", "label": "Routing Profile", "status": "complete",
         "input": {"assessment": assessment},
         "output": {"version": classifier.profile["version"], "hash": classifier.profile_hash,
                    "policy_status": classifier.profile["policy_status"],
                    "reference_rules": classifier.profile["reference_rules"],
                    "suitability_rules": classifier.profile["suitability_rules"]}},
        {"id": "routing_logic", "label": "Routing Logic", "status": "selected",
         "input": {"assessment": assessment, "local_only": local_only,
                   "routing_control_attempt": control_attempt},
         "output": decision},
    ]


def _error(request_id, started, classifier, prompt, code, reason, assessment):
    decision = {"routing_status": "error", "next_route": None, "reason_code": code,
                "reason": reason, "policy_override": False, "cloud_authorized": False,
                "execution_ready": False, "downstream_invoked": False}
    return {
        "schema_version": "routing-only-response-v2", "request_id": request_id,
        "routing_mode": "routing_only", "references": [], "assessment": assessment, **decision,
        "policy_version": POLICY_VERSION, "capability_profile_version": classifier.profile["version"],
        "profile_hash": classifier.profile_hash,
        "timing_ms": {"total": round((time.perf_counter() - started) * 1000, 3)},
        "model_timing": {}, "trace_version": "routing-only-trace-v2",
        "trace": [{"id": "driver_prompt", "label": "Driver Prompt", "status": "complete",
                   "input": {"prompt": prompt}, "output": {"normalized_prompt": prompt}},
                  {"id": "local_classifier", "label": "Local Routing Classifier", "status": "blocked",
                   "input": {}, "output": {"reason_code": code, "reason": reason}},
                  {"id": "routing_profile", "label": "Routing Profile", "status": "not_reached",
                   "input": {}, "output": {"version": classifier.profile["version"]}},
                  {"id": "routing_logic", "label": "Routing Logic", "status": "blocked",
                   "input": {}, "output": decision}],
    }


def configured_classifier():
    from .models import role
    return get_routing_classifier(os.environ.get("JLR_ROUTING_PROFILE", ""),
                                  deadline_seconds=int(os.environ.get("JLR_ROUTING_DEADLINE", "12")), role=role("router"))
