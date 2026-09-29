"""Classify live share QA without launching agents or GUI apps."""

import os
import subprocess
import unittest

import qa_share


def result(code: int, stderr: str, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class QaShareTests(unittest.TestCase):
    def test_missing_tools_lists_clis_only(self) -> None:
        previous = os.environ["PATH"]
        os.environ["PATH"] = "/nonexistent"
        try:
            missing = qa_share.missing_tools()
        finally:
            os.environ["PATH"] = previous
        self.assertEqual(
            {name for name in missing if name != "python3.13"},
            {"codex", "copilot", "pi", "opencode"},
        )

    def test_provider_quota_markers(self) -> None:
        self.assertTrue(qa_share.provider_quota("insufficient_quota"))
        self.assertTrue(qa_share.provider_quota("Rate limit reached"))
        self.assertTrue(qa_share.provider_quota("HTTP 429 too many requests"))
        self.assertTrue(qa_share.provider_quota("You've hit your usage limit"))
        self.assertFalse(qa_share.provider_quota("Started Headroom 0.39.1 on port 1"))

    def test_quota_after_share_is_not_a_kit_failure(self) -> None:
        first = result(1, "Started Headroom 0.39.1 on port 1.\ninsufficient_quota\n")
        second = result(1, "Reusing Headroom 0.39.1 on port 1.\nRate limit reached\n")
        self.assertIsNone(qa_share.share_error("pi", first, second))
        self.assertTrue(qa_share.client_ok(first))

    def test_missing_start_or_reuse_is_a_kit_failure(self) -> None:
        first = result(1, "agent crashed before proxy\n")
        self.assertIn("first launch failed", qa_share.share_error("codex", first))
        started = result(0, "Started Headroom 0.39.1 on port 1.\n")
        reused_badly = result(1, "Started Headroom 0.39.1 on port 2.\n")
        self.assertIn("did not reuse", qa_share.share_error("codex", started, reused_badly))
        already = result(0, "Reusing Headroom 0.39.1 on port 1.\n")
        self.assertIsNone(qa_share.share_error("pi-copilot", already))
        self.assertIsNone(qa_share.share_error("pi-copilot", already, already))

    def test_github_copilot_cases_use_shared_copilot_wrappers(self) -> None:
        self.assertEqual(qa_share.wrapper("pi-copilot"), "pi-headroom")
        self.assertEqual(qa_share.wrapper("opencode-copilot"), "opencode-headroom")
        self.assertEqual(
            qa_share.arguments("pi-copilot", False)[:4],
            ["--provider", "github-copilot", "--model", "grok-4.6"],
        )
        self.assertEqual(
            qa_share.arguments("opencode-copilot", True),
            ["run", "--model", "github-copilot/gpt-6-astra", qa_share.PROMPT],
        )
        self.assertIn("pi-copilot", qa_share.CASES)
        self.assertGreater(qa_share.CASES.index("pi-copilot"), qa_share.CASES.index("copilot"))
        self.assertTrue(
            qa_share.github_copilot_models("github-copilot/grok-4.6\nopencode/mimo-v2.5-free\n")
        )
        self.assertFalse(qa_share.github_copilot_models("opencode/mimo-v2.5-free\n"))

    def test_non_quota_client_error_after_share_still_fails(self) -> None:
        first = result(0, "Started Headroom 0.39.1 on port 1.\n")
        second = result(1, "Reusing Headroom 0.39.1 on port 1.\nmodel not found\n")
        self.assertIn(
            "client failed after proxy share", qa_share.share_error("copilot", first, second)
        )
