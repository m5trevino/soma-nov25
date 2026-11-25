"""Many runs of one model: the results table and the harness that makes it."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import repeat_runs
from machinetalk import results


def _rec(run, stage, status, session="S1", chatset="S1:1", model="my-7b", errors=None,
         tokens=100, ms=2000, rewrites=0):
    return {"run_id": run, "stage": stage, "status": status, "session_id": session,
            "chatset_id": chatset, "model_served": model, "errors": errors or [],
            "tokens": {"total": tokens}, "latency_ms": ms, "write_rejections": rewrites}


# ── the table ──────────────────────────────────────────────────────────────

def test_pass_table_counts_outcomes_per_model_and_stage():
    recs = [_rec("r1", "distiller", "pass"), _rec("r2", "distiller", "pass"),
            _rec("r3", "distiller", "fail", errors=["fabricated_quote (not in transcript: 'x')"]),
            _rec("r4", "distiller", "skip"),
            _rec("r1", "planbeaver", "pass", model="other")]
    rows = {(r["model"], r["stage"]): r for r in results.pass_table(recs)}
    d = rows[("my-7b", "distiller")]
    assert (d["calls"], d["pass"], d["fail"], d["skip"]) == (4, 2, 1, 1)
    assert d["pass_rate"] == round(2 / 3, 3)             # a skip is not an attempt
    assert d["top_failures"] == [("fabricated_quote", 1)]
    assert rows[("other", "planbeaver")]["pass_rate"] == 1.0


def test_a_stage_that_only_skipped_has_no_rate():
    (row,) = results.pass_table([_rec("r1", "distiller", "skip")])
    assert row["pass_rate"] is None


def test_failures_are_counted_by_kind_not_by_text():
    assert results.error_kind(["TB004-S1-0001: fabricated_quote (not in transcript: 'a')"]) == "fabricated_quote"
    assert results.error_kind(["json_parse:Expecting value@3995"]) == "json_parse:Expecting"
    assert results.error_kind(["directives[2]: fabricated_quote (not in transcript: 'x')"]) == "fabricated_quote"
    assert results.error_kind(["artifacts[0]: bad_msg_index (7)"]) == "bad_msg_index"
    assert results.error_kind(["missing:chatset_hash", "x"]) == "missing:chatset_hash"
    assert results.error_kind(["MISSING_SOMA_DATA: chat set X is not in the Soma"]) == "MISSING_SOMA_DATA"
    assert results.error_kind([]) == ""


def test_rows_come_out_in_pipeline_order():
    recs = [_rec("r", s, "pass") for s in ("olc", "distiller", "scout", "planbeaver")]
    assert [r["stage"] for r in results.pass_table(recs)] == ["distiller", "planbeaver", "scout", "olc"]


def test_boundary_tasks_are_the_ones_that_sometimes_pass():
    recs = ([_rec(f"r{i}", "distiller", "pass", chatset="S1:1") for i in range(3)]       # mastered
            + [_rec(f"r{i}", "distiller", "fail", chatset="S1:2") for i in range(3)]    # out of reach
            + [_rec("r1", "distiller", "pass", chatset="S1:3"),
               _rec("r2", "distiller", "fail", chatset="S1:3"),
               _rec("r3", "distiller", "pass", chatset="S1:3")]                         # boundary
            + [_rec("r1", "distiller", "fail", chatset="S1:4")])                        # one run: unknown
    (t,) = results.boundary_tasks(recs)
    assert t["chatset_id"] == "S1:3" and t["pass_rate"] == round(2 / 3, 3) and t["runs"] == 3


def test_a_run_counts_as_built_only_when_olc_passed():
    recs = [_rec("A", "olc", "pass"), _rec("B", "olc", "fail"), _rec("C", "scout", "pass")]
    runs = [{"type": "run", "run_id": r, "phase": "crystallize"} for r in "ABC"]
    built = {s["run_id"]: s["built"] for s in results.run_summaries(recs, runs)}
    assert built == {"A": True, "B": False, "C": False}


def test_load_separates_headers_from_stage_calls_and_skips_a_torn_line(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps({"type": "run", "run_id": "r"}) + "\n"
                 + json.dumps(_rec("r", "distiller", "pass")) + "\n" + '{"torn": ')
    stages, runs = results.load([f, tmp_path / "missing.jsonl"])
    assert len(stages) == 1 and len(runs) == 1


# ── the harness ────────────────────────────────────────────────────────────

class FakeRunner:
    """Stands in for run_engine: records the commands, writes a trajectory file."""

    def __init__(self, fail_phase=None, crash_phase=None):
        self.calls = []
        self.fail_phase = fail_phase        # exits 1: the gate failed something
        self.crash_phase = crash_phase      # exits 1 with a traceback in the log

    def __call__(self, cmd, env=None, stdout=None, stderr=None, cwd=None, timeout=None):
        self.calls.append((cmd, env))
        phase = cmd[cmd.index("--phase") + 1]
        out = Path(cmd[cmd.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        run_id = cmd[cmd.index("--run-id") + 1]
        with open(out / "trajectories.jsonl", "a") as f:
            f.write(json.dumps({"type": "run", "run_id": run_id, "phase": phase}) + "\n")
            stage = "distiller" if phase == "extract" else "olc"
            f.write(json.dumps(_rec(run_id, stage, "pass")) + "\n")
        if phase == self.crash_phase:
            stdout.write("Traceback (most recent call last):\n  boom\n")
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=1 if phase == self.fail_phase else 0)


def _argv(tmp_path, *extra):
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"stages": [
        {"stageName": s, "pinnedModels": ["my-7b"]} for s in results.STAGE_ORDER]}))
    return ["--profile", str(profile), "--source", "/data/sess-a", "--source", "/data/sess-b",
            "--root", str(tmp_path / "root"), *extra]


def test_every_run_of_every_session_gets_its_own_lane(tmp_path):
    runner = FakeRunner()
    assert repeat_runs.main(_argv(tmp_path, "--runs", "2"), runner=runner) == 0
    lanes = sorted(p.relative_to(tmp_path / "root").as_posix()
                   for p in (tmp_path / "root").glob("run-*/*"))
    assert lanes == ["run-001/sess-a", "run-001/sess-b", "run-002/sess-a", "run-002/sess-b"]
    # each lane has its own Soma directory and its own run id
    somas = {e["SOMA_SOMAS_DIR"] for _c, e in runner.calls}
    assert len(somas) == 4
    ids = {c[c.index("--run-id") + 1] for c, _e in runner.calls}
    assert len(ids) == 4


def test_both_loops_run_in_order_for_each_job_headless(tmp_path):
    runner = FakeRunner()
    repeat_runs.main(_argv(tmp_path), runner=runner)
    phases = [c[c.index("--phase") + 1] for c, _e in runner.calls]
    assert phases == ["extract", "crystallize", "extract", "crystallize"]
    assert all("--no-dashboard" in c for c, _e in runner.calls)


def test_gate_failures_do_not_stop_the_build_stages_from_being_measured(tmp_path, capsys):
    runner = FakeRunner(fail_phase="extract")
    rc = repeat_runs.main(_argv(tmp_path, "--source", "/data/sess-c"), runner=runner)
    assert {c[c.index("--phase") + 1] for c, _e in runner.calls} == {"extract", "crystallize"}
    assert rc == 0                                           # a model failing a gate is a result
    assert "ok, gate failures" in capsys.readouterr().out


def test_stop_on_failures_makes_extraction_gate_failures_final(tmp_path):
    runner = FakeRunner(fail_phase="extract")
    repeat_runs.main(_argv(tmp_path, "--stop-on-failures"), runner=runner)
    assert {c[c.index("--phase") + 1] for c, _e in runner.calls} == {"extract"}


def test_a_crash_is_a_failed_job_and_stops_it(tmp_path, capsys):
    runner = FakeRunner(crash_phase="extract")
    rc = repeat_runs.main(_argv(tmp_path), runner=runner)
    assert rc == 1 and "CRASHED" in capsys.readouterr().out
    assert {c[c.index("--phase") + 1] for c, _e in runner.calls} == {"extract"}


def test_classify_tells_a_crash_from_gate_failures():
    assert repeat_runs.classify(0, "") == "ok"
    assert repeat_runs.classify(1, "SESSION COMPLETE failed 3") == "gate_failures"
    assert repeat_runs.classify(1, "Traceback (most recent call last):") == "crashed"
    assert repeat_runs.classify(137, "") == "crashed"           # killed


def test_max_chatsets_belongs_to_extraction_only(tmp_path):
    runner = FakeRunner()
    repeat_runs.main(_argv(tmp_path, "--max-chatsets", "3"), runner=runner)
    for cmd, _e in runner.calls:
        has = "--max-chatsets" in cmd
        assert has == (cmd[cmd.index("--phase") + 1] == "extract")


def test_the_report_is_written_and_names_the_boundary(tmp_path, capsys):
    repeat_runs.main(_argv(tmp_path, "--runs", "2"), runner=FakeRunner())
    root = tmp_path / "root"
    data = json.loads((root / "results.json").read_text())
    assert {r["stage"] for r in data["pass_table"]} == {"distiller", "olc"}
    assert (root / "report.txt").read_text().startswith("RESULTS")
    assert "builds that reached a passing olc" in capsys.readouterr().out


def test_a_lane_is_never_reused(tmp_path):
    root = tmp_path / "root"
    jobs = repeat_runs.build_jobs(["/data/sess-a"], 1, root)
    jobs[0]["lane"].mkdir(parents=True)
    args = SimpleNamespace(phases=["extract"], profile="p", workers=1,
                           max_chatsets=None, engine_url=None, timeout=1)
    with pytest.raises(FileExistsError):
        repeat_runs.run_job(jobs[0], args, 9900, runner=FakeRunner())


def test_require_pins_refuses_a_profile_with_an_unpinned_stage(tmp_path, capsys):
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"stages": [{"stageName": "distiller", "pinnedModels": ["m"]}]}))
    rc = repeat_runs.main(["--profile", str(profile), "--source", "/d/s", "--require-pins",
                           "--root", str(tmp_path / "r")], runner=FakeRunner())
    assert rc == 2 and "NOT pinned" in capsys.readouterr().out
