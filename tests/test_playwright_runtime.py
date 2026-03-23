from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monitor_agent.core.models import PlaywrightRuntimeConfig
from monitor_agent.ingestion_layer.playwright_ingestor import PlaywrightIngestor


class PlaywrightRuntimeTests(unittest.TestCase):
    def test_build_context_options_headless_without_extensions(self) -> None:
        runtime = PlaywrightRuntimeConfig(
            headless=True,
            channel=None,
            extension_paths=[],
            launch_args=["--disable-gpu"],
        )
        options = PlaywrightIngestor.build_context_options("/tmp/profile", runtime)
        self.assertTrue(options["headless"])
        self.assertEqual(options["user_data_dir"], "/tmp/profile")
        self.assertIn("--disable-gpu", options.get("args", []))

    def test_build_context_options_forces_headed_when_extensions_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ext_dir = Path(tmpdir) / "my_ext"
            ext_dir.mkdir(parents=True, exist_ok=True)
            runtime = PlaywrightRuntimeConfig(
                headless=True,
                channel="chrome",
                extension_paths=[str(ext_dir)],
                launch_args=[],
            )
            options = PlaywrightIngestor.build_context_options("/tmp/profile", runtime)
            self.assertFalse(options["headless"])
            self.assertEqual(options["channel"], "chrome")
            args = options.get("args", [])
            self.assertTrue(any(arg.startswith("--load-extension=") for arg in args))
            self.assertTrue(any(arg.startswith("--disable-extensions-except=") for arg in args))

    def test_goto_with_retry_recovers_from_extension_interrupt(self) -> None:
        class _FakeTab:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class _FakeContext:
            def __init__(self, pages) -> None:
                self.pages = pages

        class _FakePage:
            def __init__(self, errors: list[Exception], ext_tab: _FakeTab) -> None:
                self.url = "about:blank"
                self._errors = list(errors)
                self.goto_calls = 0
                self.context = _FakeContext([self, ext_tab])

            def goto(self, url: str, timeout: int, wait_until: str) -> None:
                _ = timeout, wait_until
                self.goto_calls += 1
                if self._errors:
                    raise self._errors.pop(0)
                self.url = url

        ext = _FakeTab("chrome-extension://abcd/options.html")
        page = _FakePage(
            errors=[Exception('Page.goto: interrupted by another navigation to "chrome-extension://abcd/options.html"')],
            ext_tab=ext,
        )
        PlaywrightIngestor._goto_with_retry(page, "https://example.com/news", 10_000)
        self.assertTrue(ext.closed)
        self.assertEqual(page.url, "https://example.com/news")
        self.assertGreaterEqual(page.goto_calls, 2)


if __name__ == "__main__":
    unittest.main()
