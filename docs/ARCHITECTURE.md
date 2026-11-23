# Architecture

| Block | Code | Responsibility |
|---|---|---|
| Driver prompt | `server/app.py`, `V2Coordinator.run` | Accepts the request (≤2,000 characters) as a bounded job. |
| Prompt cleanup | `cleanup.py`, `cleanup_lexicon.json` | Before the router: tidies spacing, capitals and punctuation, and corrects misspellings ("macdolanld" → "McDonald's", "nvigate hme" → "Navigate home"). Local and deterministic: no model or online service. Corrects only towards a known vocabulary (driving commands and place types, common brands, the privacy policy's sensitive terms, the signed-in driver's saved names); common words, numbers, codes, e-mail addresses, quoted text and message wording are never changed. Everything after this block reads the cleaned prompt; the result keeps both. |
| Router | `routing_classifier.py`, `routing_contract.py`, `routing_policy.py`, `routing_pipeline.py`, `routing_profile.json` | The local model (Qwen 4B) returns complexity (`suitable` / `uncertain` / `needs_stronger_model`), a reason and the exact personal words in the prompt. The model's output is validated; Python decides; prompts that try to force a route stay local. |
| Context resolution | `context.py` | Resolves saved names ("my wife", "work"), preferences by meaning ("my usual songs") and session references ("there", "that song"). Facts stated in the request need no lookup. Missing or ambiguous references produce a clarification question. |
| Driver profiles | `profiles.py` | Sessions start as **Guest** (an empty private DB). Each named profile is one SQLite database encrypted at rest (format v2: an Argon2id password key wraps a random per-profile data key; both layers AES-256-GCM with an authenticated header; the password is not stored; v1 scrypt files migrate on sign-in). Signing in decrypts it into memory and binds it to the session; edits are re-encrypted to disk. Switching profile clears the session. Passwords need `JLR_MIN_PASSWORD` characters (default 4). 5 wrong passwords lock the profile for 60 s, doubling with each lockout up to 1 h. At most `JLR_MAX_PROFILES` profiles (default 20) and 5 new profiles per hour. Concurrent edits are written one at a time through a temporary file. |
| Private DB | `demo_store.py`, `context.EntityStore` | One per profile: profile, contacts, places with coordinates, preferences, messages, aliases. Every lookup uses the session's own store only. |
| Session store | `V2Session` in `coordinator.py` | 20 turns, 30-minute idle expiry; destination, stops, playback, call, remembered answers. Memory only. |
| Complexity × privacy decision | `privacy_policy.py`, `privacy_policy.json` | Assigns the request a sensitivity tier (P0–P3) and chooses the final route; see [PRIVACY_ROUTING.md](PRIVACY_ROUTING.md). |
| Local SLM | `ModelWorker`, `worker_prompt.py` | Turns the request into a reply or a plan of approved actions. It sees only each resolved item's type and label, never stored records or privacy flags. |
| Privacy layer | `masking.py`, `task_facts` in `coordinator.py` | Replaces private values with `<PRIVATE_n>` placeholders (whole words) before the larger model; a leak check stops the request if anything private remains; values are restored locally. The mapping never leaves the server. **Role-preserving:** a legend tells the model what each placeholder is ("your mother", "your gym", "a phone number"), from the policy's `role_descriptors` table only; sensitive roles (doctor, clinic, church…) are described generically. **Task facts:** for a comparison or plan that mentions saved places, driving times between them are computed on the server from stored coordinates and sent as rounded minutes between placeholders. `JLR_MASKING_MODE` = `plain` / `roles` / `roles_facts` (default). |
| Privacy receipt | `receipt.py`, Block details → Response | Built from each finished request: who answered and where (encrypted link or not), router location, tier and decision, why it stayed local, what reached the larger model (placeholders, roles, facts; private values sent: 0), outside services and the kind of data each received, warnings. No prompt, answer or private value. Shown at the top of the Response block's details and appended to `JLR_RECEIPT_LOG` (private file). |
| Model link security | `links.py` | Ollama receives unmasked prompts, so `JLR_OLLAMA_URL` must be loopback HTTP or HTTPS with a verified certificate (`JLR_OLLAMA_CA_FILE` for a private CA); a hosted provider's `JLR_LARGER_BASE_URL` follows the same rule. A remote plain-HTTP address stops the server at start and is refused again on every call. Remote Ollama: SSH tunnel or a TLS proxy. |
| Larger model | `ModelWorker` (Hermes 8B by default) | Same contract as the local worker, on masked text only. Its endpoint can be local or hosted (`trust = "external"` allowed). |
| Model endpoints | `models.py`, `transports.py`, `models.toml` | One file says which model does each job (router, local, larger) and where it runs. Providers: `ollama` (native API), `openai` (any OpenAI-compatible server: vLLM, TGI, LM Studio, llama.cpp, Ollama `/v1`, OpenAI) and `anthropic`. Every transport takes the same Ollama-style payload and returns the same reply shape, so callers and checks don't change. The router and local roles see unmasked text, so their endpoint must be `on_vehicle` (loopback, or a private HTTPS server that isn't a public model API); the server refuses to start otherwise. Without `models.toml` the roles come from the `.env` settings. `ollama.py` and `providers.py` remain as compatibility names. |
| Tools: maps | `tools.py` | OpenStreetMap Nominatim (place search, addresses) and OSRM (driving routes, stops). Saved places are sent as coordinates only; private and sensitive text is blocked from tool requests. |
| Validate & execute | `contract.py`, `bind_plan` / `execute` in `coordinator.py` | Approved actions only. Hard rules: negation, drafting requests, quoted instructions, refused domains. Unfamiliar wording or values not taken from the request go to driver confirmation. Calls and messages always need confirmation. Simulated adapters run the plan. |
| Response | `finish()` in `coordinator.py`, `server/static/app.js` | Reply, question, confirmation or error; refusals use fixed system wording; a privacy notice when a request was kept local. |

## Approved actions

`media.play`, `media.pause`, `media.next_track`, `navigation.start`, `navigation.add_stop`, `navigation.get_destination`, `phone.call`, `phone.end_call`, `messaging.send`, `messages.latest`, `calendar.next_appointment`, `vehicle.get_trip_state`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/`, `/app.js`, `/inspector.js`, `/profiles.js`, `/style.css` | Workspace UI |
| GET | `/api/health` | Status and configured models |
| POST | `/api/v2/sessions` | Create a session (`X-JLR-Session` header on later calls) |
| GET | `/api/v2/session` | Session state and pending result |
| POST | `/api/v2/session/reset` | Clear the session |
| POST | `/api/v2/requests` | Run a request (`{prompt}`; `Prefer: respond-async` returns a job) |
| POST | `/api/v2/requests/clarification` | Answer a question (`{request_id, slot_id, answer}`) |
| POST | `/api/v2/requests/confirmation` | Approve or decline the exact plan (`{request_id, plan_hash, approved}`) |
| POST | `/api/v2/requests/cancel` | Cancel a pending question or confirmation |
| GET / POST | `/api/v2/jobs/<id>`, `/api/v2/jobs/<id>/cancel` | Poll or cancel a job |
| GET | `/api/v2/inspector` | The session's private DB contents, profile, current session, configured models (for the UI) |
| GET | `/api/v2/profiles` | Profile names (Guest first); no private data |
| POST | `/api/v2/profile/login` | Sign the session in (`{profile_id, password}`); 403 wrong password, 429 locked |
| POST | `/api/v2/profile/guest` | Switch the session back to Guest |
| POST | `/api/v2/profiles/create` | Create a profile and sign in (`{name, password}`, password ≥4 characters) |
| POST | `/api/v2/profile/data` | Add or remove a contact, place or preference in the signed-in profile (`{op: put\|remove, kind, record\|key}`) |

Key result fields: `prompt` (as typed), `cleaned_prompt`, `prompt_cleanup` (changes), `next_route`, `privacy_routing` (tier, items, decision), `routing_notice`, `privacy` (masked words), `context`, `plan`, `confirmation_reason`, `navigation`, `api_preview`, `trace`. `simulation` is always true and `cloud_authorized` always false.
