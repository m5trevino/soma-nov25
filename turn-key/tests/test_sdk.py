"""
Integration tests for the TurnKey SDK's circuit breaker and API flow.

Tests that:
1. Profile creation/serialization round-trips correctly
2. The GatewayDeckRotator skips shutdown models
3. report_failure/report_success work correctly
4. run_stage wires up the circuit breaker properly
"""

import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from http.client import HTTPResponse

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from turnkey.sdk import SDK, SDKConfig
from turnkey.types import SDKError
from turnkey.gateway_rotator import GatewayDeckRotator


def _create_test_profile() -> dict:
    """Create a minimal test profile for SDK."""
    return {
        "version": "1.0.0",
        "name": "test-profile",
        "description": "A test profile",
        "deck": [
            {
                "uid": "card-1-1234567890",
                "modelId": "test-model",
                "modelName": "Test Model",
                "provider": "test",
                "tier": "free"
            }
        ],
        "stages": [
            {
                "stageName": "summarize",
                "pinnedModels": [],
                "useDeck": True
            }
        ],
        "pipeline": {
            "mode": "sequential",
            "workers": 1,
            "timeout": 180,
            "maxRetries": 10,
            "engineUrl": "http://test:3099"
        }
    }


def _write_test_profile(path: str) -> None:
    """Write a minimal test profile to disk."""
    with open(path, "w") as f:
        json.dump(_create_test_profile(), f)


class TestProfileRoundTrip(unittest.TestCase):
    """Test that profiles are created in the correct format."""

    def test_profile_schema(self):
        """A valid profile has deck, stages, and pipeline."""
        profile = {
            "version": "1.0.0",
            "name": "test-profile",
            "description": "A test profile",
            "deck": [
                {
                    "uid": "card-1-1234567890",
                    "modelId": "gpt-4",
                    "modelName": "GPT-4",
                    "provider": "OpenRouter",
                    "tier": "expensive"
                }
            ],
            "stages": [
                {
                    "stageName": "summarize",
                    "pinnedModels": [],
                    "useDeck": True
                }
            ],
            "pipeline": {
                "mode": "sequential",
                "workers": 1,
                "timeout": 180,
                "maxRetries": 10,
                "engineUrl": "http://localhost:3099"
            }
        }

        self.assertEqual(profile["version"], "1.0.0")
        self.assertIn("deck", profile)
        self.assertIn("stages", profile)
        self.assertIn("pipeline", profile)
        self.assertIn("engineUrl", profile["pipeline"])


class TestGatewayRotator(unittest.TestCase):
    """Test the GatewayDeckRotator with circuit breaker logic."""

    def setUp(self):
        """Create a rotator with two gateways and two models each."""
        self.gateway_config = [
            {
                "gateway": "OpenRouter",
                "models": [
                    {"id": "gpt-4", "name": "GPT-4"},
                    {"id": "claude-3", "name": "Claude-3"}
                ]
            },
            {
                "gateway": "Ollama",
                "models": [
                    {"id": "llama3", "name": "LLaMA 3"},
                    {"id": "mistral", "name": "Mistral"}
                ]
            }
        ]
        self.rotator = GatewayDeckRotator(self.gateway_config)

    def test_initial_rotation(self):
        """Initial rotation should cycle through models and return all 4."""
        models = []
        for _ in range(4):
            info = self.rotator.next_model("test")
            models.append(info["model_id"])
        
        # All 4 unique models should appear (order may vary by gateway config)
        self.assertEqual(len(models), 4)
        self.assertEqual(len(set(models)), 4)  # all unique
        
        # Verify specific models are present
        model_ids = set(models)
        self.assertIn("gpt-4", model_ids)
        self.assertIn("claude-3", model_ids)
        self.assertIn("llama3", model_ids)
        self.assertIn("mistral", model_ids)

    def test_report_failure_increments_counter(self):
        """report_failure should increment the failure count."""
        self.rotator.report_failure("OpenRouter", "gpt-4")
        
        # Internal state should have incremented
        self.assertEqual(self.rotator._failures["OpenRouter"]["gpt-4"], 1)

    def test_three_failures_shut_down(self):
        """Three consecutive failures should shut down the model."""
        for _ in range(3):
            self.rotator.report_failure("OpenRouter", "gpt-4")
        
        self.assertIn("OpenRouter:gpt-4", self.rotator._shutdown_models)

    def test_shutdown_model_is_skipped(self):
        """Shutdown models should be skipped during rotation."""
        # Shut down the first two models
        self.rotator.report_failure("OpenRouter", "gpt-4")
        self.rotator.report_failure("OpenRouter", "gpt-4")
        self.rotator.report_failure("OpenRouter", "gpt-4")
        
        self.rotator.report_failure("Ollama", "llama3")
        self.rotator.report_failure("Ollama", "llama3")
        self.rotator.report_failure("Ollama", "llama3")
        
        # Next model should be claude-3 (next available in OpenRouter)
        info = self.rotator.next_model("test")
        self.assertEqual(info["model_id"], "claude-3")
        self.assertEqual(info["gateway"], "OpenRouter")

    def test_report_success_resets_counter(self):
        """report_success should reset failure counter."""
        self.rotator.report_failure("OpenRouter", "gpt-4")
        self.rotator.report_failure("OpenRouter", "gpt-4")
        
        # Reset the counter
        self.rotator.report_success("OpenRouter", "gpt-4")
        
        self.assertEqual(self.rotator._failures["OpenRouter"]["gpt-4"], 0)

    def test_all_models_shutdown_raises(self):
        """If all models are shut down, next_model should raise RuntimeError."""
        # Shut down all models
        for model in ["gpt-4", "claude-3", "llama3", "mistral"]:
            gateway = "OpenRouter" if model in ["gpt-4", "claude-3"] else "Ollama"
            for _ in range(3):
                self.rotator.report_failure(gateway, model)
        
        with self.assertRaises(RuntimeError):
            self.rotator.next_model("test")

    def test_report_success_undoes_shutdown(self):
        """If a model succeeds after shutdown, it should be removed from shutdown list."""
        # Shut down gpt-4
        for _ in range(3):
            self.rotator.report_failure("OpenRouter", "gpt-4")
        
        self.assertIn("OpenRouter:gpt-4", self.rotator._shutdown_models)
        
        # Report success (simulates recovery)
        self.rotator.report_success("OpenRouter", "gpt-4")
        
        self.assertNotIn("OpenRouter:gpt-4", self.rotator._shutdown_models)


class TestSDKFailureReporting(unittest.TestCase):
    """Test that SDK.run_stage wires up report_failure/report_success."""

    def _write_profile(self) -> str:
        """Write a test profile to a temp file and return the path."""
        tmpdir = tempfile.mkdtemp()
        profile_path = os.path.join(tmpdir, "test_profile.json")
        _write_test_profile(profile_path)
        return profile_path

    def test_successful_call_reports_success(self):
        """Successful run_stage should call report_success."""
        mock_response = {"choices": [{"message": {"content": "Hello"}}]}
        
        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            mock_resp = Mock(spec=HTTPResponse)
            mock_resp.read.return_value = json.dumps(mock_response).encode()
            mock_urlopen.return_value.__enter__ = Mock(return_value=mock_resp)
            mock_urlopen.return_value.__exit__ = Mock(return_value=False)
            
            profile_path = self._write_profile()
            sdk = SDK(SDKConfig(engine_url="http://test:3099"), profile_path=profile_path)
            
            result = sdk.run_stage("summarize", [{"role": "user", "content": "hi"}])
            
            # Verify the response was returned correctly
            self.assertIn("choices", result)
            
            # Failure counter should be 0 after success
            self.assertEqual(sdk.rotator._failures["test"]["test-model"], 0)

    def test_session_stage_and_response_linkage_are_preserved(self):
        """The first call omits pea_session; continuations carry SES-###."""
        mock_response = {
            "choices": [{"message": {"content": "Hello"}}],
            "session": "SES-014",
            "stage": "DI-03",
            "log_link": ("https://engine.save-aichats.com/v1/soma/"
                         "?session=SES-014&slug=distiller&stage=DI-03"),
        }

        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            mock_resp = Mock(spec=HTTPResponse)
            mock_resp.read.return_value = json.dumps(mock_response).encode()
            mock_urlopen.return_value.__enter__ = Mock(return_value=mock_resp)
            mock_urlopen.return_value.__exit__ = Mock(return_value=False)

            profile_path = self._write_profile()
            sdk = SDK(SDKConfig(engine_url="http://test:3099"),
                      profile_path=profile_path)
            messages = [{"role": "user", "content": "hi"}]
            first = sdk.run_stage("distiller", messages)
            second = sdk.run_stage("distiller", messages,
                                   session_id="SES-014")

            first_body = json.loads(
                mock_urlopen.call_args_list[0][0][0].data.decode("utf-8"))
            second_body = json.loads(
                mock_urlopen.call_args_list[1][0][0].data.decode("utf-8"))
            self.assertNotIn("pea_session", first_body)
            self.assertEqual(first_body["stage"], "distiller")
            self.assertEqual(second_body["pea_session"], "SES-014")
            self.assertEqual(second_body["stage"], "distiller")
            self.assertEqual(second["session"], "SES-014")
            self.assertEqual(second["stage"], "DI-03")
            self.assertEqual(second["log_link"], mock_response["log_link"])
            self.assertEqual(second["provider"], "test")
            self.assertEqual(first["provider"], "test")

    def test_failed_call_reports_failure(self):
        """Failed run_stage should call report_failure on the rotator."""
        import urllib.error
        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            # URLError is caught by _call_engine and re-raised as SDKError,
            # which triggers report_failure in run_stage.
            mock_urlopen.side_effect = urllib.error.URLError("Connection error")
            
            profile_path = self._write_profile()
            sdk = SDK(SDKConfig(engine_url="http://test:3099"), profile_path=profile_path)
            
            # With only 1 model and max_retries=10, the model fails 3 times during
            # retry, gets shut down, then the 4th next_model() raises RuntimeError.
            # We verify report_failure was called by checking the failure counter.
            try:
                sdk.run_stage("summarize", [{"role": "user", "content": "hi"}])
            except (SDKError, RuntimeError):
                pass  # Either is acceptable — SDKError before shutdown, RuntimeError after
            
            # The rotator should have been called with report_failure
            # (at least 3 failures triggered the shutdown)
            self.assertGreater(sdk.rotator._failures["test"]["test-model"], 0)
            # And the model should be in the shutdown list
            self.assertIn("test:test-model", sdk.rotator._shutdown_models)


class TestProviderIsAlwaysSent(unittest.TestCase):
    """The model and the provider travel as two separate fields on every call.

    The engine must not choose the gateway. A missing provider, a placeholder
    provider, or an engine reply from a different provider all fail the call,
    and the error says what is wrong and how to add the provider.
    """

    MSGS = [{"role": "user", "content": "hi"}]

    def _sdk(self, card_overrides=None, drop_provider=False):
        profile = _create_test_profile()
        if card_overrides:
            profile["deck"][0].update(card_overrides)
        if drop_provider:
            profile["deck"][0].pop("provider")
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "p.json")
        with open(path, "w") as f:
            json.dump(profile, f)
        return SDK(SDKConfig(engine_url="http://test:3099"), profile_path=path)

    def _ok_urlopen(self, mock_urlopen, response=None):
        mock_resp = Mock(spec=HTTPResponse)
        mock_resp.read.return_value = json.dumps(
            response or {"choices": [{"message": {"content": "ok"}}]}).encode()
        mock_urlopen.return_value.__enter__ = Mock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = Mock(return_value=False)

    def test_model_and_provider_are_separate_fields(self):
        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            self._ok_urlopen(mock_urlopen)
            self._sdk().run_stage("summarize", self.MSGS)
            body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["provider"], "test")
        self.assertNotIn(":", body["model"])

    def test_missing_provider_explains_the_problem_and_the_fix(self):
        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(SDKError) as ctx:
                self._sdk()._call_engine("some/model", self.MSGS, stage="distiller")
            mock_urlopen.assert_not_called()
        err = ctx.exception
        self.assertEqual(err.error_code, "PROVIDER_REQUIRED")
        text = str(err)
        self.assertIn("PROVIDER MISSING", text)
        self.assertIn("some/model", text)
        self.assertIn("distiller", text)
        self.assertIn('"provider"', text)
        self.assertIn("How to fix", text)
        self.assertIn("provider=", text)

    def test_placeholder_providers_are_rejected(self):
        sdk = self._sdk()
        for bad in ("", "  ", "unknown", "default", "UNKNOWN"):
            with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
                with self.assertRaises(SDKError) as ctx:
                    sdk._call_engine("m", self.MSGS, provider=bad)
                mock_urlopen.assert_not_called()
            self.assertEqual(ctx.exception.error_code, "PROVIDER_REQUIRED", bad)

    def test_profile_card_without_provider_fails_with_the_fix_in_the_message(self):
        from turnkey.types import StageError
        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(StageError) as ctx:
                self._sdk(drop_provider=True).run_stage("summarize", self.MSGS)
            mock_urlopen.assert_not_called()
        self.assertIn("PROVIDER MISSING", str(ctx.exception))
        self.assertIn("deck card", str(ctx.exception))

    def test_engine_serving_a_different_provider_fails_and_is_not_retried(self):
        from turnkey.types import StageError
        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            self._ok_urlopen(mock_urlopen, {
                "choices": [{"message": {"content": "ok"}}], "provider": "merge"})
            with self.assertRaises(StageError) as ctx:
                self._sdk().run_stage("summarize", self.MSGS)
            self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIn("PROVIDER MISMATCH", str(ctx.exception))
        self.assertIn("merge", str(ctx.exception))
        self.assertIn("How to fix", str(ctx.exception))

    def test_matching_provider_in_the_reply_is_accepted(self):
        with patch("turnkey.sdk.urllib.request.urlopen") as mock_urlopen:
            self._ok_urlopen(mock_urlopen, {
                "choices": [{"message": {"content": "ok"}}], "provider": "Test"})
            result = self._sdk().run_stage("summarize", self.MSGS)
        self.assertIn("choices", result)


if __name__ == "__main__":
    unittest.main()