"""One bad minute must not retire the deck; a wrong provider must not be a failure forever."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from machinetalk.client import EngineClient, TurnKeyClient

REPO = Path(__file__).resolve().parent.parent


# ── the SDK's provider check (the repo copy, which the launcher uses) ───────

def _sdk_check(tmp_path, requested_provider, served, env=None):
    """Run the repo SDK's _call_engine against a canned engine reply."""
    import os
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({
        "version": 1, "name": "t",
        "deck": [{"uid": "c1", "modelId": "x", "provider": requested_provider}],
        "stages": [{"stageName": "distiller", "pinnedModels": [], "useDeck": True}],
        "pipeline": {"mode": "sequential", "workers": 1, "timeout": 5, "maxRetries": 0}}))
    code = textwrap.dedent(f"""
        import json, io, urllib.request
        import turnkey.sdk as m
        from turnkey.profile_integration import initialize_sdk_with_profile
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        urllib.request.urlopen = lambda req, timeout=None: R(json.dumps(
            {{"provider": {served!r}, "model": "x",
              "choices": [{{"message": {{"content": "ok"}}}}]}}).encode())
        sdk = initialize_sdk_with_profile({str(profile)!r})
        try:
            r = sdk._call_engine("x", [{{"role": "user", "content": "hi"}}],
                                 provider={requested_provider!r})
            print("OK", r.get("upstream_provider") or "")
        except m.SDKError as e:
            print("REJECTED", e.error_code)
    """)
    env_vars = dict(os.environ, PYTHONPATH=str(REPO / "turn-key"), **(env or {}))
    out = subprocess.run([sys.executable, "-c", code], env=env_vars, capture_output=True,
                         text=True, timeout=60)
    return (out.stdout + out.stderr).strip().splitlines()[-1].strip()


def test_openrouter_answers_naming_the_upstream_company_are_accepted(tmp_path):
    assert _sdk_check(tmp_path, "openrouter", "Fireworks") == "OK Fireworks"
    assert _sdk_check(tmp_path, "openrouter", "Mistral") == "OK Mistral"


def test_a_real_mismatch_on_an_ordinary_gateway_is_still_rejected(tmp_path):
    assert _sdk_check(tmp_path, "google", "openrouter") == "REJECTED PROVIDER_MISMATCH"


def test_the_gateway_naming_itself_is_accepted(tmp_path):
    assert _sdk_check(tmp_path, "google", "google").startswith("OK")


def test_more_aggregators_can_be_named_in_the_environment(tmp_path):
    assert _sdk_check(tmp_path, "requesty", "Anthropic") == "REJECTED PROVIDER_MISMATCH"
    assert _sdk_check(tmp_path, "requesty", "Anthropic",
                      env={"SOMA_AGGREGATOR_GATEWAYS": "requesty"}) == "OK Anthropic"


# ── the deck ───────────────────────────────────────────────────────────────

class Rotator:
    def __init__(self, shut):
        self._shutdown_models = set(shut)
        self._failures = {"openrouter": {"a": 3, "b": 3}, "google": {"c": 3}}


def _tk(shut):
    tk = object.__new__(TurnKeyClient)
    tk._sdk = SimpleNamespace(rotator=Rotator(shut))
    return tk


def test_a_transient_shutdown_is_lifted_and_the_failure_counts_reset():
    tk = _tk({"openrouter:a", "openrouter:b", "google:c"})
    assert tk.revive_deck() == 3
    assert tk._sdk.rotator._shutdown_models == set()
    assert all(v == 0 for g in tk._sdk.rotator._failures.values() for v in g.values())


def test_a_card_marked_broken_stays_shut_when_the_deck_is_lifted():
    tk = _tk({"openrouter:a", "openrouter:b", "google:c"})
    tk.mark_broken("openrouter", "a")
    assert tk.revive_deck() == 2
    assert tk._sdk.rotator._shutdown_models == {"openrouter:a"}


def test_broken_cards_are_kept_per_client_not_shared():
    one, two = _tk(set()), _tk(set())
    one.mark_broken("openrouter", "a")
    assert two.revive_deck() == 0 and "openrouter:a" not in two.__dict__.get("_broken", set())


class _Dying:
    """A deck that is shut down, then lifted, then answers."""

    def __init__(self):
        self.n, self.revived, self.broken = 0, 0, []

    def call(self, stage, messages, **kw):
        self.n += 1
        if self.n <= 2:
            raise RuntimeError("All models have been shut down due to consecutive failures.")
        return "result"

    def revive_deck(self):
        self.revived += 1
        return 3

    def mark_broken(self, gateway, model):
        self.broken.append((gateway, model))


def _engine(tk):
    ec = object.__new__(EngineClient)
    ec._tk = tk
    ec.slept = []
    ec._sleep = ec.slept.append
    ec.model_for = lambda stage: "m"
    return ec


MSGS = [{"role": "user", "content": "hi"}]


def test_a_fully_shut_down_deck_is_lifted_and_the_call_goes_through():
    tk = _Dying()
    ec = _engine(tk)
    assert ec._call_with_retry("distiller", MSGS) == "result"
    assert tk.revived == 2 and ec.slept == [30.0, 30.0]      # it waited, then tried again


def test_a_provider_mismatch_marks_the_card_broken_by_name():
    class Mismatch:
        n = 0
        broken = []

        def call(self, *a, **k):
            Mismatch.n += 1
            if Mismatch.n == 1:
                raise RuntimeError("PROVIDER MISMATCH: asked provider 'openrouter' for model "
                                   "'mistralai/mistral-nemo' but the engine served it from 'Mistral'.")
            return "result"

        def mark_broken(self, g, m):
            Mismatch.broken.append((g, m))
    assert _engine(Mismatch())._call_with_retry("distiller", MSGS) == "result"
    assert Mismatch.broken == [("openrouter", "mistralai/mistral-nemo")]
