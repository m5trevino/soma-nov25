# Running the Soma pipeline on a GPU box (RunPod / Vast)

What is proven, what is not, and the commands. Written 2026-11-19.

> **STATUS (2026-11-20): NOT READY.** An earlier version of this file implied the
> pipeline was ready for a GPU box. It was not, and is not. The facts:
> - A full 12-stage pass (through a passing OLC) has happened once: 1 of 7 sessions in a
>   rotating-model run. It has not been reproduced, and never on a single model.
> - On one model (`qwen/qwen3.7-flash`) the extraction finished 3 of 6 chat sets, and the
>   build never got past its first stage. Drafter passes about 1 call in 3 on any model.
> - Nothing has been tried against vLLM at all.
> Do not move this to a GPU box until one complete run, all 12 stages, passes from the
> launcher with its gate verdicts visible on the log site.

## What the pipeline does

Every stage is a tool-call conversation against the Soma (SQLite). The model reads its
instructions (`get_receptor_config`), its input (`get_chatset`, `query_nodes`) and writes its
output (`create_soma_node`). Python gates every write and hands rejections back to the model.

Two loops, never one process: `--phase extract` (distiller, introbeaver, planbeaver,
outrobeaver, per chat set) then `--phase crystallize` (nexus, spark, falcon, eagle, drafter,
scout, oc, olc, per session).

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `ENGINE_URL` | the engine that routes to models | from `.env` (a Tailscale IP) — use `http://engine.save-aichats.com:3099` off-network |
| `ENGINE_KEY` | engine auth. Put it in the environment, never in a tracked file | — |
| `SOMA_SESSIONS_DIR` | chat-log sessions, one directory each | `/media/storage/dendritic123/chat-logs/sessions` |
| `SOMA_INVARIANTS_DB` | invariants corpus (2.6 GB SQLite, scout queries it) | `/media/storage/invariants/invariants.db` |
| `SOMA_SOMAS_DIR` | where per-session Soma databases go (the harness sets one per lane) | `somas/` |
| `SOMA_STRICT_PINS` | `0` lets a pinned stage fall back to the whole deck | strict (on) |
| `SOMA_WRITE_RETRIES` | times a gate-rejected write is handed back to the model | `2` |
| `SOMA_TRAJECTORY_FULL` | `1` keeps instruction/chat-set text in trajectory logs | off (stored by hash) |

Setup: clone the repo, create a venv, `pip install -e turn-key`, copy a sessions subset and
`invariants.db` (or point the variables at them), set `ENGINE_URL` and `ENGINE_KEY`.

## Serving the model

- The engine must have a gateway entry for the endpoint (base URL, model id). **Not done yet.**
- The loop needs tool calling with `tool_choice: "required"`. vLLM needs
  `--enable-auto-tool-choice` and the tool-call parser that matches the model.
  **Unproven on vLLM** — this is the first thing to test on the box.
- Pin by model id in the profile: every stage's `pinnedModels`. A pin is strict: a stage is
  served by its pinned model or it fails. Pins match by model id, not provider.

## Smoke test, then the real thing

```bash
# 1. one distiller call on one chat set (is tool calling working on this model?)
python3 soma/run_engine.py --soma-only --source SESSION_DIR --profile PINNED_PROFILE.json

# 2. one session, both loops, headless
python3 soma/repeat_runs.py --profile PINNED_PROFILE.json --require-pins \
    --source SESSION_DIR --runs 1 --root runs/smoke

# 3. many runs
python3 soma/repeat_runs.py --profile PINNED_PROFILE.json --require-pins \
    --source S1 --source S2 ... --runs 5 --parallel 8 --root runs/exp1
# extraction only, first N chat sets of each session:
#   --phases extract --max-chatsets 4
```

Each job gets its own lane (own Soma, own log). Output under `--root`:
`report.txt`, `results.json` (pass table, boundary tasks), and per lane `out/trajectories.jsonl`
(every stage call: the whole tool conversation, tokens per turn, verdict, run id).

A job that ends with gate failures is a normal result (`ok, gate failures`). `CRASHED` means an
exception. A stage that cannot get an answer is recorded as a failed stage (`TRANSPORT`) and the
build halts there.

## Baseline to compare against (gemini-3.1-flash-lite, 12 real sessions, small sample)

Distiller on the chat sets it attempted (not skipped): **29%** pass before provenance feedback,
**47%** after (17 → 10 failures of 55 calls). Cost: ~34% more tokens per call (more turns).
Remaining failures are misquotes (`fabricated_quote`, `text_not_found`).
The other stages passed when they ran: introbeaver 6/7, planbeaver 6/6, outrobeaver 6/6,
nexus–eagle 5/5, drafter 2/2, scout 2/2, oc 2/2, olc 1/1.
Only 1 of 5 full-pipeline runs reached a passing olc: the rest had no chat set that cleared the
distiller, so drafter halted with `MISSING_SOMA_DATA`, or oc returned REWORK.

About 70% of chat sets in a random sample are skipped (`OUT_OF_DOMAIN`: not technical, no build
intent; `STATIC_ONLY`). That is the distiller acting as the app-building filter. Pick app-building
sessions for pass-rate experiments.

## Not done / not proven

- vLLM tool-call compatibility, and the engine gateway entry for the endpoint.
- Whether a small model beats 47% on the distiller. Expect worse before better.
- The retry budget (`SOMA_WRITE_RETRIES` 2 vs 4): the experiment hit Gemini's free-tier
  15 requests/minute and was inconclusive. A 429 now waits 20–75 s on a pinned stage.
- Quality of the derived artifact state (outrobeaver) and invariant rules (olc) on rich sessions.
- Eagle's output has no consumer.
