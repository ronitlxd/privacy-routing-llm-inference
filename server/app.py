"""JLR V2 HTTP server: static workspace UI plus the /api/v2 endpoints.

Standard library only. Listens on loopback by default; reach it through an SSH
tunnel. On a network address it serves HTTPS only (JLR_TLS_CERT and JLR_TLS_KEY).
"""
import ipaddress
import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Settings come from the environment, then from .env (written by `python jlr_setup.py`).
from jlr.settings import load_env  # noqa: E402
load_env(os.environ.get("JLR_ENV_FILE") or REPO / ".env")
# A relative receipt-log path is relative to the repo, wherever the server is started from.
if os.environ.get("JLR_RECEIPT_LOG") and not Path(os.environ["JLR_RECEIPT_LOG"]).is_absolute():
    os.environ["JLR_RECEIPT_LOG"] = str(REPO / os.environ["JLR_RECEIPT_LOG"])

MAX_PROMPT, MAX_BODY = 2000, 16384
STATIC = {"/": ("index.html", "text/html"), "/inspector": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/inspector.js": ("inspector.js", "text/javascript"), "/profiles.js": ("profiles.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/driver": ("driver.html", "text/html"), "/driver.js": ("driver.js", "text/javascript"), "/driver.css": ("driver.css", "text/css"),
          "/vendor/leaflet.js": ("vendor/leaflet.js", "text/javascript"), "/vendor/leaflet.css": ("vendor/leaflet.css", "text/css")}
# The driver screen can be the home page (JLR_HOME_PAGE=driver); the inspector stays at /inspector.
if os.environ.get("JLR_HOME_PAGE") == "driver":
    STATIC["/"] = STATIC["/driver"]
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-src https://www.youtube-nocookie.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Handler(BaseHTTPRequestHandler):
    # Each read or write on a connection must make progress within 30 s, so a client that connects and then sends
    # nothing cannot hold a server thread forever (the site may be reachable from a network). It limits waiting on
    # the socket only: a request that takes longer to answer (a slow model) is not cut off.
    timeout = float(os.environ.get("JLR_CONNECTION_TIMEOUT", "30"))

    def log_message(self, *args):
        pass

    def reply(self, code, body, content_type="application/json; charset=utf-8", cache="no-store"):

        data = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, dict) else body

        self.send_response(code); self.send_header("Content-Type", content_type)

        self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", cache)

        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")

        self.send_header("Content-Security-Policy", CSP)

        self.end_headers(); self.wfile.write(data)

    def same_origin(self):
        """The page and the request come from this site: the Origin header (if any) names this server's own address.
        Either scheme is accepted, because an HTTPS proxy in front of the site (Caddy, nginx, tailscale serve) turns
        the browser's https:// into plain http:// here. Another site (a different host or port) is always refused."""
        origin, host = self.headers.get("Origin"), self.headers.get("Host", "")
        return not origin or (bool(host) and origin in (f"http://{host}", f"https://{host}"))

    def do_GET(self):
        if self.path.startswith("/tiles/"):
            # Map tiles for the driver screen, fetched by this server (see jlr/tiles.py).
            from jlr import tiles
            try:
                return self.reply(200, tiles.get(*tiles.parse(self.path)), "image/png", cache="private, max-age=86400")
            except tiles.TileError as exc:
                return self.reply(404, {"error": str(exc)})
            except Exception:
                return self.reply(502, {"error": "Map tiles are unavailable"})

        if self.path.startswith("/api/v2/jobs/"):

            from jlr.jobs import JOBS, JobError

            try:

                return self.reply(200, JOBS.get(self.path.rsplit("/", 1)[-1], self.headers.get("X-JLR-Session")))

            except JobError as exc:

                return self.reply(exc.code, {"error": str(exc)})

        if self.path == "/api/v2/inspector":
            from jlr.coordinator import get_coordinator
            import copy
            sid = self.headers.get("X-JLR-Session")
            if not sid:
                return self.reply(400, {"error": "X-JLR-Session is required."})
            c = get_coordinator()
            try:
                session = c.sessions.get(sid)
            except Exception:
                return self.reply(404, {"error": "Session expired or does not exist."})
            with session.lock:
                snapshot = copy.deepcopy({
                    "session_id": sid, "retained_turn_limit": 20, "idle_expiry_minutes": 30,
                    "turns": session.turns, "application_state": session.state,
                    "references": session.references,
                    "clarified_references": [{"type": k[0], "phrase": k[1], "value": v}
                                             for k,v in session.clarified_references.items()],
                    "pending_request": session.pending.get("result") if session.pending else None})
            with session.store.connect() as db:
                database = {table: [dict(row) for row in db.execute("SELECT * FROM " + table)]
                            for table in ("profile", "contacts", "places", "preferences", "messages", "entity_aliases", "v2_metadata")}
            return self.reply(200, {"database": database, "session": snapshot, "profile": session.profile,
                                   "models": {"local_worker": c.local_worker.model, "larger_worker": c.larger_worker.model},
                                   "sample_data": True})

        if self.path == "/api/v2/profiles":
            from jlr.coordinator import get_coordinator
            registry = get_coordinator().sessions.profiles
            from jlr.profiles import min_password
            return self.reply(200, {"enabled": registry is not None, "profiles": registry.list() if registry else [],
                                   "min_password": min_password()})

        if self.path.startswith("/api/v2/session"):

            from jlr.coordinator import get_coordinator

            sid = self.headers.get("X-JLR-Session")

            if not sid:

                return self.reply(400, {"error": "X-JLR-Session is required."})

            try:

                session = get_coordinator().sessions.get(sid)

                return self.reply(200, {"schema_version": "jlr-end-to-end-v2", "session_id": sid,

                                       "turns": session.turns, "state": session.state, "profile": session.profile,

                                       "pending": bool(session.pending), "pending_result": session.pending.get("result") if session.pending else None, "simulation": True})

            except Exception as exc:

                return self.reply(404, {"error": str(exc)})

        if self.path == "/api/health":
            return self.reply(200, {"status": "ok", "v2_available": True, "v2_schema": "jlr-end-to-end-v2",
                                   "v2_contract_revision": 2, "simulation": True,
                                   "map_provider": "OpenStreetMap (Nominatim/Photon + OSRM)",
                                   **model_summary(), "models_ready": dict(MODELS_READY)})
        if self.path not in STATIC:
            return self.reply(404, {"error": "Not found"})
        name, mime = STATIC[self.path]
        return self.reply(200, (ROOT / "static" / name).read_bytes(), mime + "; charset=utf-8")

    def read_json(self):

        if not self.same_origin(): raise PermissionError("Cross-origin requests are not allowed.")

        if self.headers.get_content_type() != "application/json": raise TypeError("Use application/json.")

        length = int(self.headers.get("Content-Length", "0"))

        if not 0 < length <= MAX_BODY: raise OverflowError("Request is empty or too large.")

        return json.loads(self.rfile.read(length))

    def do_POST(self):
        if self.path == "/api/v2/speech":
            return self.speech()
        if self.path.startswith("/api/v2/"):
            return self.v2()
        return self.reply(404, {"error": "Not found"})

    def speech(self):
        """Voice input: one recorded clip in, its text out. Transcribed on this server; nothing is kept."""
        from jlr import speech
        from jlr.coordinator import V2Error, get_coordinator
        if not self.same_origin(): return self.reply(403, {"error": "Cross-origin requests are not allowed."})
        try: get_coordinator().sessions.get(self.headers.get("X-JLR-Session") or "")
        except V2Error as exc: return self.reply(404, {"error": str(exc)})
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not 0 < length <= speech.MAX_AUDIO_BYTES: return self.reply(413, {"error": "The recording is empty or too long."})
        audio = self.rfile.read(length)
        try: return self.reply(200, speech.transcribe(audio, self.headers.get_content_type()))
        except speech.SpeechUnavailable as exc: return self.reply(503, {"error": str(exc)})
        except speech.SpeechError as exc: return self.reply(400, {"error": str(exc)})
        except Exception: return self.reply(500, {"error": "Voice input failed."})

    def v2(self):

        from jlr.coordinator import V2Error, get_coordinator

        from jlr.jobs import JOBS, JobError

        try:

            payload = self.read_json()

            if not isinstance(payload, dict): raise ValueError("Provide a JSON object.")

            c = get_coordinator()

            if self.path == "/api/v2/sessions":

                session = c.sessions.create()

                return self.reply(201, {"schema_version": "jlr-end-to-end-v2", "contract_revision": 2,

                    "session_id": session.session_id, "simulation": True, "expires_in_seconds": 1800})

            sid = self.headers.get("X-JLR-Session") or payload.get("session_id")

            if not isinstance(sid, str): raise ValueError("session_id is required")

            try: session = c.sessions.get(sid)

            except V2Error as exc: return self.reply(404, {"error": str(exc)})

            if self.path.startswith("/api/v2/profile"):
                return self.profile_request(c, sid, session, payload)

            if self.path == "/api/v2/media/lookup":
                # Only called after the driver taps "Play on YouTube" (the text is shown first).
                from jlr import media
                try: found = media.lookup(payload.get("query"))
                except media.MediaError as exc: return self.reply(400, {"error": str(exc)})
                from jlr import receipt as privacy_receipt
                try: privacy_receipt.log({"request_id": None, "status": "media_lookup"},
                                         {"outside_services": [{"service": found["service"], "received": found["received"]}]})
                except OSError: pass
                return self.reply(200, found)

            if self.path == "/api/v2/session/reset":

                JOBS.cancel_session(sid); c.sessions.reset(sid)

                return self.reply(200, {"session_id": sid, "status": "reset"})

            if self.path.startswith("/api/v2/jobs/") and self.path.endswith("/cancel"):

                return self.reply(202, JOBS.cancel(self.path.split("/")[-2], sid))

            if self.path == "/api/v2/requests/cancel":

                JOBS.cancel_session(sid)

                return self.reply(200, c.cancel(sid, payload.get("request_id")))


            key = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")

            if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128): raise ValueError("Invalid idempotency key")

            if self.path == "/api/v2/requests":

                prompt = payload.get("prompt")

                if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= MAX_PROMPT: raise ValueError("Enter a prompt of 1–2,000 characters")

                fn = lambda cancel: c.run(sid, prompt.strip(), idempotency_key=key, cancel_event=cancel)

            elif self.path == "/api/v2/requests/clarification":

                answer = payload.get("answer")

                if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= MAX_PROMPT: raise ValueError("Enter an answer of 1–2,000 characters")

                fn = lambda cancel: c.clarify(sid, answer.strip(), payload.get("request_id"), payload.get("slot_id"), cancel)

            elif self.path == "/api/v2/requests/confirmation":

                if not isinstance(payload.get("approved"), bool): raise ValueError("approved must be boolean")

                fn = lambda cancel: c.confirm(sid, payload.get("request_id"), payload.get("plan_hash"), payload['approved'], cancel)

            else: return self.reply(404, {"error": "Unknown V2 endpoint"})

            job = JOBS.submit(sid, self.path, payload, fn, key)

            if payload.get('async') is True or self.headers.get('Prefer') == 'respond-async': return self.reply(202, job)

            # Synchronous callers share the same bounded scheduler.

            while job['status'] in {'queued', 'running', 'cancelling'}:

                time.sleep(.1); job = JOBS.get(job['job_id'], sid)

            return self.reply(200 if job['status'] == 'completed' else 409, job.get('result', {'error': job.get('error', job['status'])}))

        except PermissionError as exc: return self.reply(403, {"error": str(exc)})

        except OverflowError as exc: return self.reply(413, {"error": str(exc)})

        except TypeError as exc: return self.reply(415, {"error": str(exc)})

        except JobError as exc: return self.reply(exc.code, {"error": str(exc)})

        except (ValueError, UnicodeError) as exc: return self.reply(400, {"error": str(exc)})

        except Exception: return self.reply(503, {"error": "V2 temporarily unavailable"})

    def profile_request(self, c, sid, session, payload):
        '''Switch, create or edit the driver profile bound to this session.'''
        from jlr.jobs import JOBS
        from jlr.profiles import ProfileError
        registry = c.sessions.profiles
        if registry is None: return self.reply(404, {"error": "Profiles are not enabled on this server."})
        try:
            if self.path in {"/api/v2/profile/login", "/api/v2/profiles/create", "/api/v2/profile/guest"}:
                if self.path == "/api/v2/profile/guest": store = c.sessions.store
                elif self.path == "/api/v2/profiles/create": store = registry.create(payload.get("name"), payload.get("password"))
                else:
                    if not isinstance(payload.get("profile_id"), str): raise ProfileError("Choose a profile.")
                    store = registry.open(payload["profile_id"], payload.get("password"))
                JOBS.cancel_session(sid)
                session = c.sessions.switch(sid, store)
                return self.reply(200, {"session_id": sid, "profile": session.profile})
            if self.path == "/api/v2/profile/data":
                if not session.profile.get("protected"): raise ProfileError("Sign in to a profile to save private data. The guest has none.", 403)
                kind = payload.get("kind"); op = payload.get("op")
                if op == "remove":
                    key = payload.get("key")
                    if kind not in {"contact", "place", "preference"} or not isinstance(key, str) or not key.strip(): raise ProfileError("Choose an entry to remove.")
                    session.store.remove(kind, key)
                elif op == "put":
                    session.store.put(kind, valid_record(kind, payload.get("record")))
                else: raise ProfileError("Unknown operation.")
                return self.reply(200, {"status": "saved", "profile": session.profile})
            return self.reply(404, {"error": "Unknown profile endpoint"})
        except ProfileError as exc: return self.reply(exc.code, {"error": str(exc)})


def valid_record(kind, record):
    '''Checks one private-data entry typed in the UI.'''
    from jlr.profiles import ProfileError
    if not isinstance(record, dict): raise ProfileError("Fill in the entry.")
    def text(name, low, high, label):
        value = record.get(name, "")
        if not isinstance(value, str) or not low <= len(value.strip()) <= high: raise ProfileError(f"{label}: use {low}–{high} characters.")
        return value.strip()
    if kind == "contact":
        return {"alias": text("alias", 1, 40, "What you call them"), "label": text("label", 1, 80, "Name"), "email": text("email", 0, 120, "Email or phone")}
    if kind == "place":
        out = {"alias": text("alias", 1, 40, "Place name"), "label": text("label", 1, 80, "Label"), "address": text("address", 0, 200, "Address")}
        try:
            lat, lon = float(record.get("latitude")), float(record.get("longitude"))
        except (TypeError, ValueError): raise ProfileError("Enter latitude and longitude as numbers.") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ProfileError("Latitude or longitude is out of range.")
        return dict(out, latitude=lat, longitude=lon)
    if kind == "preference":
        if record.get("key") not in {"usual_playlist", "usual_drink"}: raise ProfileError("Choose a preference.")
        return {"key": record["key"], "value": text("value", 1, 80, "Value")}
    raise ProfileError("Unknown entry type.")


MODELS_READY = {}


def model_summary():
    """Which model does each job, where it runs and how far it is trusted (from models.toml or .env)."""
    from jlr import models
    try: config = models.config()
    except ValueError as exc:   # without the server's folders (health is readable by anyone who can open the site)
        return {"models": {"error": str(exc).replace(str(REPO) + os.sep, "").replace(str(Path.home()), "~")}}
    roles = config.roles
    return {"v2_local_model": roles["local"].model, "v2_larger_model": roles["larger"].model,
            "v2_larger_provider": roles["larger"].endpoint.provider, "models": config.describe()}


def warm_up_models():
    """Load each Ollama model once at start, so the first request does not pay a cold load.

    Each model is loaded with the same runner options the request path uses (a different num_gpu or
    num_ctx would make Ollama reload it) and kept resident (keep_alive -1). Models on another kind of
    server (OpenAI-compatible, Anthropic) are not preloaded: they are marked "remote".
    """
    from jlr import models
    MODELS_READY.clear()
    seen = set()
    for role in models.config().roles.values():
        if (role.endpoint, role.model) in seen: continue
        seen.add((role.endpoint, role.model))
        if role.endpoint.provider != "ollama" or not role.warmup:
            MODELS_READY[role.model] = "remote: loaded by its server" if role.endpoint.provider != "ollama" else "not preloaded"
            continue
        MODELS_READY[role.model] = "loading"
        try:
            role.transport("/api/generate", {"model": role.model, "prompt": "", "keep_alive": -1, "stream": False,
                                             "options": role.options(1)}, timeout=900)
            MODELS_READY[role.model] = "ready"
        except Exception as exc:
            MODELS_READY[role.model] = "failed: " + type(exc).__name__
    from jlr import speech
    if speech.available():
        try: speech.load(); MODELS_READY["speech"] = "ready"
        except Exception as exc: MODELS_READY["speech"] = "failed: " + type(exc).__name__


def is_loopback_host(host):
    if host == "localhost": return True
    try: return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError: return False


def tls_context(environ=os.environ):
    """HTTPS settings for the site, or None for plain HTTP. Relative paths are relative to the repo."""
    cert, key = environ.get("JLR_TLS_CERT", "").strip(), environ.get("JLR_TLS_KEY", "").strip()
    if not cert and not key: return None
    if not (cert and key): raise ValueError("Set both JLR_TLS_CERT and JLR_TLS_KEY (certificate and private key), or neither.")
    cert, key = (str(p if Path(p).is_absolute() else REPO / p) for p in (cert, key))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try: context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as exc: raise ValueError(f"Cannot load the HTTPS certificate {cert} / key {key}: {exc}") from None
    return context


def listen_settings(environ=os.environ):
    """(host, port, tls context). Plain HTTP is only allowed on a loopback address: anything the network can
    reach is served over HTTPS, so profile passwords and private data never cross it unencrypted."""
    host, port = environ.get("HOST", "127.0.0.1"), int(environ.get("PORT", "8770"))
    context = tls_context(environ)
    if context is None and not is_loopback_host(host):
        raise ValueError(f"HOST={host} lets other computers reach the site, which needs HTTPS: set JLR_TLS_CERT and "
                         "JLR_TLS_KEY (or run ./install.sh --access). For this computer only, use HOST=127.0.0.1.")
    return host, port, context


class TLSServer(ThreadingHTTPServer):
    """ThreadingHTTPServer over TLS. The handshake runs in the request's own thread, so a slow or refusing
    client (e.g. one that does not trust a self-signed certificate) never blocks other connections."""
    def __init__(self, address, handler, context):
        super().__init__(address, handler)
        self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def finish_request(self, request, client_address):
        try:
            request.settimeout(15); request.do_handshake(); request.settimeout(None)
        except (ssl.SSLError, OSError):
            return
        super().finish_request(request, client_address)


if __name__ == "__main__":
    import threading
    from jlr import models
    try:
        host, port, context = listen_settings()
    except ValueError as exc:
        print(f"JLR V2 not started: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        # models.toml (or .env): unmasked prompts only go to on_vehicle endpoints, never over plain HTTP to another
        # machine, API keys are present, and the router model matches its routing profile. Refuse to start otherwise.
        checked = models.check(models.config(), routing_profile=None)
    except ValueError as exc:
        print(f"JLR V2 not started: {exc}", file=sys.stderr)
        sys.exit(2)
    for warning in checked.warnings: print(f"JLR V2 warning: {warning}", file=sys.stderr)
    for name, role in checked.roles.items():
        where, _ = role.endpoint.where()
        print(f"  {name:<6} {role.model} via {role.endpoint.provider} at {where} ({role.endpoint.trust})")
    if os.environ.get("JLR_WARMUP", "1") != "0":
        threading.Thread(target=warm_up_models, name="model-warm-up", daemon=True).start()
    if context:
        print(f"JLR V2 listening on https://{host}:{port}/")
        TLSServer((host, port), Handler, context).serve_forever()
    else:
        print(f"JLR V2 listening on http://{host}:{port}/")
        ThreadingHTTPServer((host, port), Handler).serve_forever()
