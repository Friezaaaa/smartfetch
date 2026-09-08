import unittest

from smartfetch.model_routing import (
    APPROVED_GEMINI_MODELS,
    GEMINI_WORKLOADS,
    BenchmarkModelRouter,
    ModelRoutingError,
)


class ModelRoutingTests(unittest.TestCase):
    def test_only_two_approved_models_and_finite_workloads_exist(self) -> None:
        self.assertEqual(
            APPROVED_GEMINI_MODELS,
            ("gemini-3.8-flash", "gemini-3.5-flash-lite"),
        )
        self.assertEqual(
            GEMINI_WORKLOADS,
            ("answer", "structured_search", "webpage", "image", "pdf", "audio", "video"),
        )

    def test_no_model_winner_is_declared_by_default(self) -> None:
        router = BenchmarkModelRouter()
        with self.assertRaisesRegex(ModelRoutingError, "model_route_unconfigured"):
            router.select("answer")

    def test_complete_benchmark_table_is_immutable_and_selects_profiles(self) -> None:
        routes = {
            workload: (
                "gemini-3.8-flash" if workload in {"answer", "video"} else "gemini-3.5-flash-lite"
            )
            for workload in GEMINI_WORKLOADS
        }
        router = BenchmarkModelRouter(routes=routes)
        routes["answer"] = "gemini-3.5-flash-lite"
        selection = router.select("answer")
        self.assertEqual(selection.model_id, "gemini-3.8-flash")
        self.assertEqual(selection.route_label, "flash")
        self.assertEqual(selection.thinking_level, "low")
        self.assertGreater(selection.max_output_tokens, 0)
        self.assertLessEqual(selection.max_output_tokens, 8192)

    def test_partial_or_extra_tables_fail_closed(self) -> None:
        with self.assertRaisesRegex(ModelRoutingError, "invalid_model_routing"):
            BenchmarkModelRouter(routes={"answer": "gemini-3.8-flash"})
        routes = {workload: "gemini-3.8-flash" for workload in GEMINI_WORKLOADS}
        routes["other"] = "gemini-3.8-flash"
        with self.assertRaisesRegex(ModelRoutingError, "invalid_model_routing"):
            BenchmarkModelRouter(routes=routes)

    def test_only_approved_override_is_accepted(self) -> None:
        router = BenchmarkModelRouter(override_model_id="gemini-3.5-flash-lite")
        self.assertEqual(router.select("pdf").model_id, "gemini-3.5-flash-lite")
        with self.assertRaisesRegex(ModelRoutingError, "invalid_model_routing"):
            BenchmarkModelRouter(override_model_id="caller-selected-model")

    def test_unknown_workloads_and_hostile_subclasses_fail_without_comparison_side_effects(self) -> None:
        class HostileString(str):
            def __hash__(self) -> int:
                raise RuntimeError("routing-canary")

        router = BenchmarkModelRouter(override_model_id="gemini-3.8-flash")
        for workload in ("other", HostileString("answer")):
            with self.assertRaisesRegex(ModelRoutingError, "invalid_model_routing"):
                router.select(workload)


if __name__ == "__main__":
    unittest.main()
