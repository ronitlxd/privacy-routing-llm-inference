# Privacy-aware routing: sensitivity tiers × complexity

**Version:** `privacy-policy-v3` · October 2026

## Why

Routing originally used complexity alone, and privacy only decided what to mask afterwards. Now **every route decision uses both**: how much reasoning the request needs, and how sensitive its data is.

## Sensitivity tiers

| Tier | Name | Meaning | Examples | Can masking protect it? |
|---|---|---|---|---|
| P0 | Public | No personal data | "What is cholesterol?", "nearest Starbucks", "compare three insurance plans" | Not needed |
| P1 | Personal | Identifies the driver's life; low harm when masked | "my wife", "work", "my usual songs", "there" | Yes (placeholders) |
| P2 | Sensitive | Harmful if exposed | health, finances, legal matters about the driver or someone they know; emails, phone numbers | Identifiers yes; **health, finance and legal content no**, because the content itself is what the model must reason about |
| P3 | Restricted | Must never leave the device | password/PIN with a value, card numbers (checksum), government ID (checksum), bank account numbers | No |

A request's tier is the **highest** tier among its detected items.

## Decision (complexity × privacy)

| Router complexity | P0 | P1 | P2 maskable | P2 content | P3 |
|---|---|---|---|---|---|
| Simple / uncertain | Local | Local | Local | Local | Local |
| Needs stronger model | Larger model (unmasked) | Larger model, **masked** | Larger model, **masked** | **Kept local** + notice | **Kept local** + notice |

Reason codes: `local_suitable`, `local_uncertain`, `local_policy_override`, `stronger_public`, `stronger_masked`, `sensitive_kept_local`, `restricted_kept_local`.

Principle: **privacy dominates capability** once data cannot be masked without losing its meaning. The driver is told when a request was kept local for privacy ("Kept on the local model for privacy: this request contains health information…").

## How sensitivity is detected (four detectors, highest tier wins)

1. **Patterns**, most restrictive first:
   - credentials with a real value ("PIN is 4821", not "password is too long")
   - card and government-ID numbers that pass the Luhn checksum
   - bank account numbers
   - emails
   - phone numbers in phone shapes only (a run of years such as "2019 2020" is not a phone number)
   - street addresses: number + capitalised name + street type

   The privacy layer masks exactly these pattern matches, so detection and masking use one definition.
2. **Sensitive-topic lexicon** (health, finance, legal). A term counts only when its sentence is about the driver or someone they know (I/me/my/we/our…, lowercase "us", or a person the router flagged). So "What is cholesterol?" and "Compare US mortgage rates" stay P0, while "My cholesterol is high" is P2.
3. **Router references**: personal words flagged by the router model, mapped by type (person/location/preference → P1, contact detail → P2).
4. **Context Resolution**: saved names and session references resolved from the Private DB or session → P1.

Router and context phrases that contain a stricter item (e.g. "My PIN is 4821") are absorbed into it, so a restricted value is never echoed through a lower-tier item.

## Where it runs

```
Driver Prompt → Prompt Cleanup (spelling; local, no model)
             → Router (complexity + personal words)
             → Context Resolution (saved data, session)
             → Complexity × Privacy decision   ← NEW: final route chosen here
             → Local SLM  |  Privacy Layer → Larger Model
```

The decision runs **after** Context Resolution so it knows both complexity and every resolved personal item. It is re-evaluated after each clarification answer.

## Enforcement

- **Kept-local requests** never reach the larger model.
- **Restricted values and unmaskable sensitive content** are added to the outbound block list, so they can't be sent to web or map tools either.
- **Results:** restricted values are hidden (`[hidden credential]`); card numbers show only their last 4 digits.
- **Per-driver isolation:** each driver profile has its own private database, encrypted at rest with its password; the guest has none. A session can only resolve the signed-in driver's data, and switching driver clears the session.
- **Cleanup helps detection:** misspelled sensitive words ("cholestrol", "blod presure") are corrected before the privacy tier is assessed; credentials, numbers and message text are never altered.
- **Unchanged protections still apply:** masking with whole-word placeholders, the leak check, saved places sent as coordinates only, and local-only restoration.

## Masking without losing context (policy v3)

Plain placeholders hide *who* and *where*, but also *what*: "visit `<PRIVATE_1>`, go to the `<PRIVATE_3>`" no longer says it's your mother and your gym, and the model has no way to compare travel orders. Two additions keep the meaning while sending no new private data:

1. **Role legend.** The larger model receives `placeholders: {"<PRIVATE_1>": "your mother", "<PRIVATE_3>": "your gym"}`.
   - Descriptions come only from `role_descriptors` in `privacy_policy.json`, never from saved values, so a name, address or email can't appear in them.
   - Names get "a person you know".
   - Roles that reveal sensitive context (therapist, doctor, clinic, church, lawyer…) get only the generic description.
2. **Task facts (data minimisation).**
   - **When:** a question, comparison or plan that mentions saved places.
   - **What:** the server computes driving times between those places from their stored coordinates (OSRM receives coordinates only, as for any route) and sends **rounded minutes between placeholders**, never coordinates or addresses. START is the configured starting point.
   - **People:** a person's place counts when the person is mentioned ("visit mom" uses the saved place "mom's house").

The leak check covers the legend and the facts too. `JLR_MASKING_MODE` selects `plain`, `roles` or `roles_facts` (the default). `bench/masking_benchmark.py` compares the three modes.

## Configuration

All tiers, word lists, patterns and the decision table are in `jlr/privacy_policy.json` (versioned, like the routing profile); `JLR_PRIVACY_POLICY` can point to another file. Code: `jlr/privacy_policy.py` (`assess`, `decide`, `outbound_blocklist`), wired into `V2Coordinator.resume` in `jlr/coordinator.py`.

## Related structural fixes made while testing

- **Router reference check:** a capitalisation-only difference ("my cholesterol" vs "My cholesterol") no longer fails the whole request; the prompt's own text is used. Invented or changed words are still rejected.
- **Context Resolution:** a personal fact whose value is stated in the request ("my salary is 52000") needs no database lookup. It is still detected and protected by the privacy policy.
- **Session references:** a phrase counts as one only if it contains a pointing word (it, that, there, the same, we discussed…). "recommend one for me" no longer triggers a clarification.

## Verification (16 Oct)

Tests: 144 Python tests pass, including 19 new privacy-policy tests that cover every cell of the decision table. All frontend tests pass.

Live results on the development server:

| Request | Complexity | Tier | Route |
|---|---|---|---|
| What is cholesterol? | simple | P0 | local |
| Play my usual songs | simple | P1 | local |
| Compare three insurance plans… | complex | P0 | larger model, unmasked |
| Compare driving to work vs bus… | complex | P1 | larger model, "work" → `<PRIVATE_1>` |
| My cholesterol is high… compare diet plans | complex | P2 health | kept local + notice |
| My salary is 52000… loan vs investing | complex | P2 finance | kept local + notice |
| My PIN is 4821… compare banks | complex | P3 | kept local; PIN shown as `[hidden credential]` |

## Known limitations

- **Lexicon misses:** topic detection is a word list. It misses sensitive content phrased without listed terms, and it can over-flag (e.g. "loan" in a public question with "my").
- **Router noise:** the router still over-extracts some public words ("cost", "effort") as personal. That doesn't change P2/P3 decisions, but on P1 requests it causes unnecessary masking.
- **Answer quality:** kept-local answers to complex requests are weaker; the local model often asks for data instead of reasoning.
- **No consent option:** the driver can't yet choose "send a masked version anyway" for P2 content.
- **Storage:** P3 values typed by the driver are still stored in the session turn history.
- **Evaluation:** there's no measured accuracy yet. The next step is a labelled evaluation set for tier detection, route decisions and leakage rate.
