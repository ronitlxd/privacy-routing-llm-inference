"""How the app talks to a model server: one function shape for every provider.

    transport(path, payload, timeout) -> Ollama-style reply
        {"response": <json text>, "done_reason": "stop" | "length", "prompt_eval_count", "eval_count", "total_duration", ...}

The router and the workers build an Ollama /api/generate payload (model, system, prompt, JSON schema in
"format", options). The ollama transport sends it as it is; the openai and anthropic transports translate
it, so every caller and every check is the same whatever serves the model.

  ollama     native /api/generate: JSON schema enforced, GPU layers, keep_alive, Ollama's own timings
  openai     any OpenAI-compatible /chat/completions server (vLLM, TGI, LM Studio, llama.cpp, Ollama /v1, OpenAI):
             asks for json_schema output; if the server refuses that (HTTP 400/422) it falls back to
             json_object mode with the schema in the instructions
  anthropic  Messages API, schema in the instructions

Every reply is still validated by the caller (validate_assessment / validate_worker).
All transports: the address is checked before each call (loopback or HTTPS only), certificate checks
are always on (ca_file adds a private authority), machine proxy settings are ignored so prompts go only
to the configured server, and an HTTP error reports the status code only (error bodies can echo prompts).
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

SCHEMA_STATUS = (400, 422)   # what servers answer when they do not support json_schema output


class ModelHTTPError(OSError):
    def __init__(self, label, status):
        super().__init__(f'{label} returned HTTP {status}')
        self.status = status


def schema_instruction(system, schema):
    return (system + '\n\nReply with one JSON object only (no prose, no code fences) that matches this JSON Schema:\n'
            + json.dumps(schema, separators=(',', ':')))


def json_text(text):
    """The JSON object in a reply, tolerating code fences or a sentence around it."""
    text = (text or '').strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.S)
    if fenced: text = fenced.group(1)
    if not text.startswith('{'):
        start, end = text.find('{'), text.rfind('}')
        if start >= 0 and end > start: text = text[start:end + 1]
    return text


def ssl_context(ca_file=''):
    return ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()


def _open(request, timeout, context):
    """The one place a model request leaves the process (tests replace it)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    return opener.open(request, timeout=timeout)


def post_json(url, body, headers, timeout, context, label):
    """POST (or GET when body is None) JSON and return the decoded reply."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json', **headers})
    try:
        with _open(request, timeout, context) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ModelHTTPError(label, exc.code) from None


def _label(endpoint):
    return f'Model server "{endpoint.name}" ({endpoint.provider} at {endpoint.host})'


def ollama(endpoint):
    context = ssl_context(endpoint.ca_file)

    def call(path, payload=None, timeout=240):
        endpoint.check_link()
        key = endpoint.api_key()
        headers = {'Authorization': 'Bearer ' + key} if key else {}
        return post_json(endpoint.url + path, payload, headers, timeout, context, _label(endpoint))
    return call


def openai(endpoint):
    context = ssl_context(endpoint.ca_file)
    state = {'json_schema': True}
    lock = threading.Lock()

    def call(path, payload, timeout=240):
        started = time.perf_counter()
        endpoint.check_link()
        key = endpoint.api_key()
        headers = {'Authorization': 'Bearer ' + key} if key else {}
        options = payload.get('options') or {}
        schema, system = payload.get('format'), payload.get('system', '')
        body = {'model': payload['model'], 'max_tokens': options.get('num_predict', 512), 'temperature': options.get('temperature', 0),
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': payload.get('prompt', '')}]}
        reply = None
        if schema and state['json_schema']:
            try:
                reply = post_json(endpoint.url + '/chat/completions',
                                  {**body, 'response_format': {'type': 'json_schema', 'json_schema': {'name': 'reply', 'schema': schema}}},
                                  headers, timeout, context, _label(endpoint))
            except ModelHTTPError as exc:
                if exc.status not in SCHEMA_STATUS: raise
                with lock: state['json_schema'] = False   # this server has no json_schema mode: use json_object from now on
        if reply is None:
            if schema:
                body['messages'][0]['content'] = schema_instruction(system, schema)
                body['response_format'] = {'type': 'json_object'}
            remaining = max(0.1, timeout - (time.perf_counter() - started))
            reply = post_json(endpoint.url + '/chat/completions', body, headers, remaining, context, _label(endpoint))
        choice = (reply.get('choices') or [{}])[0]
        usage = reply.get('usage') or {}
        text = (choice.get('message') or {}).get('content')
        return {'response': json_text(text) if schema else (text or ''),
                'done_reason': 'length' if choice.get('finish_reason') == 'length' else 'stop',
                'prompt_eval_count': usage.get('prompt_tokens'), 'eval_count': usage.get('completion_tokens'),
                'total_duration': int((time.perf_counter() - started) * 1e9)}
    call.json_schema_state = state
    return call


def anthropic(endpoint):
    context = ssl_context(endpoint.ca_file)

    def call(path, payload, timeout=240):
        started = time.perf_counter()
        endpoint.check_link()
        options = payload.get('options') or {}
        schema, system = payload.get('format'), payload.get('system', '')
        body = {'model': payload['model'], 'max_tokens': options.get('num_predict', 512),
                'system': schema_instruction(system, schema) if schema else system,
                'messages': [{'role': 'user', 'content': payload.get('prompt', '')}]}
        headers = {'x-api-key': endpoint.api_key(), 'anthropic-version': '2023-06-01'}
        reply = post_json(endpoint.url + '/messages', body, headers, timeout, context, _label(endpoint))
        text = ''.join(part.get('text', '') for part in reply.get('content', []) if part.get('type') == 'text')
        usage = reply.get('usage') or {}
        return {'response': json_text(text) if schema else text, 'done_reason': 'length' if reply.get('stop_reason') == 'max_tokens' else 'stop',
                'prompt_eval_count': usage.get('input_tokens'), 'eval_count': usage.get('output_tokens'),
                'total_duration': int((time.perf_counter() - started) * 1e9)}
    return call


FACTORIES = {'ollama': ollama, 'openai': openai, 'anthropic': anthropic}
_transports = {}
_lock = threading.Lock()


def for_endpoint(endpoint):
    """One transport per endpoint (it keeps per-server state, such as json_schema support)."""
    with _lock:
        if endpoint not in _transports:
            call = FACTORIES[endpoint.provider](endpoint)
            call.provider, call.endpoint = endpoint.provider, endpoint
            _transports[endpoint] = call
        return _transports[endpoint]
