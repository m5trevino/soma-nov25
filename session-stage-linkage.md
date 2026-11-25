
# SESSION & STAGE ID LINKAGE — COMPLETE SPEC

---

## 1. WHAT THE IDs ARE

**Session ID**: human-readable counter, never a UUID.
Format: `SES-001`, `SES-002`, `SES-014` — SES + zero-padded 3-digit number.
One per run. Never resets globally.

**Stage ID**: short code + running counter.
Format: `DI-01`, `DI-03`, `IB-01`, `PB-02` — 2-letter code + zero-padded 2-digit number.
Code identifies stage type. Counter per-stage-type, never resets.
NOT the full name (`distiller-02` was rejected; `DI-02` was confirmed).

**log_link**: engine response includes a URL to the dashboard viewer for that call.
Format: `{SOMA_DASHBOARD_URL}/v1/soma/?session={session}&slug={stage_name}&stage={stage_id}`

---

## 2. SHORT CODE MAPPING

| Stage slug      | Code | | Stage slug      | Code |
|-----------------|------|---|-----------------|------|
| distiller       | DI   | | axonhillock     | AX   |
| introbeaver     | IB   | | scout           | SC   |
| planbeaver      | PB   | | myelinator      | MY   |
| outrobeaver     | OB   | | terminalplayer  | TP   |
| nexus           | NX   | | saltatorygate   | SG   |
| spark           | SP   | | codepublisher   | CP   |
| falcon          | FA   | | seeder          | SE   |
| drafter         | DR   | |                 |      |

---

## 3. DATA FLOW

> **Corrected 2026-11-16.** An earlier version of this doc said the pipeline
> owns the session id and sends its own counter on every call. That was wrong,
> and it caused every re-run of the same chat session to merge into one engine
> session. The ENGINE generates the session id. The pipeline uses it.

### The rule

- **A call that carries a stage and NO `pea_session` is the first call of a new
  session.** That absence tells the engine "generate one". The engine runs
  `assign_session()` and returns the code in the reply.
- **A call that carries a `pea_session` is a continuation.** Sending the id tells
  the engine the id is already assigned, so it does not generate another.

### Pipeline (runs on a different machine than the engine)

For each chat session in a run:

1. First call: send `stage=...` and **no** `pea_session`.
2. Read `session` from the reply (the engine's code, e.g. `SES-321`).
3. Every later call for **that session and only that session**: send
   `pea_session=<that code>`.
4. The next run, or the next chat session, starts again at step 1 and gets its
   own engine session.

The first call per session is serialized. If two first calls were in flight
together, both would send no id and the engine would open two sessions.

The local `SES-007`-style code (`short_codes.py`) is a display and file-name
counter on the pipeline's side. It is **never** sent to the engine.

Code: `soma/machinetalk/engine_sessions.py` (the rule),
`EngineClient.call_stage` (`session_code` is the engine's id or `None`),
`engine.py` (`_engine_sessions`).

### Engine (on the engine box, this repo)

1. `pea_session` provided -> continuation; uses it as-is. Writes `SESSION: <value>`.
2. `pea_session` empty AND `stage` present -> assigns `assign_session()`.
3. Every call with `stage`: `assign_stage(stage_name)` -> the stage id (`DI-03`).
4. Writes `SESSION:` and `STAGE:` to the PEA file header.
5. Returns `session`, `stage`, `log_link` in the response (via `write_pea_file`).

### Response shape (already built and verified)

```json
{
  "...": "the LLM answer",
  "session": "SES-002",
  "stage": "DI-03",
  "log_link": "https://engine.save-aichats.com/v1/soma/?session=SES-002&slug=distiller&stage=DI-03",
  "pea_session": "SES-002",
  "pea_dir": "...",
  "pea_file": "..."
}
```

---

## 4. WHY THE PIPELINE MUST NOT MAKE UP A SESSION ID

If the pipeline sends an id of its own from the first call (a local counter, or
the chat session's name), the engine treats it as an existing session and groups
by it. A local id that is stable per chat session therefore merges every run of
that chat session into one session. Observed on 2026-11-16: `SES-007` held 40
calls from repeated runs of one chat session.

Sending nothing on the first call is the signal that makes the engine start a
new session.

---

## 5. NEW SESSION VS CONTINUATION (Engine Logic)

- `pea_session` empty AND `stage` present -> new session (engine generates it).
- `pea_session` matches a known session -> continuation.
- `pea_session` is new to the engine -> the engine accepts it as a new session
  (lazy-add). The pipeline must not rely on this: it exists for other callers.

---

## 6. COUNTER PERSISTENCE

- File: `soma_counters.json` (beside the DB at `/root/hetzner/ai-engine/`).
- Thread-safe via `threading.Lock`.
- Survives engine restarts.
- Stage counters never reset — `DI-01`, `DI-02`, …, `DI-156`, … forever.

---

## 7. WHAT'S ALREADY BUILT

| File | What it does | Status |
|---|---|---|
| `app/core/soma_counter.py` | `assign_session()`, `assign_stage()`, `stage_code()`, `STAGE_CODES`, persist/load/save, thread lock | Verified working |
| `app/routes/openai_compat.py` | `stage` field on `OpenAIChatRequest`. Endpoint assigns session+stage when stage present. All 5 finalize paths include stage in meta via `_soma_stage_meta()` | Verified: PEA shows SES-002, DI-03 |
| `app/core/pea_trace.py` | `render_pea_text` reads `stage_id`. `write_pea_file` returns `session`, `stage`, `log_link`. Link uses `SOMA_DASHBOARD_URL` | Verified |
| `app/routes/soma_logs.py` | Deep-link support (`?session&slug&stage` params). Auto-selects session, opens stage cell | Dashboard returns 200 |
| `soma_counters.json` | Persisted counter state | Created on first call |

---

## 8. WHAT THE PIPELINE/SDK STILL NEEDS

The TurnKey SDK (`turn-key/turnkey/sdk.py`) currently builds:
```python
payload = {"model": model, "messages": messages, "stream": False}
```
It does NOT send `stage` or `pea_session`.

Needed changes:
1. `run_stage()` must accept `session_id` and `stage_name` parameters.
2. `_call_engine()` must include `"stage": stage_name` and `"pea_session": session_id` in the payload.
3. Pipeline: the first call of a session sends no `pea_session`; it reads the engine's `session` from the reply and passes that to every later `run_stage` call of that session (see section 3).

These changes are on the user's side (different box), not in this repo's engine code.

---

## 9. ARCHITECTURE RATIONALE

- **Counter, not UUID**: UUIDs are indistinguishable at a glance.
- **Short codes, not full names**: `DI-01` passes more info per character than `distiller-01`.
- **Never resetting stage counters**: `DI-01` is globally unique across all runs — universal identifier.
- **Engine generates the session ID**: one authority, one counter, no collisions. The pipeline signals a new session by sending no id on the first call.
- **Engine generation is the normal path, not a fallback**: any call with a stage and no id gets a readable one.

---

## 10. END-TO-END FLOW EXAMPLE

```
A new run starts a chat session.

Call 1:  stage=distiller            (no pea_session)
         -> Engine generates SES-321, STAGE: DI-03
         -> Response: session=SES-321, stage=DI-03, log_link

Call 2:  pea_session=SES-321, stage=introbeaver
         -> Engine: already assigned. SESSION: SES-321, STAGE: IB-01
         -> Response: session=SES-321, stage=IB-01, log_link

Call 3:  pea_session=SES-321, stage=distiller
         -> SESSION: SES-321, STAGE: DI-04

The next run of the same chat session: Call 1 sends no pea_session,
and the engine generates SES-322. The two runs never merge.
```

Dashboard groups all calls of a run under its own engine session. Each call has
its own stage ID and log_link.
