import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from smartfetch.config import SERVICE_VERSION
from smartfetch.model_routing import BenchmarkModelRouter
from smartfetch.provider_controls import RequestCostBudget
from smartfetch.provider_health import ProviderAdapterError, ProviderCircuitBreaker, ProviderConfig
from smartfetch.provider_types import AnswerRequest
from smartfetch.providers.gemini import GeminiProvider


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_FILES = (
    ROOT / "smartfetch" / "provider_health.py",
    ROOT / "smartfetch" / "provider_controls.py",
    ROOT / "smartfetch" / "model_routing.py",
    ROOT / "smartfetch" / "providers" / "exa.py",
    ROOT / "smartfetch" / "providers" / "gemini.py",
)


class ProviderStage2ScopeTests(unittest.IsolatedAsyncioTestCase):
    def test_stage2_modules_do_not_read_environment_or_import_runtime_layers(self) -> None:
        forbidden_imports = {
            "smartfetch.server",
            "smartfetch.payments",
            "smartfetch.core",
            "smartfetch.browser_fetch",
            "smartfetch.security",
            "smartfetch.mcp_server",
        }
        for path in PROVIDER_FILES:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            self.assertTrue(forbidden_imports.isdisjoint(imported), path.name)
            self.assertNotIn("os.environ", source)
            self.assertNotIn("os.getenv", source)
            self.assertNotIn("getenv(", source)

    async def test_absent_gemini_configuration_does_not_construct_sdk_or_call_network(self) -> None:
        config = ProviderConfig("gemini", None, 30.0, 65_536, 10_000)
        with patch("smartfetch.providers.gemini.genai.Client") as client:
            provider = GeminiProvider(
                config=config,
                circuit=ProviderCircuitBreaker(provider="gemini"),
                budget=RequestCostBudget(max_total_micro_usd=10_000),
                router=BenchmarkModelRouter(),
                text_workload="structured_search",
            )
            with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
                await provider.synthesize_answer(AnswerRequest("q", (("s1", "source"),)))
            client.assert_not_called()

    def test_stage2_does_not_change_release_version_or_public_tool_wiring(self) -> None:
        self.assertEqual(SERVICE_VERSION, "1.10.6")
        mcp_source = (ROOT / "smartfetch" / "mcp_server.py").read_text(encoding="utf-8")
        for name in (
            "fetch_webpage",
            "webpage_to_markdown",
            "extract_webpage_text",
            "render_webpage",
        ):
            self.assertIn(name, mcp_source)
        self.assertNotIn("search_and_extract", mcp_source)
        self.assertNotIn("extract_structured_data", mcp_source)

    def test_finite_errors_do_not_retain_causes_or_canaries(self) -> None:
        canary = "provider-stage2-secret-canary"
        error = ProviderAdapterError("search_failed", provider="exa", cause=RuntimeError(canary))
        self.assertEqual(error.args, ("search_failed",))
        self.assertFalse(hasattr(error, "__cause__") and error.__cause__ is not None)
        self.assertNotIn(canary, repr(error))


if __name__ == "__main__":
    unittest.main()
