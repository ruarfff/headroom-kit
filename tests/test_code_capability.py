"""Installed Headroom must select the AST compressor without changing OpenAI routes."""

import os
import tempfile
import unittest

from headroom_kit.proxy import protect_unretrievable_routes


def code_fixture() -> str:
    body = "\n".join(f"    value = value + {index}" for index in range(30))
    return "\n".join(
        f"def function_{index}(value):\n"
        f'    """Long docstring for function {index} describing unused helper behaviour."""\n'
        f"{body}\n"
        f"    return value\n"
        for index in range(40)
    )


class CodeCapability(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.previous = {
            key: os.environ.get(key) for key in ("HOME", "HEADROOM_WORKSPACE_DIR", "HF_HUB_OFFLINE")
        }
        os.environ["HOME"] = temporary.name
        os.environ["HEADROOM_WORKSPACE_DIR"] = temporary.name
        os.environ["HF_HUB_OFFLINE"] = "1"
        self.addCleanup(self.restore_env)

    def restore_env(self) -> None:
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_code_aware_compressor_is_selected_for_source(self) -> None:
        from headroom.transforms.code_compressor import _check_tree_sitter_available
        from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

        self.assertTrue(_check_tree_sitter_available())
        code = code_fixture()
        router = ContentRouter(
            ContentRouterConfig(
                enable_code_aware=True,
                prefer_code_aware_for_code=True,
                lossless=False,
            )
        )
        compressor = router._get_code_compressor()
        self.assertEqual(type(compressor).__name__, "CodeAwareCompressor")
        result = router.compress(code)
        self.assertEqual(result.strategy_chain, ["code_aware"])
        self.assertLess(len(result.compressed), len(code))
        self.assertIn("def function_0", result.compressed)
        self.assertIn("lines omitted", result.compressed)

    def test_openai_routes_stay_lossless(self) -> None:
        from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

        code = code_fixture()
        kept = ContentRouter(
            ContentRouterConfig(
                enable_code_aware=True,
                prefer_code_aware_for_code=True,
                lossless=True,
            )
        ).compress(code)
        self.assertEqual(kept.strategy_chain, ["passthrough"])
        self.assertEqual(kept.compressed, code)
        calls: list[tuple[str, dict[str, object]]] = []

        class Proxy:
            openai_pipeline = "original"

            def _derived_compress_pipeline(self, key: str, **overrides: object) -> str:
                calls.append((key, overrides))
                return "lossless-pipeline"

        app = type("App", (), {"state": type("State", (), {"proxy": Proxy()})()})()
        protect_unretrievable_routes(app)
        self.assertEqual(calls, [("kit-openai-lossless", {"lossless": True})])
        self.assertEqual(app.state.proxy.openai_pipeline, "lossless-pipeline")


if __name__ == "__main__":
    unittest.main()
