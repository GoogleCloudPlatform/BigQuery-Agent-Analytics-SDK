# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Hermetic tests for the Adaptive Workbench HTML showcase, docs, notebook, and OSS hygiene."""

from __future__ import annotations

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

DEMO_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DEMO_DIR.parents[1]

# Ensure demo package modules can be imported cleanly even when run standalone.
if str(DEMO_DIR) not in sys.path:
  sys.path.insert(0, str(DEMO_DIR))


class _CodeAsciiParser(HTMLParser):
  """Tracks text inside <code> and <pre> tags to enforce pure-ASCII contents."""

  def __init__(self) -> None:
    super().__init__(convert_charrefs=True)
    self._stack: list[str] = []
    self.non_ascii_hits: list[tuple[str, str]] = []

  def handle_starttag(
      self, tag: str, attrs: list[tuple[str, str | None]]
  ) -> None:
    del attrs
    low = tag.lower()
    if low in ("code", "pre"):
      self._stack.append(low)

  def handle_endtag(self, tag: str) -> None:
    low = tag.lower()
    if low in ("code", "pre") and self._stack:
      for idx in range(len(self._stack) - 1, -1, -1):
        if self._stack[idx] == low:
          del self._stack[idx]
          break

  def handle_data(self, data: str) -> None:
    if self._stack:
      bad = [ch for ch in data if ord(ch) >= 128]
      if bad:
        self.non_ascii_hits.append((self._stack[-1], "".join(bad[:8])))


# Generic regexes assembled dynamically so this test module never self-matches
# and contains zero real personal or internal identifiers.
_BANNED_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("corp_domain", re.compile(r"\.corp\." + r"google\.com", re.IGNORECASE)),
    ("corp_short", re.compile(r"\bx20" + r"web\b", re.IGNORECASE)),
    ("depot_path", re.compile(r"//depot/" + r"google3")),
    ("google3_import", re.compile(r"\bgoogle3" + r"\.")),
    ("home_dir", re.compile(r"/usr/local/" + r"google/home/")),
    ("personal_dev_project", re.compile(r"\b[a-z][a-z0-9-]*-dev-\d{5,}\b")),
    (
        "dot_gemini_internal",
        re.compile(r"\.gemini/" + r"(?:vault|harness|jetski)"),
    ),
    (
        "internal_bot_suffix",
        re.compile(
            r"\b[a-z][a-z0-9_]*_(?:manage_bot|em_bot|implementer|reviewer|scout|oncall_agent)\b"
        ),
    ),
    (
        "internal_go_link",
        re.compile(r"(?<![A-Za-z0-9_./:-])" + "go" + r"/[a-z0-9_-]{2,}"),
    ),
    (
        "internal_cl_link",
        re.compile(r"(?<![A-Za-z0-9_./:-])" + "cl" + r"/\d{6,}"),
    ),
    (
        "internal_bug_link",
        re.compile(r"(?<![A-Za-z0-9_./:-])" + "b" + r"/\d{6,}"),
    ),
)

_FORBIDDEN_FILENAMES = frozenset({"intent.md", "spec.md", "plan.md"})
_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".sql",
        ".sh",
        ".md",
        ".ipynb",
        ".json",
        ".html",
        ".txt",
        ".yaml",
        ".yml",
        ".gitignore",
    }
)


def scan_directory_for_oss_hygiene(root: Path) -> list[str]:
  """Scans a directory tree for forbidden SDLC files or internal strings."""
  violations: list[str] = []
  for path in sorted(root.rglob("*")):
    if not path.is_file():
      continue
    rel = path.relative_to(root)
    if any(
        part in ("__pycache__", ".pytest_cache", "reports", ".venv")
        for part in rel.parts
    ):
      continue
    if path.name in _FORBIDDEN_FILENAMES:
      violations.append(f"{rel}: forbidden SDLC file {path.name}")
      continue
    if path.suffix not in _TEXT_SUFFIXES and path.name != ".gitignore":
      continue
    content = path.read_text(encoding="utf-8", errors="replace")
    for label, pattern in _BANNED_REGEXES:
      if pattern.search(content):
        violations.append(f"{rel}: matched banned pattern '{label}'")
  return violations


class ShowcaseHtmlAndHygieneTest(unittest.TestCase):
  """Tests for showcase HTML generator, runners, docs, notebook, and hygiene."""

  def test_build_showcase_html_fixture_mode_and_dom_contract(self) -> None:
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
      out_html = Path(tmp) / "showcase.html"
      result = build_showcase_html(demo_fixture=True, output_path=out_html)
      self.assertTrue(out_html.exists())
      self.assertEqual(Path(result["output_path"]), out_html)

      # Verify owner-only 0600 permissions.
      mode = stat.S_IMODE(out_html.stat().st_mode)
      self.assertEqual(mode, 0o600)

      html_text = out_html.read_text(encoding="utf-8")
      self.assertIn("<!doctype html>", html_text.lower())

      # Self-contained: zero external CDNs or iframes/objects/embeds.
      self.assertNotRegex(
          html_text,
          re.compile(
              r"""(?:https?:)?//(?:fonts\.googleapis|unpkg|cdn\.jsdelivr|cdnjs)""",
              re.IGNORECASE,
          ),
      )
      self.assertNotRegex(
          html_text,
          re.compile(r"<\s*(?:iframe|frame|object|embed)\b", re.IGNORECASE),
      )

      # Pure ASCII inside all <code> and <pre> blocks.
      parser = _CodeAsciiParser()
      parser.feed(html_text)
      self.assertEqual(parser.non_ascii_hits, [])

      # Light/Dark theme toggle + all 5 sections + Product Demo Studio IDs.
      required_ids = (
          'id="theme-toggle"',
          'id="toc-nav"',
          'id="overview"',
          'id="ablation-lab"',
          'id="search-studio"',
          'id="cost-cascade"',
          'id="patterns-eval"',
          'id="tab-overview"',
          'id="tab-ablation-lab"',
          'id="tab-search-studio"',
          'id="tab-cost-cascade"',
          'id="tab-patterns-eval"',
          'id="btn-s1"',
          'id="btn-s2"',
          'id="btn-s3"',
          'id="query-chip-1"',
          'id="query-chip-2"',
          'id="query-chip-3"',
          'id="query-chip-4"',
          'id="query-chip-5"',
          'id="search-results-table"',
          'id="expand-trace-btn"',
          'id="cascade-table"',
          'id="lever-toggles"',
          'id="export-eval-btn"',
          'id="eval-suite-drawer" class="drawer hidden"',
      )
      for needle in required_ids:
        self.assertIn(needle, html_text, f"Missing required selector: {needle}")

      # Ensure simulator/KPI/ablation/score fixes hold and vendor names stay absent.
      self.assertNotIn("14.53", html_text)
      self.assertIn("Step-1 Compact Bytes Read", html_text)
      self.assertIn("0 B raw trace JSON read in Step 1", html_text)
      self.assertIn("Step-1 Query Scan:", html_text)
      self.assertIn("Exact Match", html_text)
      self.assertNotIn("turns.slice(0, 6)", html_text)
      self.assertIn("turns.forEach(", html_text)
      self.assertNotIn("arize", html_text.lower())
      self.assertNotIn("braintrust", html_text.lower())

  def test_build_showcase_html_xss_and_secret_redaction(self) -> None:
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    xss_probe = "</script><script>alert('xss')</script>"
    img_probe = '<img src=x onerror="alert(1)">'
    secret_token = "AIza" + "SyDummyFakeKey1234567890abcdef"
    oauth_token = "ya29." + "a0AfH6SMFakeBearerToken12345"

    with tempfile.TemporaryDirectory() as tmp:
      tmp_dir = Path(tmp)
      live_json = tmp_dir / "live.json"
      search_json = tmp_dir / "search.json"
      pattern_json = tmp_dir / "pattern.json"
      out_html = tmp_dir / "showcase.html"

      live_json.write_text(
          json.dumps(
              {
                  "generated_at_utc": "2026-11-24T00:00:00Z",
                  "project_id": "demo-project",
                  "dataset_id": "demo_dataset",
                  "table_id": "agent_events",
                  "location": "US",
                  "model_name": "gemini-2.5-flash",
                  "mock_live": True,
                  "eval_turns": [
                      {
                          "session_id": f"sess_{xss_probe}",
                          "invocation_id": "inv_01",
                          "turn_index": 0,
                          "root_agent": "support_agent",
                          "has_error": True,
                          "has_tool_loop": False,
                          "turn_latency_ms": 420,
                          "total_tokens": 900,
                          "error_summary": f"CARD_EXPIRED {secret_token}",
                          "trace": {
                              "agents": {"a": {"instruction": "secret"}},
                              "turns": [],
                          },
                          "ablation": {
                              "s1_bytes": 12000,
                              "s1_tokens_est": 3000,
                              "s1_exceeds_2048": True,
                              "s2_bytes": 4000,
                              "s2_tokens_est": 1000,
                              "s2_exceeds_2048": False,
                              "s3_bytes": 320,
                              "s3_tokens_est": 80,
                              "s3_exceeds_2048": False,
                              "s3_text": (
                                  f"[OUTCOME: has_error=True]\nUSER: {img_probe}\n"
                                  f"TOOL: Authorization: Bearer {oauth_token}"
                              ),
                          },
                      }
                  ],
                  "ablation_summary": {
                      "turn_count": 1,
                      "s1_avg_bytes": 12000,
                      "s1_avg_tokens": 3000,
                      "s1_over_2048_count": 1,
                      "s2_avg_bytes": 4000,
                      "s2_avg_tokens": 1000,
                      "s2_over_2048_count": 0,
                      "s3_avg_bytes": 320,
                      "s3_avg_tokens": 80,
                      "s3_over_2048_count": 0,
                      "ingest_llm_cost_usd": 0.0,
                  },
              }
          ),
          encoding="utf-8",
      )
      search_json.write_text(
          json.dumps(
              {
                  "index_status": {
                      "ai_embed_column": "MOCK",
                      "search_index": "SCAN_FALLBACK_BELOW_10GIB",
                      "vector_index": "SKIPPED_BELOW_5000_ROWS",
                      "row_count": 1,
                      "embeddings_ready": 1,
                  },
                  "queries": [
                      {
                          "query_id": "Q1",
                          "query_text": xss_probe,
                          "router_decision": {
                              "mode": "KEYWORD",
                              "layer": "L0_RULE",
                              "confidence": 1.0,
                              "latency_ms": 0.0,
                              "input_tokens": 0,
                              "output_tokens": 0,
                              "cost_usd": 0.0,
                          },
                          "step1_bytes_processed": 0,
                          "raw_trace_baseline_bytes_processed": 12000,
                          "step1_hits": [],
                          "layer3_verifications": [],
                      }
                  ],
              }
          ),
          encoding="utf-8",
      )
      pattern_json.write_text(
          json.dumps(
              {
                  "discovered_patterns": [],
                  "suite_health": {
                      "total_tasks": 2,
                      "positive_cases": 1,
                      "negative_cases": 1,
                      "balance_ratio": 0.5,
                      "warnings": [],
                  },
                  "cost_cascade_ablation": [],
              }
          ),
          encoding="utf-8",
      )

      build_showcase_html(
          live_json=live_json,
          search_json=search_json,
          pattern_json=pattern_json,
          output_path=out_html,
      )
      html_text = out_html.read_text(encoding="utf-8")
      self.assertNotIn(xss_probe, html_text)
      self.assertNotIn(img_probe, html_text)
      self.assertNotIn(secret_token, html_text)
      self.assertNotIn(oauth_token, html_text)

  def test_shell_scripts_exist_valid_bash_and_portable(self) -> None:
    for name in ("setup.sh", "run_e2e_demo.sh", "reset.sh"):
      script = DEMO_DIR / name
      self.assertTrue(script.exists(), f"Missing {name}")
      self.assertTrue(os.access(script, os.X_OK), f"{name} is not executable")
      proc = subprocess.run(
          ["bash", "-n", str(script)],
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(proc.returncode, 0, f"bash -n {name}: {proc.stderr}")
      content = script.read_text(encoding="utf-8")
      self.assertIn("Apache License, Version 2.0", content)

    run_script = (DEMO_DIR / "run_e2e_demo.sh").read_text(encoding="utf-8")
    self.assertIn("PYTHONDONTWRITEBYTECODE=1", run_script)
    self.assertIn("PYTHONSAFEPATH=1", run_script)
    self.assertIn("--offline", run_script)

  def test_gitignore_and_docs_and_examples_readme(self) -> None:
    gitignore = (DEMO_DIR / ".gitignore").read_text(encoding="utf-8")
    for entry in (
        ".env",
        "reports/",
        ".pytest_cache/",
        "__pycache__/",
        "*.pyc",
    ):
      self.assertIn(entry, gitignore)

    for doc in ("README.md", "DEMO_NARRATION.md", "VERIFICATION.md"):
      doc_path = DEMO_DIR / doc
      self.assertTrue(doc_path.exists(), f"Missing {doc}")
      self.assertGreater(len(doc_path.read_text(encoding="utf-8")), 200)

    examples_readme = (REPO_ROOT / "examples" / "README.md").read_text(
        encoding="utf-8"
    )
    self.assertIn("combined_cuj_trace_search_demo/", examples_readme)

  def test_notebook_valid_nbformat_v4_and_covers_all_stages(self) -> None:
    nb_path = DEMO_DIR / "combined_cuj_trace_search_demo.ipynb"
    self.assertTrue(nb_path.exists(), "Missing notebook")
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    self.assertEqual(nb.get("nbformat"), 4)
    self.assertGreaterEqual(nb.get("nbformat_minor", 0), 5)
    cells = nb.get("cells", [])
    self.assertGreaterEqual(len(cells), 10)
    joined_source = "\n".join(
        "".join(c.get("source", [])) for c in cells if isinstance(c, dict)
    )
    for keyword in (
        "DEMO_MOCK_LIVE",
        "eval_turns",
        "_ai_index_trajectory",
        "AI.EMBED",
        "AI.SEARCH",
        "AI.GENERATE",
        "EvalSuite",
    ):
      self.assertIn(keyword, joined_source)

  def test_hygiene_scanner_rejects_injected_violations(self) -> None:
    """Negative check (#check-can-fail): scanner MUST catch bad artifacts."""
    with tempfile.TemporaryDirectory() as tmp:
      bad_dir = Path(tmp)
      (bad_dir / "spec.md").write_text("# forbidden", encoding="utf-8")
      (bad_dir / "leak.py").write_text(
          'URL = "https://example.corp.'
          + 'google.com"\n'
          + 'PROJ = "alice-sample-dev-'
          + '123456"\n'
          + 'BUG = "'
          + "b"
          + '/123456789"\n'
          + 'CL = "'
          + "cl"
          + '/987654321"\n'
          + 'GO = "'
          + "go"
          + '/sample-link"\n'
          + 'HOME = "/usr/local/'
          + 'google/home/alice"\n'
          + 'VAULT = ".gemini/'
          + 'vault"\n'
          + 'AGENT = "demo_worker_'
          + 'manage_bot"\n',
          encoding="utf-8",
      )
      violations = scan_directory_for_oss_hygiene(bad_dir)
      self.assertGreaterEqual(len(violations), 8)

  def test_run_e2e_demo_offline_produces_all_artifacts(self) -> None:
    if not (DEMO_DIR / "agent" / "run_live_demo.py").is_file():
      self.skipTest("Stage 1 runner not present")
    if not (DEMO_DIR / "analytics" / "index_and_search.py").is_file():
      self.skipTest("Stage 2 modules not present")

    with tempfile.TemporaryDirectory() as tmp:
      reports_dir = Path(tmp) / "run_offline"
      env = os.environ.copy()
      env["PYTHON_BIN"] = sys.executable
      env["REPORTS_DIR"] = str(reports_dir)
      env["PYTHONDONTWRITEBYTECODE"] = "1"
      env["PYTHONSAFEPATH"] = "1"
      proc = subprocess.run(
          ["bash", str(DEMO_DIR / "run_e2e_demo.sh"), "--offline"],
          cwd=str(DEMO_DIR),
          env=env,
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(
          proc.returncode,
          0,
          f"run_e2e_demo.sh --offline failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
      )

      expected_files = (
          "live_demo_results.json",
          "search_results.json",
          "pattern_eval_report.json",
          "eval_dataset_generated.json",
          "showcase.html",
      )
      for fname in expected_files:
        fpath = reports_dir / fname
        self.assertTrue(fpath.is_file(), f"Missing output artifact: {fname}")
        self.assertGreater(fpath.stat().st_size, 100, f"Empty file: {fname}")

      showcase_path = reports_dir / "showcase.html"
      self.assertEqual(stat.S_IMODE(showcase_path.stat().st_mode), 0o600)
      showcase_text = showcase_path.read_text(encoding="utf-8")
      self.assertIn("Offline Runner Execution", showcase_text)
      self.assertIn("CARD_EXPIRED", showcase_text)

  def test_notebook_executes_cleanly_offline_with_real_modules(self) -> None:
    try:
      import ipykernel  # noqa: F401, PLC0415
      import matplotlib  # noqa: F401, PLC0415
      from nbclient import NotebookClient  # noqa: PLC0415
      import nbformat  # noqa: PLC0415
    except ImportError:
      self.skipTest("nbformat/nbclient/ipykernel/matplotlib not installed")

    nb_path = DEMO_DIR / "combined_cuj_trace_search_demo.ipynb"
    nb = nbformat.read(str(nb_path), as_version=4)

    with tempfile.TemporaryDirectory() as tmp:
      old_reports = os.environ.get("NOTEBOOK_REPORTS_DIR")
      old_mock = os.environ.get("DEMO_MOCK_LIVE")
      os.environ["NOTEBOOK_REPORTS_DIR"] = tmp
      os.environ["DEMO_MOCK_LIVE"] = "true"
      try:
        client = NotebookClient(nb, timeout=90, kernel_name="python3")
        client.execute(cwd=str(DEMO_DIR))
      finally:
        if old_reports is None:
          os.environ.pop("NOTEBOOK_REPORTS_DIR", None)
        else:
          os.environ["NOTEBOOK_REPORTS_DIR"] = old_reports
        if old_mock is None:
          os.environ.pop("DEMO_MOCK_LIVE", None)
        else:
          os.environ["DEMO_MOCK_LIVE"] = old_mock

      for fname in (
          "live_demo_results.json",
          "search_results.json",
          "pattern_eval_report.json",
          "eval_dataset_generated.json",
          "showcase.html",
      ):
        fpath = Path(tmp) / fname
        self.assertTrue(fpath.is_file(), f"Missing notebook artifact: {fname}")
      self.assertEqual(
          stat.S_IMODE((Path(tmp) / "showcase.html").stat().st_mode), 0o600
      )

  def test_demo_directory_has_zero_oss_hygiene_violations(self) -> None:
    violations = scan_directory_for_oss_hygiene(DEMO_DIR)
    self.assertEqual(violations, [], f"Hygiene violations: {violations}")


if __name__ == "__main__":
  unittest.main()
