import threading
import unittest

from smartfetch.provider_health import (
    ProviderAdapterError,
    ProviderCircuitBreaker,
    ProviderConfig,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class ProviderHealthTests(unittest.TestCase):
    def test_configuration_is_bounded_and_redacts_secret(self) -> None:
        config = ProviderConfig(
            provider="exa",
            api_key="test-secret-canary",
            timeout_seconds=10.0,
            max_response_bytes=1_048_576,
            max_cost_micro_usd=20_000,
        )
        self.assertTrue(config.configured)
        self.assertNotIn("test-secret-canary", repr(config))
        self.assertNotIn("test-secret-canary", str(config))

        for kwargs in (
            {"provider": "other"},
            {"timeout_seconds": True},
            {"timeout_seconds": 0},
            {"max_response_bytes": 0},
            {"max_cost_micro_usd": -1},
        ):
            values = {
                "provider": "exa",
                "api_key": "key",
                "timeout_seconds": 10.0,
                "max_response_bytes": 1024,
                "max_cost_micro_usd": 20_000,
            }
            values.update(kwargs)
            with self.assertRaisesRegex(ValueError, "invalid_provider_config"):
                ProviderConfig(**values)

    def test_missing_configuration_fails_with_finite_error(self) -> None:
        config = ProviderConfig(
            provider="gemini",
            api_key=None,
            timeout_seconds=30.0,
            max_response_bytes=65_536,
            max_cost_micro_usd=40_000,
        )
        self.assertFalse(config.configured)
        with self.assertRaises(ProviderAdapterError) as caught:
            config.require_configured()
        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertEqual(str(caught.exception), "provider_unavailable")

    def test_circuit_opens_then_allows_exactly_one_half_open_probe(self) -> None:
        clock = FakeClock()
        circuit = ProviderCircuitBreaker(
            provider="exa",
            failure_threshold=1,
            recovery_seconds=30.0,
            clock=clock,
        )
        permit = circuit.begin_call()
        circuit.record_failure(permit)
        self.assertEqual(circuit.state, "open")
        with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
            circuit.begin_call()

        clock.now += 30.0
        probe = circuit.begin_call()
        self.assertTrue(probe.probe)
        with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
            circuit.begin_call()
        circuit.record_success(probe)
        self.assertEqual(circuit.state, "closed")
        self.assertFalse(circuit.begin_call().probe)

    def test_half_open_probe_is_thread_safe(self) -> None:
        clock = FakeClock()
        circuit = ProviderCircuitBreaker(
            provider="gemini",
            failure_threshold=1,
            recovery_seconds=1.0,
            clock=clock,
        )
        circuit.record_failure(circuit.begin_call())
        clock.now += 1.0
        barrier = threading.Barrier(8)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            try:
                circuit.begin_call()
            except ProviderAdapterError:
                outcome = "rejected"
            else:
                outcome = "allowed"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("allowed"), 1)
        self.assertEqual(outcomes.count("rejected"), 7)

    def test_permits_are_owner_bound_single_use_and_consumed_atomically(self) -> None:
        exa = ProviderCircuitBreaker(provider="exa")
        gemini = ProviderCircuitBreaker(provider="gemini")
        foreign = gemini.begin_call()
        with self.assertRaisesRegex(ValueError, "invalid_provider_permit"):
            exa.record_success(foreign)

        permit = exa.begin_call()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def finish() -> None:
            barrier.wait()
            try:
                exa.record_success(permit)
            except ValueError as exc:
                outcome = str(exc)
            else:
                outcome = "accepted"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=finish) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(outcomes.count("invalid_provider_permit"), 1)

        with self.assertRaisesRegex(ValueError, "invalid_provider_permit"):
            exa.record_failure(permit)

    def test_failure_codes_are_allowlisted_and_never_include_cause(self) -> None:
        secret = "provider-secret-canary"
        error = ProviderAdapterError("model_failed", provider="gemini", cause=RuntimeError(secret))
        self.assertEqual(error.code, "model_failed")
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error))
        with self.assertRaisesRegex(ValueError, "invalid_provider_error"):
            ProviderAdapterError("unexpected", provider="gemini")


if __name__ == "__main__":
    unittest.main()
