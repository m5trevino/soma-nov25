# How stages work: building an app that uses the TurnKey SDK for AI

For an agent implementing an app. Everything here was read from the code in
`turn-key/turnkey/` (`sdk.py`, `types.py`, `loader.py`, `gateway_rotator.py`) and
the Soma client in `soma/machinetalk/client.py`. Where the code and the design
intent differ, that is said plainly under "Known gap".

## 1. The rule that governs everything

The owner's own words, stated 2026-11-13. Do not paraphrase them.

> tool call to the soma db. reads the instructions from the soma db. reads the chat set from the soma db. process it and writes the output into the soma db.

- A stage is one unit of AI work. The model starts it with a tool call to the Soma,
  reads its instructions from the Soma, reads its input (the chat set) from the Soma,
  processes it, and writes its output back into the Soma.
- **The model is never the gate.** Python validates what gets written. Do not move a
  decision (pass/fail, routing, "is this good enough") into a prompt.
- Do not put app logic or history into a system prompt. Instructions live in the
  database (`stage_instructions` table), not in prompt files and not concatenated
  into the request.

## 2. The four layers

```
your app
  -> run_stage(stage_name, messages, ...)        TurnKey SDK       turn-key/turnkey/sdk.py
       -> rotator.next_model(stage_name)         picks model + provider (gateway)
       -> POST {engine_url}/v1/chat/completions  the only HTTP call there is
            -> the engine (FastAPI, :3099)       routes to the provider, logs the call
  <- raw OpenAI-style JSON dict
your app: validate the output in Python (gate), then write it to the Soma
```

There is no `anthropic` or `openai` SDK in this stack. Every model call goes through
`SDK.run_stage()`. Never build your own request to `/v1/chat/completions`: that
bypasses authentication, the model rotator, retries and provenance.

## 3. What a "stage" is in the SDK

A stage is just a **name** you pass to `run_stage`. The SDK uses the name for two things:

1. **Model selection.** The profile may pin models to a stage (`pinnedModels`). A
   pinned stage draws only from its own list, rotating among them in order. An
   unpinned stage draws from the whole deck, rotating across gateways.
2. **Labelling.** The name is sent to the engine as `stage` so the call is logged
   under it.

The SDK does not know what a stage means. Meaning (the instruction text, the input
shape, the output contract) belongs to the app and the Soma database.

In the Soma pipeline the stages are `distiller`, `introbeaver`, `planbeaver`,
`outrobeaver`. Other names (`receptor`, `classifier`, `integrator`, `retrograde`)
are aliases mapped onto those four in `soma/machinetalk/client.py:33-40`.
**Those four are Soma's, not the SDK's.** A different app defines its own stages;
see section 14.

## 4. The profile (one JSON file)

`SDK(profile_path=...)` loads it at init and fails fast if it is invalid. A TurnKey
profile must contain `deck`, `stages`, `pipeline`.

```json
{
  "deck": [
    {"modelId": "some/model-id", "provider": "openrouter"}
  ],
  "stages": [
    {"stageName": "distiller", "pinnedModels": [], "useDeck": true},
    {"stageName": "planbeaver", "pinnedModels": ["some/model-id"], "useDeck": true}
  ],
  "pipeline": {
    "timeout": 180,
    "maxRetries": 10,
    "engineUrl": "http://engine.save-aichats.com:3099",
    "engineKey": "<set via ENGINE_KEY, see section 8>"
  }
}
```

- **Every deck card needs `modelId` and `provider`.** The SDK sends them as two
  separate fields and the engine is not allowed to choose the gateway. A card with
  no provider (or `unknown`/`default`) fails with `PROVIDER_REQUIRED`.
- A stage can be given as a list (above) or a dict keyed by stage name.
- **To test one model, pin it in the profile (`pinnedModels`). Never in code.**
  Clients and dendrites do not pick models; TurnKey rotates over the deck.
- Utility models (embedding, rerank, whisper, tts, ocr, image/video) are dropped from
  the deck automatically (`is_chat_model`), because they cannot answer a chat call.
- Engine URL order: profile `pipeline.engineUrl`, then the `ENGINE_URL` env var, then
  the SDK default `http://engine.save-aichats.com:3099`.
- Defaults when the profile is silent: timeout 180 s, 10 retries.

## 5. Calling a stage

```python
from turnkey import SDK

sdk = SDK(profile_path="path/to/profile.json")
raw = sdk.run_stage(
    "distiller",
    messages=[{"role": "system", "content": ...}, {"role": "user", "content": ...}],
    session_id="SES-014",     # the run's session code, see section 7
    timeout=None,             # per call; falls back to profile timeout
    temperature=0.0,          # optional; extraction stages use 0.0
    tools=None, tool_choice=None,   # optional pass-through
)
text = raw["choices"][0]["message"]["content"]
served_by = raw["provider"]       # SDK fills this in if the engine did not
```

What `run_stage` does, in order:

1. `rotator.next_model(stage)` returns `{model_id, gateway}`.
2. It POSTs `{model, provider, messages, stream: false, [tools, tool_choice,
   temperature, pea_session, stage]}` to `/v1/chat/completions` with the engine key
   in the `X-Peacock-Key` header.
3. If the engine reports a different provider than the one asked, the call is
   **rejected** (`PROVIDER_MISMATCH`), not accepted.
4. On a transient failure (`CONNECTION_ERROR`, `TIMEOUT`, any `HTTP_5xx`) it reports
   the failure to the rotator's circuit breaker, rotates to the next model and
   retries, up to `maxRetries`.
5. On a non-retryable failure or when retries run out it raises `StageError`.

The return value is the raw engine JSON as a `dict`. `session_id` is sent to the
engine as `pea_session`. Note the SDK docstring still says `pea_session` for that
parameter; the real parameter name is `session_id`. The Soma client detects the name
with `_pea_param_name` for that reason.

## 6. Errors you must handle

| Raised | `error_code` | Meaning | Retried by SDK? |
|---|---|---|---|
| `StageError` | `PROVIDER_REQUIRED` | a deck card has no real provider | no |
| `StageError` | `PROVIDER_MISMATCH` | engine served from a different gateway | no |
| `StageError` | `HTTP_4xx` | bad request or auth (401 = missing/wrong engine key) | no |
| `StageError` | `HTTP_5xx`, `TIMEOUT`, `CONNECTION_ERROR` | transient, retries exhausted | yes |
| `SDKError` | `PARSE_ERROR` | engine reply was not JSON | no |
| `ValueError` | | `messages` was empty | no |

`StageError` carries `retry_history`, `failed_models` and `root_cause`, and
`to_dict()` is JSON-serializable for logging.

Some failures escape the SDK as plain exceptions (`http.client.RemoteDisconnected`
is one). The Soma client wraps `run_stage` in its own retry loop (default 10
attempts) so a dropped connection does not kill a whole chat set. Do the same, and
treat a `TypeError` as a code bug that must not be retried.

## 7. Session code and linkage

The app owns the run's session counter (e.g. `SES-014`) and sends it on **every**
call as `session_id`. The engine groups calls under it and assigns the stage id.
A chat-session *name* is not a session code: the engine does not accept it, and it
starts a new session per call. The engine's own `SES-0xx` numbering is global and
differs from the CLI's local counter; match them by timestamp, model and token
counts.

## 8. Authentication and secrets

- Inference calls (`/v1/chat/completions`) need the engine key. The SDK reads
  `pipeline.engineKey` from the profile, else the `ENGINE_KEY` environment variable
  (it loads every `.env` it finds walking up from the working directory).
- **Put the key in `.env` (mode 600, gitignored). Never in a tracked file, a profile
  committed to git, a log, a report, or a prompt.** Mask it whenever you show an env
  file. Send it only to your own engine.
- A 401 on every call almost always means `ENGINE_KEY` was not loaded.

## 9. The stage lifecycle for an app (the pattern to implement)

This is the order the Soma pipeline follows (`EngineClient.call_stage`). Follow it.

1. **Read the instruction row** for the stage from the `stage_instructions` table.
   If there is no row, stop with an error. Never fall back to a file on disk.
2. **Read the input** (the chat set / payload) from the Soma database.
3. **Call the stage** through `run_stage` (retry wrapper around it, section 6).
4. **Gate the output in Python.** `soma/machinetalk/gate.py` parses and validates the
   output against the stage's contract. Examples: the distiller must return the
   `chatset_hash`; introbeaver and planbeaver return a `status`; a `skip` flag is
   honoured only if Python agrees.
5. **Check provenance in Python.** For `distiller` and `introbeaver`, every entry the
   model emits must match the source text at the offsets it claims
   (`soma/machinetalk/provenance.py`). A quote that is not verbatim in the transcript
   fails the stage (`fabricated_quote`). The model's claim is not allowed into the
   graph unverified.
6. **Write the accepted output to the Soma database.** Only validated output is
   written. Python does the math (counts, noise signal, thresholds) and the model
   does language.
7. **A stage is done** when the gate passed, or when it was skipped without errors
   (`is_done = gate.ok or (gate.skipped and not gate.errors)`).

Each stage is told its own job and nothing about the other stages, so it has no
pipeline to reason about.

## 10. Reading results correctly

- Engine `SUCCESS` only means the HTTP call worked. It does **not** mean the output
  passed the gate. The gate result and the local per-stage log
  (`soma/output/logs/<stage>/`) are the truth.
- Failure kinds to check first: `missing:chatset_hash` (a stub reply), 
  `fabricated_quote` (directive text not verbatim in the transcript), `json_parse`
  (broken JSON), engine `Request timed out`.
- Judge a model by the gate verdict, never by "it returned something".
- Do not loosen a gate, a prompt or a provenance rule to make a run pass. If the gate
  itself looks wrong, say so with evidence and let the owner decide.

## 11. Keeping the deck current

`sdk.sync()` fetches the live model list from the engine
(`GET /v1/freshner/models`), rebuilds the deck by gateway, and keeps your stage
assignments and pipeline settings. The engine decides what it lists (provider
switches and price policy are set on the model catalog at
`https://catalog.save-aichats.com/`). The deck you call from is whatever your profile
holds; `sync()` is how it follows the engine.

## 12. Checklist before you ship an app on this SDK

- [ ] Every deck card has `modelId` and a real `provider`.
- [ ] `ENGINE_KEY` is in `.env`, not in any tracked file.
- [ ] Every call goes through `sdk.run_stage()`. No raw HTTP to the engine.
- [ ] Every call sends the run's session code as `session_id`.
- [ ] Stage instructions come from the `stage_instructions` table, with no copy in
      code or in a prompt file.
- [ ] Every stage output passes a Python gate before it is written; provenance is
      checked where the stage quotes source text.
- [ ] The app handles `StageError` and `SDKError`, and has its own retry for
      connection drops that escape the SDK.
- [ ] Success is judged by the gate verdict, not by the HTTP status.
- [ ] A test run uses ONE pinned model and the same chat set each time.
- [ ] No rule was invented: if a FunctionSlot has no rule, flag it for human review.

## 13. Known gap between the stated design and the current code

The design (section 1) says the model itself fetches its instructions and its input
from the Soma. The current `EngineClient.call_stage` does this in Python instead: it
reads the `stage_instructions` row, assembles a system message and a user message
(`fleet.assemble`), and sends both to `run_stage`. The instruction source is correct
(the database, not disk); who fetches it is not what the owner described. Do not
extend that pattern and do not "fix" it on your own. Raise it with the owner.

Also, `AGENTS.md` and `CLAUDE.md` point to `soma/PIPELINE_ACTION_MAP.md`,
`soma/SESSION_HANDOFF.md` and `soma/TURNKEY_TECHNICAL_MANUAL.md`. Those three files
were not found in the repository when this guide was written, so nothing here is
taken from them.

## 14. A new app with its own stages (not Soma's four)

The SDK and the engine do not know the Soma stage names. A stage is only a string,
so a new app uses its own stages and the same mechanics as sections 3 to 11.

**What is generic (use as is)**
- `SDK`, `run_stage(stage_name, ...)`, the profile, deck rotation, retries,
  `StageError`, `session_id`, the engine key.
- The engine accepts any stage name. It logs the call under that name and gives it a
  stage id such as `XX-01`.

**What is Soma-specific (do not reuse, build your own)**
- `distiller / introbeaver / planbeaver / outrobeaver`, their aliases, and
  `_EXTRACTIVE_STAGES` in `soma/machinetalk/client.py`.
- `gate.py` and `provenance.py`: those validate Soma's output contracts and
  chat-set quotes. A new app's stages have different contracts.
- The Soma database and its `stage_instructions` table.

**Steps for a new app**
1. **Name the stages** after the app's own jobs, one string per job
   (for example `extract`, `rank`, `summarize`). Keep each stage to one job.
2. **Add each one to the profile** under `stages`. Pin models per stage with
   `pinnedModels`, or leave `[]` and `useDeck: true` to rotate over the deck. A stage
   name that is missing from the profile still works: it simply draws from the whole
   deck with no pin.
3. **Give each stage its own instruction row** in the app's own Soma database
   (the app's store plays the role `stage_instructions` plays for Soma). Instructions
   live there, not in code, prompt files, or concatenated into the request. A stage
   is told its own job and nothing about the other stages.
4. **Define each stage's output contract** and write a Python gate for it: what shape
   the output must have, what counts as pass, what counts as skip. The model is never
   the gate.
5. **Define provenance only if the stage quotes source text.** If so, verify every
   quote against the source in Python. If the stage does not quote, no provenance
   check is needed.
6. **Write only gated output** into the app's Soma database. Python does the math,
   the model does language.
7. **Send the run's session code** as `session_id` on every call, as in section 7.
8. **Record the contract for each stage** (name, instruction row, input, output
   shape, gate rule). If the owner has not defined a gate rule for a stage, flag it
   for human review. Do not invent one.

**Naming caution (checked in the engine code).** The engine builds a stage's code from
the first two letters of its name (`soma_counter.stage_code`), with a counter per full
name. Two stages that start with the same two letters (for example `summarize` and
`support`) get ids like `SU-01` and `SU-01` and cannot be told apart by id. Choose names
whose first two letters differ. Short lowercase names, no spaces.

**Minimal skeleton**

```python
from turnkey import SDK, StageError

sdk = SDK(profile_path="app_profile.json")      # stages: extract, rank, summarize
SESSION = "SES-001"                              # the app's run counter

def run(stage, instruction_body, payload):
    messages = [
        {"role": "system", "content": instruction_body},   # from the app's Soma DB
        {"role": "user", "content": payload},              # from the app's Soma DB
    ]
    raw = sdk.run_stage(stage, messages, session_id=SESSION)
    return raw["choices"][0]["message"]["content"]

# for each stage: read its row + input from the DB, run it, gate it in Python,
# and write to the DB only if the gate passes.
```

The skeleton shows the SDK call only. Reading the DB, the gate and the write are the
app's own code and must follow sections 1 and 9. Per section 13, the owner's stated
design is that the model fetches its own instructions and input from the Soma by
tool call; confirm with the owner which way the new app should do it before building
it.
