from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monitor_agent.core.config import load_config


class ConfigImportsTests(unittest.TestCase):
    def test_imports_are_merged_and_main_overrides_imported_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frag_sources = root / "sources.yaml"
            frag_llm = root / "llm.yaml"
            main = root / "config.yaml"

            frag_sources.write_text(
                """
sources:
  rss:
    - name: "Feed A"
      url: "https://example.com/rss"
      max_items: 10
  playwright: []
""".strip(),
                encoding="utf-8",
            )
            frag_llm.write_text(
                """
llm:
  provider: "openai"
  model: "deepseek-chat"
  base_url: "https://api.deepseek.com/v1"
""".strip(),
                encoding="utf-8",
            )
            main.write_text(
                """
imports:
  - "./sources.yaml"
  - "./llm.yaml"
domain: "AI Infrastructure"
schedule:
  timezone: "Asia/Shanghai"
  times: ["07:00", "22:00"]
llm:
  model: "deepseek-reasoner"
storage:
  root_dir: "./data"
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config(str(main))
            self.assertEqual(cfg.domain, "AI Infrastructure")
            self.assertEqual(len(cfg.sources.rss), 1)
            self.assertEqual(cfg.sources.rss[0].name, "Feed A")
            self.assertEqual(cfg.llm.base_url, "https://api.deepseek.com/v1")
            # Main file overrides imported fragment value.
            self.assertEqual(cfg.llm.model, "deepseek-reasoner")

    def test_domain_profiles_expand_sources_with_dynamic_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            main = root / "config.yaml"
            main.write_text(
                """
domain: "AI Infrastructure"
domain_profiles:
  - domain: "AI Infrastructure"
    focus_areas: ["GPU supply"]
    entities: ["NVIDIA"]
    keywords: ["Blackwell"]
    source_links:
      - "https://hnrss.org/frontpage"
      - "https://openai.com/news/"
schedule:
  timezone: "Asia/Shanghai"
  times: ["07:00", "22:00"]
storage:
  root_dir: "./data"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(str(main))
            self.assertEqual(cfg.domain_scope, ["AI Infrastructure"])
            self.assertEqual(cfg.domain_profiles[0].domain, "AI Infrastructure")
            self.assertEqual(len(cfg.sources.rss), 1)
            self.assertEqual(cfg.sources.rss[0].url, "https://hnrss.org/frontpage")
            self.assertEqual(cfg.sources.rss[0].max_items, 35)
            self.assertEqual(len(cfg.sources.playwright), 1)
            self.assertEqual(cfg.sources.playwright[0].url, "https://openai.com/news/")
            self.assertFalse(cfg.sources.playwright[0].force_playwright)

    def test_domain_profiles_expand_playwright_advanced_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            main = root / "config.yaml"
            main.write_text(
                """
domain: "AI Infrastructure"
domain_profiles:
  - domain: "AI Infrastructure"
    source_links:
      - url: "https://example.com/news"
        type: "playwright"
        follow_links_enabled: true
        max_links_per_source: 7
        same_domain_only: true
        article_url_patterns: ["/article/"]
        exclude_url_patterns: ["/video/"]
filtering:
  importance_threshold: 0.6
storage:
  root_dir: "./data"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(str(main))
            self.assertEqual(len(cfg.sources.playwright), 1)
            source = cfg.sources.playwright[0]
            self.assertEqual(source.url, "https://example.com/news")
            self.assertTrue(source.follow_links_enabled)
            self.assertEqual(source.max_links_per_source, 7)
            self.assertTrue(source.same_domain_only)
            self.assertEqual(source.article_url_patterns, ["/article/"])
            self.assertEqual(source.exclude_url_patterns, ["/video/"])
            self.assertTrue(source.force_playwright)

    def test_domain_profiles_expand_inline_source_type_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            main = root / "config.yaml"
            main.write_text(
                """
domain: "AI Infrastructure"
domain_profiles:
  - domain: "AI Infrastructure"
    source_links:
      - "https://www.wsj.com/tech | playwright | WSJ Tech"
      - "rss: https://hnrss.org/frontpage"
storage:
  root_dir: "./data"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(str(main))
            self.assertEqual(len(cfg.sources.playwright), 1)
            self.assertEqual(cfg.sources.playwright[0].url, "https://www.wsj.com/tech")
            self.assertEqual(cfg.sources.playwright[0].name, "WSJ Tech")
            self.assertTrue(cfg.sources.playwright[0].force_playwright)
            self.assertEqual(len(cfg.sources.rss), 1)
            self.assertEqual(cfg.sources.rss[0].url, "https://hnrss.org/frontpage")

    def test_domain_profiles_allow_non_forced_playwright_for_html_first_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            main = root / "config.yaml"
            main.write_text(
                """
domain: "AI Infrastructure"
domain_profiles:
  - domain: "AI Infrastructure"
    source_links:
      - url: "https://example.com/news"
        type: "playwright"
        force_playwright: false
storage:
  root_dir: "./data"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(str(main))
            self.assertEqual(len(cfg.sources.playwright), 1)
            self.assertFalse(cfg.sources.playwright[0].force_playwright)


if __name__ == "__main__":
    unittest.main()
