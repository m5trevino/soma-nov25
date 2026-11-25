"""A pinned stage is served by its pinned model or not at all."""
import json

import pytest

from machinetalk.client import (EngineClient, PinUnavailable, TurnKeyClient,
                                pins_from_profile)


class FakeRotator:
    """Just enough of the SDK's deck rotator: a deck, pins, and a breaker."""

    def __init__(self, deck, pins, shut_down=()):
        self._flat = [(g, {"id": m}) for g, m in deck]
        self.pins = pins
        self.shut = set(shut_down)
        self.revived = []

    def peek_model(self, stage=""):
        for model_id in self.pins.get(stage, []):
            for g, m in self._flat:
                if m["id"] == model_id and f"{g}:{model_id}" not in self.shut:
                    return {"gateway": g, "model_id": model_id}
        g, m = next((g, m) for g, m in self._flat
                    if f"{g}:{m['id']}" not in self.shut)
        return {"gateway": g, "model_id": m["id"]}

    def report_success(self, gateway, model_id):
        self.shut.discard(f"{gateway}:{model_id}")
        self.revived.append(model_id)


class FakeSDK:
    def __init__(self, rotator):
        self.rotator = rotator
        self.calls = []

    def run_stage(self, stage, messages, **kw):
        self.calls.append((stage, self.rotator.peek_model(stage)["model_id"]))
        return {"model": self.calls[-1][1],
                "choices": [{"message": {"content": "ok"}}]}


def _client(deck, pins, shut_down=(), strict=True):
    tk = object.__new__(TurnKeyClient)
    tk._sdk = FakeSDK(FakeRotator(deck, pins, shut_down))
    tk._pea_kw = False
    tk._pins = pins
    tk._strict_pins = strict
    return tk


DECK = [("google", "gemini"), ("openrouter", "qwen"), ("runpod", "my-7b")]
MSGS = [{"role": "user", "content": "hi"}]


def test_a_pinned_stage_is_served_by_its_pinned_model():
    tk = _client(DECK, {"distiller": ["my-7b"]})
    tk.call("distiller", MSGS)
    assert tk._sdk.calls == [("distiller", "my-7b")]


def test_a_pinned_model_the_breaker_shut_down_is_brought_back_not_replaced():
    tk = _client(DECK, {"distiller": ["my-7b"]}, shut_down={"runpod:my-7b"})
    tk.call("distiller", MSGS)
    assert tk._sdk.rotator.revived == ["my-7b"]
    assert tk._sdk.calls == [("distiller", "my-7b")]         # never the rest of the deck


def test_a_pin_the_deck_does_not_hold_refuses_the_call():
    tk = _client(DECK, {"distiller": ["not-in-the-deck"]})
    with pytest.raises(PinUnavailable, match="not-in-the-deck"):
        tk.call("distiller", MSGS)
    assert tk._sdk.calls == []                                # nothing was sent anywhere


def test_an_unpinned_stage_still_rotates_the_deck():
    tk = _client(DECK, {"distiller": ["my-7b"]})
    tk.call("introbeaver", MSGS)
    assert tk._sdk.calls[0][0] == "introbeaver"               # no pin to hold, no refusal


def test_strict_off_restores_the_old_fallback():
    tk = _client(DECK, {"distiller": ["not-in-the-deck"]}, strict=False)
    tk.call("distiller", MSGS)
    assert tk._sdk.calls == [("distiller", "gemini")]


def test_pinned_models_names_a_stage_by_its_pipeline_name():
    tk = _client(DECK, {"distiller": ["my-7b"]})
    assert tk.pinned_models("receptor") == ["my-7b"]          # receptor is the distiller
    assert tk.pinned_models("scout") == []


# ── the retry loop ─────────────────────────────────────────────────────────

class _Flaky:
    def __init__(self, fails, pins):
        self.fails, self.n = fails, 0
        self._pins, self._strict_pins = pins, True

    def pinned_models(self, stage):
        return list(self._pins.get(stage, []))

    def call(self, stage, messages, **kw):
        self.n += 1
        if self.n <= self.fails:
            raise ConnectionError("endpoint warming up")
        return "result"


def _engine(tk):
    ec = object.__new__(EngineClient)
    ec._tk = tk
    ec.slept = []
    ec._sleep = ec.slept.append
    ec.model_for = lambda stage: "m"
    return ec


def test_a_pin_failure_is_final_not_retried():
    class Refuses:
        n = 0

        def call(self, *a, **k):
            Refuses.n += 1
            raise PinUnavailable("nope")
    with pytest.raises(PinUnavailable):
        _engine(Refuses())._call_with_retry("distiller", MSGS)
    assert Refuses.n == 1


def test_a_pinned_stage_waits_between_retries_and_stays_on_its_model():
    tk = _Flaky(fails=3, pins={"distiller": ["my-7b"]})
    ec = _engine(tk)
    assert ec._call_with_retry("distiller", MSGS) == "result"
    assert ec.slept == [2.0, 4.0, 6.0]                        # backing off, same model


def test_an_unpinned_stage_never_waits():
    tk = _Flaky(fails=3, pins={"distiller": ["my-7b"]})
    ec = _engine(tk)
    assert ec._call_with_retry("introbeaver", MSGS) == "result"
    assert ec.slept == []                                     # rotation, not waiting


# ── the profile ────────────────────────────────────────────────────────────

def test_pins_come_from_the_profile(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"stages": [
        {"stageName": "distiller", "pinnedModels": ["my-7b"]},
        {"stageName": "scout", "pinnedModels": []},
        {"stage": "olc", "pinnedModels": ["a", "b"]}]}))
    assert pins_from_profile(p) == {"distiller": ["my-7b"], "olc": ["a", "b"]}
    assert pins_from_profile(tmp_path / "missing.json") == {}


def test_a_profile_with_pins_stops_the_sdk_retrying_into_the_deck(tmp_path, monkeypatch):
    deck = [{"uid": "c1", "modelId": "gemini-3.1-flash-lite", "provider": "google"}]
    p = tmp_path / "p.json"
    p.write_text(json.dumps({
        "version": 1, "name": "t", "deck": deck,
        "stages": [{"stageName": "distiller", "pinnedModels": ["gemini-3.1-flash-lite"],
                    "useDeck": True}],
        "pipeline": {"mode": "sequential", "workers": 1, "timeout": 180, "maxRetries": 10}}))
    monkeypatch.delenv("SOMA_STRICT_PINS", raising=False)
    assert TurnKeyClient(str(p))._sdk.config.max_retries == 0
    monkeypatch.setenv("SOMA_STRICT_PINS", "0")
    assert TurnKeyClient(str(p))._sdk.config.max_retries == 10


# ── a rate limit is waited out for longer than a dropped connection ─────────

class _RateLimited(_Flaky):
    def call(self, stage, messages, **kw):
        self.n += 1
        if self.n <= self.fails:
            raise RuntimeError("HTTP 429: You exceeded your current quota")
        return "result"


def test_a_rate_limit_on_a_pinned_stage_is_waited_out_across_the_quota_window():
    ec = _engine(_RateLimited(fails=3, pins={"distiller": ["my-7b"]}))
    assert ec._call_with_retry("distiller", MSGS) == "result"
    assert ec.slept == [20.0, 35.0, 50.0]                    # past a one-minute window by the third wait


def test_the_wait_is_capped():
    ec = _engine(_RateLimited(fails=9, pins={"distiller": ["my-7b"]}))
    ec._call_with_retry("distiller", MSGS)
    assert max(ec.slept) == 75.0
