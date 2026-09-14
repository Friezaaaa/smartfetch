import logging
import json
import unittest
from unittest.mock import patch

from smartfetch.model_routing import BenchmarkModelRouter, GEMINI_WORKLOADS
from smartfetch.provider_health import ProviderCircuitBreaker, ProviderConfig
from smartfetch.v111_service import V111RuntimeConfig, load_v111_activation


def complete_runtime(**overrides):
    values = {
        "exa_config": ProviderConfig("exa", "test-exa-key", 10.0, 65_536, 50_000),
        "gemini_config": ProviderConfig("gemini", "test-gemini-key", 30.0, 65_536, 150_000),
        "exa_circuit": ProviderCircuitBreaker(provider="exa"),
        "gemini_circuit": ProviderCircuitBreaker(provider="gemini"),
        "router": BenchmarkModelRouter(routes={
            workload: "gemini-3.5-flash-lite" for workload in GEMINI_WORKLOADS
        }),
        "max_request_cost_micro_usd": 200_000,
    }
    values.update(overrides)
    return V111RuntimeConfig(**values)


class V111ActivationTests(unittest.TestCase):
    def test_only_exact_lowercase_true_requests_activation(self):
        for value in (None, "", "false", "TRUE", " true ", "1", "yes"):
            with self.subTest(value=value):
                environ = {} if value is None else {"SMARTFETCH_V111_ENABLED": value}
                activation = load_v111_activation(environ, complete_runtime())
                self.assertFalse(activation.enabled)
                self.assertIsNone(activation.service)

        activation = load_v111_activation(
            {"SMARTFETCH_V111_ENABLED": "true"}, complete_runtime()
        )
        self.assertTrue(activation.enabled)
        self.assertIsNotNone(activation.service)

    def test_invalid_flag_logs_only_bounded_code_and_never_value(self):
        canary = "Never-Log-This-Flag-Canary"
        with self.assertLogs("smartfetch.configuration", logging.WARNING) as captured:
            activation = load_v111_activation(
                {"SMARTFETCH_V111_ENABLED": canary}, complete_runtime()
            )
        self.assertFalse(activation.enabled)
        output = "\n".join(captured.output)
        self.assertIn("v111_config_invalid", output)
        self.assertNotIn(canary, output)

    def test_invalid_flag_warning_is_structured_and_bounded(self):
        with patch("smartfetch.v111_service._CONFIG_LOGGER.warning") as warning:
            load_v111_activation(
                {"SMARTFETCH_V111_ENABLED": "INVALID-CANARY"},
                complete_runtime(),
            )
        warning.assert_called_once()
        payload = json.loads(warning.call_args.args[0])
        self.assertEqual(payload, {
            "event": "v111_config_invalid",
            "level": "WARNING",
        })

    def test_missing_or_invalid_runtime_disables_all_v111_capabilities(self):
        incomplete = (
            None,
            complete_runtime(
                exa_config=ProviderConfig("exa", None, 10.0, 65_536, 50_000)
            ),
            complete_runtime(
                gemini_config=ProviderConfig("gemini", "", 30.0, 65_536, 150_000)
            ),
            complete_runtime(
                router=BenchmarkModelRouter()
            ),
            complete_runtime(max_request_cost_micro_usd=0),
        )
        for runtime in incomplete:
            with self.subTest(runtime=runtime):
                activation = load_v111_activation(
                    {"SMARTFETCH_V111_ENABLED": "true"}, runtime
                )
                self.assertFalse(activation.enabled)
                self.assertIsNone(activation.service)

    def test_activation_is_a_restart_snapshot(self):
        environ = {"SMARTFETCH_V111_ENABLED": "false"}
        activation = load_v111_activation(environ, complete_runtime())
        environ["SMARTFETCH_V111_ENABLED"] = "true"
        self.assertFalse(activation.enabled)
        self.assertIsNone(activation.service)


if __name__ == "__main__":
    unittest.main()
