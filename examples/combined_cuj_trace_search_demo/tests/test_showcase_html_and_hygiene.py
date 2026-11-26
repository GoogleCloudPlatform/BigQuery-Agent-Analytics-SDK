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
    (
        "internal_decisions_codename",
        re.compile(r"\b" + "j" + r"ev\b", re.IGNORECASE),
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
      self.assertIn("L1 Router:", html_text)
      self.assertIn("gemini-decision-1", html_text)
      self.assertIn("(Decisions API)", html_text)
      self.assertNotIn("j" + "ev", html_text.lower())
      self.assertIn("Probs:", html_text)
      self.assertIn("Model:", html_text)

      # Verify fixture queries stamp model metadata consistently inside embedded JSON.
      match = re.search(
          r'<script id="showcase-data" type="application/json">(.*?)</script>',
          html_text,
          re.DOTALL,
      )
      self.assertIsNotNone(match)
      embedded = json.loads(match.group(1))
      q_by_id = {q["query_id"]: q for q in embedded["queries"]}
      self.assertEqual(
          q_by_id["Q1"]["router_decision"]["model"], "deterministic_rules"
      )
      self.assertEqual(
          q_by_id["Q2"]["router_decision"]["model"], "deterministic_rules"
      )
      self.assertEqual(
          q_by_id["Q3"]["router_decision"]["model"], "gemini-decision-1"
      )
      self.assertEqual(
          q_by_id["Q4"]["router_decision"]["model"], "gemini-decision-1"
      )
      self.assertEqual(
          q_by_id["Q5"]["router_decision"]["model"], "deterministic_rules"
      )

  def test_build_showcase_html_l1_router_meta_bar_vertex_fallback(self) -> None:
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
      tmp_dir = Path(tmp)
      search_json = tmp_dir / "search.json"
      out_html = tmp_dir / "showcase.html"
      search_json.write_text(
          json.dumps(
              {
                  "index_status": {
                      "ai_embed_column": "MOCK",
                      "search_index": "READY",
                      "vector_index": "SKIPPED_BELOW_5000_ROWS",
                      "row_count": 2,
                      "embeddings_ready": 2,
                  },
                  "queries": [
                      {
                          "query_id": "Q3",
                          "query_text": "agent claimed refund succeeded",
                          "router_decision": {
                              "mode": "NATURAL_LANGUAGE",
                              "layer": "L1_VERTEX_HYDROGEN",
                              "confidence": 0.9,
                              "latency_ms": 110.0,
                              "input_tokens": 80,
                              "output_tokens": 18,
                              "cost_usd": 0.00005,
                              "model": "gemini-4-hydrogen",
                          },
                          "step1_bytes_processed": 100,
                          "raw_trace_baseline_bytes_processed": 12000,
                          "step1_hits": [],
                          "layer3_verifications": [],
                      }
                  ],
              }
          ),
          encoding="utf-8",
      )
      build_showcase_html(search_json=search_json, output_path=out_html)
      html_text = out_html.read_text(encoding="utf-8")
      self.assertIn("L1 Router: <code>gemini-4-hydrogen</code>", html_text)
      self.assertNotIn("(Decisions API)", html_text)
      self.assertNotIn("j" + "ev", html_text.lower())

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
      os.chmod(tmp_dir, 0o755)

      live_json.write_text(
          json.dumps(
              {
                  "generated_at_utc": "2026-11-24T00:00:00Z",
                  "project_id": "demo-project",
                  "dataset_id": "demo_dataset",
                  "table_id": "agent_events",
                  "location": "US",
                  "model_name": "gemini-4-hydrogen",
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
                          "error_summary": (
                              f"CARD_EXPIRED {secret_token} "
                              "the password is freetext_pw_leak"
                          ),
                          "trace": {
                              "agents": {"a": {"instruction": "secret"}},
                              "password": {
                                  "nested": "nested_container_pw_leak"
                              },
                              "credentials": {
                                  "value": "nested_container_cred_leak"
                              },
                              "auth": ["nested_auth_list_leak"],
                              "passphrase": "nested_passphrase_leak",
                              "signing_key": "nested_signing_key_leak",
                              "nested": {
                                  "client_secret": "nested_client_secret_leak",
                                  "apikey": "nested_apikey_leak",
                                  "cookie": "nested_cookie_leak",
                              },
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
      self.assertNotIn("nested_container_pw_leak", html_text)
      self.assertNotIn("nested_container_cred_leak", html_text)
      self.assertNotIn("nested_auth_list_leak", html_text)
      self.assertNotIn("nested_passphrase_leak", html_text)
      self.assertNotIn("nested_signing_key_leak", html_text)
      self.assertNotIn("nested_cookie_leak", html_text)
      self.assertNotIn("freetext_pw_leak", html_text)
      self.assertNotIn("nested_client_secret_leak", html_text)
      self.assertNotIn("nested_apikey_leak", html_text)
      # Caller's pre-existing directory permissions must remain untouched.
      self.assertEqual(stat.S_IMODE(tmp_dir.stat().st_mode), 0o755)
      self.assertEqual(stat.S_IMODE(out_html.stat().st_mode), 0o600)

  def test_merge_inputs_does_not_leak_fixture_into_live_run(self) -> None:
    from report.build_showcase_html import _merge_inputs  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
      tmp_dir = Path(tmp)
      live_json = tmp_dir / "live.json"
      live_json.write_text(
          json.dumps(
              {
                  "generated_at_utc": "2026-11-24T00:00:00Z",
                  "project_id": "user-project",
                  "dataset_id": "ds",
                  "table_id": "agent_events",
                  "location": "US",
                  "model_name": "gemini-4-hydrogen",
                  "mock_live": False,
                  "eval_turns": [],
                  "ablation_summary": {"turn_count": 0},
              }
          ),
          encoding="utf-8",
      )
      merged = _merge_inputs(
          live_json=live_json,
          search_json=None,
          pattern_json=None,
          eval_suite_json=None,
          demo_fixture=False,
      )
      self.assertEqual(merged["mode"], "Live BigQuery Run")
      self.assertEqual(merged["queries"], [])
      self.assertEqual(merged["cost_cascade_ablation"], [])
      self.assertEqual(merged["eval_suite"], {})

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
          + 'manage_bot"\n'
          + 'CODENAME = "'
          + "J"
          + 'ev"\n',
          encoding="utf-8",
      )
      violations = scan_directory_for_oss_hygiene(bad_dir)
      self.assertGreaterEqual(len(violations), 9)

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
        self.assertEqual(
            stat.S_IMODE(fpath.stat().st_mode),
            0o600,
            f"Expected 0600 on {fname}",
        )
      self.assertEqual(stat.S_IMODE(reports_dir.stat().st_mode), 0o700)

      showcase_path = reports_dir / "showcase.html"
      showcase_text = showcase_path.read_text(encoding="utf-8")
      self.assertIn("Offline Runner Execution", showcase_text)
      self.assertIn("CARD_EXPIRED", showcase_text)

  def test_run_e2e_demo_honors_dotenv_demo_offline_true_without_flag(
      self,
  ) -> None:
    import shutil  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
      fake_root = Path(tmp) / "repo"
      fake_demo = fake_root / "examples" / "combined_cuj_trace_search_demo"
      fake_demo.mkdir(parents=True)
      os.symlink(REPO_ROOT / "src", fake_root / "src")
      for sub in ("agent", "analytics", "report", "sql"):
        os.symlink(DEMO_DIR / sub, fake_demo / sub)
      shutil.copy2(DEMO_DIR / "run_e2e_demo.sh", fake_demo / "run_e2e_demo.sh")
      (fake_demo / ".env").write_text(
          "PROJECT_ID=demo-project\nDEMO_OFFLINE=true\n",
          encoding="utf-8",
      )
      reports_dir = Path(tmp) / "dotenv_offline_reports"
      env = {
          "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
          "PYTHON_BIN": sys.executable,
          "REPORTS_DIR": str(reports_dir),
          "PYTHONDONTWRITEBYTECODE": "1",
          "PYTHONSAFEPATH": "1",
      }
      proc = subprocess.run(
          ["bash", str(fake_demo / "run_e2e_demo.sh")],
          cwd=str(fake_demo),
          env=env,
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(
          proc.returncode,
          0,
          f"DEMO_OFFLINE=true run failed:\n{proc.stdout}\n{proc.stderr}",
      )
      self.assertTrue((reports_dir / "showcase.html").is_file())
      self.assertIn(
          "Offline Runner Execution",
          (reports_dir / "showcase.html").read_text(encoding="utf-8"),
      )

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

  def test_run_e2e_demo_rejects_mutually_exclusive_offline_and_live(
      self,
  ) -> None:
    proc = subprocess.run(
        ["bash", str(DEMO_DIR / "run_e2e_demo.sh"), "--offline", "--live"],
        cwd=str(DEMO_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(proc.returncode, 2)
    self.assertIn("mutually exclusive", proc.stderr)

  def test_reset_sh_hermetic_bq_ownership_empty_array_503_and_scale_cleanup(
      self,
  ) -> None:
    import shutil  # noqa: PLC0415

    def _run_reset(
        *,
        args: list[str],
        dotenv_text: str,
        bq_script: str,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
      with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_demo = tmp_path / "repo" / "examples" / "demo"
        fake_demo.mkdir(parents=True)
        shutil.copy2(DEMO_DIR / "reset.sh", fake_demo / "reset.sh")
        (fake_demo / ".env").write_text(dotenv_text, encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_file = tmp_path / "bq_calls.log"
        bq_stub = bin_dir / "bq"
        bq_stub.write_text(bq_script, encoding="utf-8")
        bq_stub.chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "PYTHON_BIN": sys.executable,
            "BQ_LOG_FILE": str(log_file),
        }
        proc = subprocess.run(
            ["bash", str(fake_demo / "reset.sh"), *args],
            cwd=str(fake_demo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = (
            log_file.read_text(encoding="utf-8").splitlines()
            if log_file.is_file()
            else []
        )
        return proc, calls

    base_env = (
        "PROJECT_ID=demo-project\n"
        "DATASET_ID=demo_ds\n"
        "TABLE_ID=agent_events\n"
        "# EVAL_TURNS_TABLE=eval_turns\n"
        "BQ_LOCATION=US\n"
    )

    # 1. Empty RUN_SCOPED_TABLES under set -u succeeds cleanly.
    bq_empty = (
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$BQ_LOG_FILE"\n'
        'if [[ "$1" == "ls" ]]; then echo "[]"; exit 0; fi\n'
        'if [[ "$1" == "show" ]]; then echo "Not found: table" >&2; exit 1; fi\n'
        "exit 0\n"
    )
    proc1, _ = _run_reset(
        args=["--drop-demo-tables"], dotenv_text=base_env, bq_script=bq_empty
    )
    self.assertEqual(proc1.returncode, 0, proc1.stderr)

    # 2. Owned eval_turns_<run> dropped; unowned / labels:null preserved;
    #    unpinned default eval_turns with labels:null does not fail.
    bq_mixed = (
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$BQ_LOG_FILE"\n'
        'if [[ "$1" == "ls" ]]; then\n'
        '  echo \'[{"tableReference":{"tableId":"eval_turns_run1"}},'
        '{"tableReference":{"tableId":"eval_turns_other"}}]\'\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "show" ]]; then\n'
        '  case "$*" in\n'
        '    *".agent_events") echo \'{"labels":{"bqaa_demo":"combined_cuj_trace_search"}}\'; exit 0 ;;\n'
        '    *".eval_turns") echo \'{"labels":null}\'; exit 0 ;;\n'
        '    *".eval_turns_run1") echo \'{"labels":{"bqaa_demo":"combined_cuj_trace_search"}}\'; exit 0 ;;\n'
        '    *".eval_turns_other") echo \'{"labels":null}\'; exit 0 ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n"
    )
    proc2, calls2 = _run_reset(
        args=["--drop-demo-tables"], dotenv_text=base_env, bq_script=bq_mixed
    )
    self.assertEqual(proc2.returncode, 0, proc2.stderr)
    rm_calls2 = [c for c in calls2 if c.startswith("rm ")]
    self.assertTrue(any("eval_turns_run1" in c for c in rm_calls2))
    self.assertTrue(any("agent_events" in c for c in rm_calls2))
    self.assertFalse(any("eval_turns_other" in c for c in rm_calls2))
    self.assertFalse(any(c.endswith(".eval_turns") for c in rm_calls2))

    # 3. bq show 503/auth error exits non-zero and never drops table.
    bq_503 = (
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$BQ_LOG_FILE"\n'
        'if [[ "$1" == "ls" ]]; then\n'
        '  echo \'[{"tableReference":{"tableId":"eval_turns_run1"}}]\'\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "show" ]]; then\n'
        '  echo "BigQuery error: 503 Service Unavailable" >&2\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    proc3, calls3 = _run_reset(
        args=["--drop-demo-tables"], dotenv_text=base_env, bq_script=bq_503
    )
    self.assertNotEqual(proc3.returncode, 0)
    self.assertFalse(any(c.startswith("rm ") for c in calls3))
    self.assertNotIn("Preserved non-demo table", proc3.stdout)

    # 4. --delete-scale-rows deletes scale_synth_* on owned eval_turns_<run>.
    proc4, calls4 = _run_reset(
        args=["--delete-scale-rows"], dotenv_text=base_env, bq_script=bq_mixed
    )
    self.assertEqual(proc4.returncode, 0, proc4.stderr)
    del_calls = [c for c in calls4 if c.startswith("query ")]
    self.assertTrue(any("eval_turns_run1" in c for c in del_calls))
    self.assertFalse(any("eval_turns_other" in c for c in del_calls))

    # 5. Harmless stderr warnings from bq ls / bq show do NOT corrupt stdout
    #    JSON parsing.
    bq_stderr_warn = (
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$BQ_LOG_FILE"\n'
        'if [[ "$1" == "ls" ]]; then\n'
        '  echo "WARNING: benign CLI deprecation notice" >&2\n'
        '  echo \'[{"tableReference":{"tableId":"eval_turns_warn1"}}]\'\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "show" ]]; then\n'
        '  echo "WARNING: benign show stderr warning" >&2\n'
        '  case "$*" in\n'
        '    *".eval_turns") echo \'{"labels":null}\'; exit 0 ;;\n'
        '    *) echo \'{"labels":{"bqaa_demo":"combined_cuj_trace_search"}}\'; exit 0 ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n"
    )
    proc5, calls5 = _run_reset(
        args=["--drop-demo-tables"],
        dotenv_text=base_env,
        bq_script=bq_stderr_warn,
    )
    self.assertEqual(proc5.returncode, 0, proc5.stderr)
    rm_calls5 = [c for c in calls5 if c.startswith("rm ")]
    self.assertTrue(any("eval_turns_warn1" in c for c in rm_calls5))

  def test_issue608_readme_safepath_setup_notebook_and_html_redaction(
      self,
  ) -> None:
    from analytics.trajectory_extractor import extract_s3_outcome_first  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    # 1. Item #10: README fixture command succeeds under PYTHONSAFEPATH=1.
    with tempfile.TemporaryDirectory() as tmp:
      out_html = Path(tmp) / "showcase_preview.html"
      clean_env = {
          "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
          "PYTHONDONTWRITEBYTECODE": "1",
          "PYTHONSAFEPATH": "1",
      }
      proc = subprocess.run(
          [
              sys.executable,
              "report/build_showcase_html.py",
              "--demo-fixture",
              "--output",
              str(out_html),
          ],
          cwd=str(DEMO_DIR),
          env=clean_env,
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(
          proc.returncode,
          0,
          f"README fixture command failed:\n{proc.stdout}\n{proc.stderr}",
      )
      self.assertTrue(out_html.is_file())
      self.assertEqual(stat.S_IMODE(out_html.stat().st_mode), 0o600)
      readme_text = (DEMO_DIR / "README.md").read_text(encoding="utf-8")
      self.assertIn("python3 report/build_showcase_html.py", readme_text)
      self.assertNotIn("python3 -m report.build_showcase_html", readme_text)

    # 2. Item #9: setup.sh enables discoveryengine.googleapis.com.
    setup_text = (DEMO_DIR / "setup.sh").read_text(encoding="utf-8")
    self.assertIn("discoveryengine.googleapis.com", setup_text)

    # 3. Item #4: notebook Cell 1 shares RUN_ID across table and output dir.
    nb = json.loads(
        (DEMO_DIR / "combined_cuj_trace_search_demo.ipynb").read_text(
            encoding="utf-8"
        )
    )
    cell1_src = "".join(nb["cells"][1]["source"])
    self.assertIn("RUN_ID =", cell1_src)
    self.assertIn("eval_turns_{RUN_ID}", cell1_src)
    self.assertIn("notebook_run_{RUN_ID}", cell1_src)

    # 4. Item #1 + Comment bullets 1-2: error-text secrets, budget tail, and
    #    structured parent-key bypasses never reach HTML or verifier trajectory.
    budget_tail = (
        "{} " * 40
    ) + '{"password": {"nested": "html_budget_tail_leak"}}'
    s3_traj = extract_s3_outcome_first(
        {
            "turns": [
                {
                    "events": [
                        {
                            "author": "customer_ops_agent",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "function_call": {
                                            "name": "auth_tool",
                                            "args": {
                                                "auth_result": {
                                                    "value": "html_ar_leak"
                                                },
                                                "credentials_result": {
                                                    "value": "html_cr_leak"
                                                },
                                                "authentication": {
                                                    "value": "html_an_leak"
                                                },
                                                "auth_error": {
                                                    "secret": "html_ae_leak"
                                                },
                                            },
                                        }
                                    },
                                    {
                                        "text": (
                                            "CARD_EXPIRED "
                                            'credentials={"value":"html_str_leak"} '
                                            "auth={'token': 'html_py_leak'} "
                                            'password="html quoted space leak"'
                                        )
                                    },
                                ],
                            },
                        }
                    ]
                }
            ]
        },
        metrics={"has_error": True, "error_summary": budget_tail},
    )
    scrubbed_err = scrub_sensitive_text(
        'CARD_EXPIRED credentials={"value":"html_err_sum_leak"} ' + budget_tail
    )
    with tempfile.TemporaryDirectory() as tmp:
      tmp_dir = Path(tmp)
      live_json = tmp_dir / "live.json"
      search_json = tmp_dir / "search.json"
      out_html = tmp_dir / "showcase.html"
      live_json.write_text(
          json.dumps(
              {
                  "generated_at_utc": "2026-11-25T00:00:00Z",
                  "project_id": "demo-project",
                  "dataset_id": "demo_ds",
                  "table_id": "agent_events",
                  "location": "US",
                  "model_name": "gemini-4-hydrogen",
                  "mock_live": True,
                  "eval_turns": [
                      {
                          "session_id": "sess_leak_check",
                          "invocation_id": "inv_leak_check",
                          "turn_index": 0,
                          "root_agent": "customer_ops_agent",
                          "has_error": True,
                          "has_tool_loop": False,
                          "turn_latency_ms": 200,
                          "total_tokens": 400,
                          "error_summary": scrubbed_err,
                          "trace": {
                              "auth_result": {"value": "trace_ar_leak"},
                              "credentials_result": {"value": "trace_cr_leak"},
                              "authentication": {"value": "trace_an_leak"},
                              "auth_error": {"value": "trace_ae_leak"},
                              "turns": [],
                          },
                          "ablation": {
                              "s1_bytes": 4000,
                              "s1_tokens_est": 1000,
                              "s1_exceeds_2048": False,
                              "s2_bytes": 1200,
                              "s2_tokens_est": 300,
                              "s2_exceeds_2048": False,
                              "s3_bytes": len(s3_traj.encode("utf-8")),
                              "s3_tokens_est": 80,
                              "s3_exceeds_2048": False,
                              "s3_text": s3_traj,
                          },
                      }
                  ],
                  "ablation_summary": {"turn_count": 1},
              }
          ),
          encoding="utf-8",
      )
      search_json.write_text(
          json.dumps(
              {
                  "index_status": {
                      "ai_embed_column": "MOCK",
                      "search_index": "READY",
                      "vector_index": "SKIPPED_BELOW_5000_ROWS",
                      "row_count": 1,
                      "embeddings_ready": 1,
                  },
                  "queries": [
                      {
                          "query_id": "Q1",
                          "query_text": "CARD_EXPIRED",
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
                          "raw_trace_baseline_bytes_processed": 4000,
                          "step1_hits": [
                              {
                                  "session_id": "sess_leak_check",
                                  "invocation_id": "inv_leak_check",
                                  "has_error": True,
                                  "error_summary": scrubbed_err,
                                  "_ai_index_trajectory": s3_traj,
                              }
                          ],
                          "step2_lazy_trace": None,
                          "layer3_verifications": [],
                      }
                  ],
              }
          ),
          encoding="utf-8",
      )
      build_showcase_html(
          live_json=live_json,
          search_json=search_json,
          output_path=out_html,
      )
      html_text = out_html.read_text(encoding="utf-8")
      for secret in (
          "html_budget_tail_leak",
          "html_ar_leak",
          "html_cr_leak",
          "html_an_leak",
          "html_ae_leak",
          "html_str_leak",
          "html_py_leak",
          "html quoted space leak",
          "html_err_sum_leak",
          "trace_ar_leak",
          "trace_cr_leak",
          "trace_an_leak",
          "trace_ae_leak",
      ):
        self.assertNotIn(secret, html_text)
        self.assertNotIn(secret, s3_traj)
        self.assertNotIn(secret, scrubbed_err)
      self.assertIn("CARD_EXPIRED", html_text)

  def test_html_redaction_covers_multiline_unterminated_and_dynamic_kv_forms(
      self,
  ) -> None:
    from report.build_showcase_html import _redact_secrets  # noqa: PLC0415
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    # P1-1: unterminated / oversized quoted values fail closed to end-of-string.
    p1_1_cases = (
        ('password="line1\nSECRET_LINE_2', "SECRET_LINE_2"),
        (
            'credentials=\'{\n  "value": "my_secret",\n  "pad": "truncated',
            "my_secret",
        ),
        (
            'private_key="-----BEGIN PRIVATE'
            " KEY-----\nMIIEvQIBADANBgkqhkiG9w0_SECRET",
            "MIIEvQIBADANBgkqhkiG9w0_SECRET",
        ),
        (
            'credentials="hdr\n' + ("B" * 16500) + 'TAIL_SECRET"',
            "TAIL_SECRET",
        ),
    )
    for sample, secret_val in p1_1_cases:
      with self.subTest(p1_1=secret_val):
        scrubbed = _redact_secrets(sample)
        self.assertNotIn(secret_val, scrubbed)
        self.assertIn("[REDACTED]", scrubbed)

    # P1-2 + P2-2: dynamic free-text KV forms + dict/text benign keep parity.
    p1_2_cases = (
        ('CARD_EXPIRED auth_error={"value":"LEAK_AE_VAL"}', "LEAK_AE_VAL"),
        (
            "CARD_EXPIRED auth_error={'value':'LEAK_AE_SQ_VAL'}",
            "LEAK_AE_SQ_VAL",
        ),
        (
            'CARD_EXPIRED authentication_result={"value":"LEAK_AUTH_RES"}',
            "LEAK_AUTH_RES",
        ),
        (
            'CARD_EXPIRED credentials_payload={"value":"LEAK_CRED_PAY"}',
            "LEAK_CRED_PAY",
        ),
        (
            'CARD_EXPIRED credential_data={"value":"LEAK_CRED_DATA"}',
            "LEAK_CRED_DATA",
        ),
        ('credentials=\n{"value":"LEAK_ML_EQ"}', "LEAK_ML_EQ"),
        ("myPwd=secret_mypwd", "secret_mypwd"),
    )
    for sample, secret_val in p1_2_cases:
      with self.subTest(p1_2=secret_val):
        scrubbed = _redact_secrets(sample)
        self.assertNotIn(secret_val, scrubbed)
        self.assertIn("[REDACTED]", scrubbed)

    # Benign keep forms preserved in both text and dicts.
    benign_dict = _redact_secrets(
        {
            "DB_PASSWORD_FILE": "/var/run/db_pw",
            "NOPASSWD_FILE": "/etc/sudoers",
            "passwordless": "true",
            "total_tokens": 1542,
            "prompt_token": 412,
            "auth_error": "PERMISSION_DENIED",
            "auth_status": "forbidden",
            "credential_type": "oauth_service_account",
            "author": "alice",
            "authority": "ca",
        }
    )
    self.assertEqual(benign_dict["DB_PASSWORD_FILE"], "/var/run/db_pw")
    self.assertEqual(benign_dict["NOPASSWD_FILE"], "/etc/sudoers")
    self.assertEqual(benign_dict["passwordless"], "true")
    self.assertEqual(benign_dict["auth_error"], "PERMISSION_DENIED")

    # End-to-end rendered HTML must never leak any P1-1 or P1-2 payload.
    p1_2_block = "\n".join(
        [s for s, _ in p1_2_cases]
        + ["DB_PASSWORD_FILE=/var/run/db_pw", "passwordless=true"]
    )
    with tempfile.TemporaryDirectory() as tmpdir:
      root = Path(tmpdir)
      live_json = root / "live.json"
      search_json = root / "search.json"
      out_html = root / "showcase.html"
      eval_turns = [
          {
              "session_id": "sess_html_r2_kv",
              "invocation_id": "inv_html_r2_kv",
              "scenario_id": "scn_html_r2_kv",
              "has_error": True,
              "error_summary": p1_2_block,
              "raw_trace_bytes": 1200,
              "ablation": {
                  "s1_bytes": 1200,
                  "s1_tokens_est": 300,
                  "s1_exceeds_2048": False,
                  "s2_bytes": 600,
                  "s2_tokens_est": 150,
                  "s2_exceeds_2048": False,
                  "s3_bytes": 200,
                  "s3_tokens_est": 50,
                  "s3_exceeds_2048": False,
                  "s3_text": p1_2_block,
              },
          }
      ]
      for idx, (p1_sample, _) in enumerate(p1_1_cases[:3]):
        eval_turns.append(
            {
                "session_id": f"sess_html_r2_q{idx}",
                "invocation_id": f"inv_html_r2_q{idx}",
                "scenario_id": f"scn_html_r2_q{idx}",
                "has_error": True,
                "error_summary": f"CARD_EXPIRED {p1_sample}",
                "raw_trace_bytes": 1200,
                "ablation": {
                    "s1_bytes": 1200,
                    "s1_tokens_est": 300,
                    "s1_exceeds_2048": False,
                    "s2_bytes": 600,
                    "s2_tokens_est": 150,
                    "s2_exceeds_2048": False,
                    "s3_bytes": 200,
                    "s3_tokens_est": 50,
                    "s3_exceeds_2048": False,
                    "s3_text": f"CARD_EXPIRED {p1_sample}",
                },
            }
        )
      live_json.write_text(
          json.dumps(
              {
                  "eval_turns": eval_turns,
                  "ablation_summary": {"turn_count": len(eval_turns)},
              }
          ),
          encoding="utf-8",
      )
      search_json.write_text(
          json.dumps(
              {
                  "index_status": {
                      "ai_embed_column": "MOCK",
                      "search_index": "READY",
                      "vector_index": "SKIPPED_BELOW_5000_ROWS",
                      "row_count": len(eval_turns),
                      "embeddings_ready": len(eval_turns),
                  },
                  "queries": [],
              }
          ),
          encoding="utf-8",
      )
      build_showcase_html(
          live_json=live_json, search_json=search_json, output_path=out_html
      )
      html_text = out_html.read_text(encoding="utf-8")
      for _, secret_val in p1_1_cases[:3] + p1_2_cases:
        self.assertNotIn(secret_val, html_text)
      self.assertIn("CARD_EXPIRED", html_text)
      self.assertIn("DB_PASSWORD_FILE=/var/run/db_pw", html_text)
      self.assertIn("passwordless=true", html_text)

  def test_html_redaction_covers_python_reprs_escaped_quotes_and_dotted_keys(
      self,
  ) -> None:
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    error_payload = (
        "CARD_EXPIRED "
        "password=b'html_bytes_sec' "
        "password=('u', 'html_tuple_sec') "
        "auth_error=('html_ae_tuple_sec',) "
        "credentials=Credentials(value='html_ctor_sec') "
        r"password=\"html escaped sec\" "
        "{'db.password': 'html_dot_sq_sec'} "
        '{"db.password": "html_dot_dq_sec"} '
        "password1=html_num_pw_sec "
        "apiKey2=html_num_ak_sec "
        'api_keys=["html_plural_ak_sec"]'
    )
    with tempfile.TemporaryDirectory() as tmpdir:
      root = Path(tmpdir)
      live_json = root / "live.json"
      search_json = root / "search.json"
      out_html = root / "showcase.html"
      live_json.write_text(
          json.dumps(
              {
                  "eval_turns": [
                      {
                          "session_id": "sess_html_repr",
                          "invocation_id": "inv_html_repr",
                          "scenario_id": "scn_html_repr",
                          "has_error": True,
                          "error_summary": error_payload,
                          "raw_trace_bytes": 1200,
                          "ablation": {
                              "s1_bytes": 1200,
                              "s1_tokens_est": 300,
                              "s1_exceeds_2048": False,
                              "s2_bytes": 600,
                              "s2_tokens_est": 150,
                              "s2_exceeds_2048": False,
                              "s3_bytes": 200,
                              "s3_tokens_est": 50,
                              "s3_exceeds_2048": False,
                              "s3_text": error_payload,
                          },
                      }
                  ],
                  "ablation_summary": {"turn_count": 1},
              }
          ),
          encoding="utf-8",
      )
      search_json.write_text(
          json.dumps(
              {
                  "index_status": {
                      "ai_embed_column": "MOCK",
                      "search_index": "READY",
                      "vector_index": "SKIPPED_BELOW_5000_ROWS",
                      "row_count": 1,
                      "embeddings_ready": 1,
                  },
                  "queries": [],
              }
          ),
          encoding="utf-8",
      )
      build_showcase_html(
          live_json=live_json, search_json=search_json, output_path=out_html
      )
      html_text = out_html.read_text(encoding="utf-8")
      for secret_val in (
          "html_bytes_sec",
          "html_tuple_sec",
          "html_ae_tuple_sec",
          "html_ctor_sec",
          "html escaped sec",
          "html_dot_sq_sec",
          "html_dot_dq_sec",
          "html_num_pw_sec",
          "html_num_ak_sec",
          "html_plural_ak_sec",
      ):
        self.assertNotIn(secret_val, html_text)
      self.assertIn("CARD_EXPIRED", html_text)

  def test_html_cookie_jar_nextline_citations_and_deferred_hydration_drawer(
      self,
  ) -> None:
    from analytics.index_and_search import run_index_and_search  # noqa: PLC0415
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    citation_prefix = "[1] " * 50
    error_payload = (
        f"{citation_prefix}CARD_EXPIRED "
        "Authentication: failed "
        '{"cookies": {"sessionid": "html_cookie_leak"}} '
        "password:\n  html_nl_leak"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
      root = Path(tmpdir)
      live_json = root / "live.json"
      search_json = root / "search.json"
      out_html = root / "showcase.html"
      live_json.write_text(
          json.dumps(
              {
                  "eval_turns": [
                      {
                          "session_id": "sess_html_fix",
                          "invocation_id": "inv_html_fix",
                          "scenario_id": "scn_html_fix",
                          "has_error": True,
                          "error_summary": error_payload,
                          "raw_trace_bytes": 1200,
                          "ablation": {
                              "s1_bytes": 1200,
                              "s1_tokens_est": 300,
                              "s1_exceeds_2048": False,
                              "s2_bytes": 600,
                              "s2_tokens_est": 150,
                              "s2_exceeds_2048": False,
                              "s3_bytes": 240,
                              "s3_tokens_est": 60,
                              "s3_exceeds_2048": False,
                              "s3_text": error_payload,
                          },
                      }
                  ],
                  "ablation_summary": {"turn_count": 1},
              }
          ),
          encoding="utf-8",
      )
      default_report = run_index_and_search(
          output_json=search_json, offline=True
      )
      self.assertTrue(
          all(q["step2_lazy_trace"] is None for q in default_report["queries"])
      )
      build_showcase_html(
          live_json=live_json, search_json=search_json, output_path=out_html
      )
      html_text = out_html.read_text(encoding="utf-8")
      self.assertNotIn("html_cookie_leak", html_text)
      self.assertNotIn("html_nl_leak", html_text)
      self.assertIn("CARD_EXPIRED", html_text)
      self.assertIn("Authentication: failed", html_text)
      self.assertNotIn(
          "JSON.stringify(q.step2_lazy_trace || {}, null, 2)", html_text
      )
      self.assertIn("NOT_HYDRATED_ON_DEFAULT_SEARCH", html_text)
      self.assertIn("--hydrate-top-trace", html_text)

  def test_html_dynamic_eval_turns_table_and_review2_redaction(self) -> None:
    from report.build_showcase_html import build_showcase_html  # noqa: PLC0415

    pad_17k = "a" * 17_000
    error_payload = (
        "CARD_EXPIRED cookies=3 "
        f'{{"body": "{pad_17k}", "request:password": "html_tail_leak"}} '
        '["password:\\t html_arr_leak"] '
        "password_count=234567 "
        "authentication_result=html_opaque_tok "
        "Cookie: a=1; b=html_ck2"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
      root = Path(tmpdir)
      live_json = root / "live.json"
      search_json = root / "search.json"
      out_html = root / "showcase.html"
      live_json.write_text(
          json.dumps(
              {
                  "eval_turns_table": "eval_turns_live_fallback",
                  "eval_turns": [
                      {
                          "session_id": "sess_html_r2_tbl",
                          "invocation_id": "inv_html_r2_tbl",
                          "scenario_id": "scn_html_r2_tbl",
                          "has_error": True,
                          "error_summary": error_payload,
                          "raw_trace_bytes": 1200,
                          "ablation": {
                              "s1_bytes": 1200,
                              "s1_tokens_est": 300,
                              "s1_exceeds_2048": False,
                              "s2_bytes": 600,
                              "s2_tokens_est": 150,
                              "s2_exceeds_2048": False,
                              "s3_bytes": 240,
                              "s3_tokens_est": 60,
                              "s3_exceeds_2048": False,
                              "s3_text": error_payload,
                          },
                      }
                  ],
                  "ablation_summary": {"turn_count": 1},
              }
          ),
          encoding="utf-8",
      )
      search_json.write_text(
          json.dumps(
              {
                  "eval_turns_table": "eval_turns_20261126_01",
                  "index_status": {
                      "ai_embed_column": "MOCK",
                      "search_index": "READY",
                      "vector_index": "SKIPPED_BELOW_5000_ROWS",
                      "row_count": 1,
                      "embeddings_ready": 1,
                  },
                  "queries": [],
              }
          ),
          encoding="utf-8",
      )
      build_showcase_html(
          live_json=live_json, search_json=search_json, output_path=out_html
      )
      html_text = out_html.read_text(encoding="utf-8")
      self.assertIn('"eval_turns_table": "eval_turns_20261126_01"', html_text)
      self.assertIn("evalTurnsTable", html_text)
      self.assertIn("FROM `' + evalTurnsTable + '`\\n", html_text)
      self.assertNotIn("FROM eval_turns\\n", html_text)
      for secret_val in (
          "html_tail_leak",
          "html_arr_leak",
          "234567",
          "html_opaque_tok",
          "html_ck2",
      ):
        self.assertNotIn(secret_val, html_text)
      self.assertIn("CARD_EXPIRED", html_text)
      self.assertIn("cookies=3", html_text)


if __name__ == "__main__":
  unittest.main()
