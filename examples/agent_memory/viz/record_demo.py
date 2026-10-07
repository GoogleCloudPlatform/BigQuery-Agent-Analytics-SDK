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

"""Records a walkthrough video and a screenshot of the web view.

Serves this folder on a local port, drives ``index.html`` in headless
Chromium with Playwright video recording on, then converts the WebM to MP4
with ffmpeg. Captions are written from the export itself, so they describe
whatever run ``data/memory_export.json`` holds. The cursor and caption bar
are injected for the recording only; the page has neither.

  pip install playwright && python -m playwright install chromium
  python examples/agent_memory/viz/record_demo.py
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Iterator, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_VIDEO = HERE.parent / "demo.mp4"
DEFAULT_SCREENSHOT = HERE / "screenshot.png"
VIDEO_SIZE = {"width": 1280, "height": 800}

# A visible cursor and a caption bar, for the recording only.
OVERLAY_JS = """
(() => {
  const install = () => {
    if (document.getElementById('demo-cursor')) return;
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    cursor.style.cssText = 'position:fixed;z-index:99;width:18px;height:18px;'
      + 'margin:-9px 0 0 -9px;border-radius:50%;pointer-events:none;'
      + 'background:rgba(29,39,51,.18);border:2px solid #1d2733;'
      + 'left:-40px;top:-40px;transition:transform .12s';
    const caption = document.createElement('div');
    caption.id = 'demo-caption';
    caption.style.cssText = 'position:fixed;z-index:98;left:50%;bottom:22px;'
      + 'transform:translateX(-50%);max-width:980px;padding:12px 20px;'
      + 'border-radius:10px;background:rgba(18,24,32,.88);color:#fff;'
      + 'font:500 17px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif;'
      + 'text-align:center;pointer-events:none;opacity:0;transition:opacity .25s';
    document.body.append(cursor, caption);
    window.addEventListener('mousemove', (e) => {
      cursor.style.left = e.clientX + 'px';
      cursor.style.top = e.clientY + 'px';
    }, true);
    window.addEventListener('mousedown', () => { cursor.style.transform = 'scale(.7)'; }, true);
    window.addEventListener('mouseup', () => { cursor.style.transform = 'none'; }, true);
    window.__demoCaption = (text) => {
      caption.textContent = text;
      caption.style.opacity = text ? '1' : '0';
    };
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install);
  } else {
    install();
  }
})();
"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):

  def log_message(self, *args: Any) -> None:
    pass


@contextlib.contextmanager
def serve(directory: Path) -> Iterator[str]:
  """Serves ``directory`` on a free local port for the duration."""
  handler = functools.partial(_QuietHandler, directory=str(directory))
  server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    yield f"http://127.0.0.1:{server.server_address[1]}"
  finally:
    server.shutdown()


def _session_number(user: dict, session_id: str) -> int:
  ids = [s["session_id"] for s in user["sessions"]]
  return ids.index(session_id) + 1


def story(export: dict) -> dict[str, Any]:
  """Facts for the captions, read from the export (absent ones are None)."""
  user = export["users"][0]
  facts: dict[str, Any] = {
      "user": user,
      "rows": sum(
          s["row_count"] for u in export["users"] for s in u["sessions"]
      ),
      "sessions": sum(len(u["sessions"]) for u in export["users"]),
      "other_user": export["users"][1] if len(export["users"]) > 1 else None,
  }
  replaced = next((p for p in user["preferences"] if p["valid_until"]), None)
  if replaced is not None:
    newer = next(
        (
            p
            for p in user["preferences"]
            if p["category"] == replaced["category"]
            and p["valid_from"] == replaced["valid_until"]
        ),
        None,
    )
    if newer is not None:
      facts["replaced"] = (replaced, newer)
  entity = max(
      user["entities"],
      key=lambda e: len({m["session_id"] for m in e["mentions"]}),
      default=None,
  )
  if entity is not None:
    facts["entity"] = entity
  traces = user["traces"]
  for i, trace in enumerate(traces):
    tools = [r for r in trace["timeline"] if r["kind"] == "tool"]
    failed = [r for r in tools if r["status"] == "error"]
    if failed and "failure" not in facts:
      facts["failure"] = (trace, failed[0])
      # A later turn of the same session that ran the same tool cleanly.
      facts["retried"] = any(
          r["kind"] == "tool"
          and r["label"] == failed[0]["label"]
          and r["status"] == "success"
          for later in traces[i + 1 :]
          if later["session_id"] == trace["session_id"]
          for r in later["timeline"]
      )
    recall = [r for r in tools if r["label"] == "recall_memory"]
    if recall and "recall" not in facts:
      facts["recall"] = (trace, recall[0])
  last = user["sessions"][-1]["session_id"]
  facts["last_recalls"] = any(
      r["label"] == "recall_memory"
      for t in traces
      if t["session_id"] == last
      for r in t["timeline"]
  )
  return facts


class _Director:
  """Moves a visible cursor and shows captions while the video records."""

  def __init__(self, page: Any) -> None:
    self.page = page

  def caption(self, text: str) -> None:
    self.page.evaluate("t => window.__demoCaption(t)", text)

  def pause(self, ms: int) -> None:
    self.page.wait_for_timeout(ms)

  def point_at(self, locator: Any, hold_ms: int = 2500) -> bool:
    """Glides the cursor onto the element; False if it is not on the page."""
    if locator.count() == 0:
      return False
    target = locator.first
    target.scroll_into_view_if_needed()
    box = target.bounding_box()
    if box is None:
      return False
    self.page.mouse.move(
        box["x"] + min(box["width"] / 2, 40),
        box["y"] + box["height"] / 2,
        steps=30,
    )
    self.pause(hold_ms)
    return True

  def click(self, locator: Any, hold_ms: int = 1500) -> None:
    """Points at the element, then presses and releases the mouse there."""
    if self.point_at(locator, hold_ms=300):
      self.page.mouse.down()
      self.page.mouse.up()
    self.pause(hold_ms)

  def scroll_to(self, selector: str, hold_ms: int = 1200) -> None:
    self.page.evaluate(
        "s => document.querySelector(s).scrollIntoView("
        "{behavior: 'smooth', block: 'start'})",
        selector,
    )
    self.pause(hold_ms)

  def scroll_top(self, hold_ms: int = 1000) -> None:
    self.page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    self.pause(hold_ms)


def _walkthrough(page: Any, base: str, facts: dict[str, Any]) -> None:
  user = facts["user"]
  d = _Director(page)
  first = user["sessions"][0]["session_id"]
  page.goto(f"{base}/index.html?user={user['user_id']}&session={first}")
  page.wait_for_selector("#graph g.node")
  d.pause(600)
  d.caption(
      f"A live ADK agent ran {facts['sessions']} sessions; the BigQuery Agent"
      f" Analytics plugin wrote {facts['rows']} rows. This page is rebuilt"
      " from those rows."
  )
  d.pause(4500)
  d.caption(
      "Short-term memory: each session's conversation, read from"
      " USER_MESSAGE_RECEIVED and LLM_RESPONSE rows."
  )
  d.point_at(page.locator(".session-btn").first, 1200)
  d.point_at(page.locator("#messages li").last, 2800)

  if "replaced" in facts:
    old, new = facts["replaced"]
    d.caption(
        f"Long-term memory: {old['category']} was saved as"
        f" {old['preference']} in session"
        f" {_session_number(user, old['session_id'])}, then replaced by"
        f" {new['preference']} in session"
        f" {_session_number(user, new['session_id'])}. The history comes from"
        " STATE_DELTA rows."
    )
    d.point_at(
        page.locator(
            f'#graph g.node[aria-label^="{old["category"]} = {old["preference"]}"]'
        ),
        3200,
    )
    d.point_at(
        page.locator(
            f'#graph g.node[aria-label^="{new["category"]} = {new["preference"]}"]'
        ),
        2600,
    )
  if "entity" in facts:
    entity = facts["entity"]
    numbers = sorted(
        {_session_number(user, m["session_id"]) for m in entity["mentions"]}
    )
    d.caption(
        f"Entities come from tool arguments: {entity['name']} was used in"
        f" session{'s' if len(numbers) > 1 else ''}"
        f" {' and '.join(str(n) for n in numbers)}."
    )
    d.point_at(
        page.locator(f'#graph g.node[aria-label^="{entity["name"]},"]'), 3200
    )

  if "failure" in facts:
    trace, row = facts["failure"]
    number = _session_number(user, trace["session_id"])
    d.caption(
        f"Reasoning: in session {number} the {row['label']} call failed"
        " (a TOOL_ERROR row), so that turn ended without an answer."
    )
    d.click(page.locator(".session-btn").nth(number - 1), 600)
    d.scroll_to("#trace-h", 900)
    d.point_at(
        page.locator(f'#traces g[aria-label^="{row["label"]}, failed"]'), 3600
    )
    if facts["retried"]:
      d.caption("The next turn retried the same tool, and it succeeded.")
      d.point_at(
          page.locator(f'#traces g[aria-label^="{row["label"]}, ok"]'), 3200
      )

  if "recall" in facts:
    trace, row = facts["recall"]
    number = _session_number(user, trace["session_id"])
    d.caption(
        f"Session {number}: the agent called recall_memory, which read the"
        " earlier sessions back from BigQuery and returned the saved"
        " preferences."
    )
    d.scroll_top(600)
    d.click(page.locator(".session-btn").nth(number - 1), 600)
    d.scroll_to("#trace-h", 900)
    d.point_at(page.locator('#traces g[aria-label^="recall_memory, ok"]'), 4200)

  if facts["last_recalls"]:
    d.caption(
        "The latest session called recall_memory again; memory returns the"
        " current version of each preference, not the replaced one."
    )
  else:
    d.caption("The latest session's conversation.")
  d.scroll_top(600)
  d.click(page.locator(".session-btn").last, 600)
  d.point_at(page.locator("#messages li").last, 3200)
  d.caption(
      "get_context(): the prompt block for the next model call, each line"
      " tagged with the session and span it came from."
  )
  d.scroll_to("#context-h", 900)
  d.pause(5200)

  if facts["other_user"] is not None:
    other = facts["other_user"]["user_id"]
    d.caption(
        "Each user is read with its own TraceFilter(user_id=...): the other"
        " user sees only their own sessions and preferences."
    )
    d.scroll_top(800)
    d.click(page.locator(f'#user-switch button[data-user="{other}"]'), 3800)
  d.caption("Every chart also has a table view.")
  d.click(page.locator("#graph-toggle"), 3000)
  d.caption("")
  d.pause(600)


def record(
    data_dir: Path, video_out: Path, screenshot_out: Optional[Path]
) -> Path:
  from playwright.sync_api import sync_playwright

  export = json.loads(
      (data_dir / "data" / "memory_export.json").read_text("utf-8")
  )
  facts = story(export)
  with serve(data_dir) as base, sync_playwright() as p:
    browser = p.chromium.launch()
    with tempfile.TemporaryDirectory() as tmp:
      context = browser.new_context(
          viewport=VIDEO_SIZE,
          record_video_dir=tmp,
          record_video_size=VIDEO_SIZE,
      )
      context.add_init_script(OVERLAY_JS)
      page = context.new_page()
      _walkthrough(page, base, facts)
      webm = Path(page.video.path())
      context.close()
      video_out.parent.mkdir(parents=True, exist_ok=True)
      if shutil.which("ffmpeg"):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(webm),
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                str(video_out),
            ],
            check=True,
        )
      else:
        video_out = video_out.with_suffix(".webm")
        shutil.copyfile(webm, video_out)
    if screenshot_out is not None:
      page = browser.new_page(viewport={"width": 1440, "height": 900})
      user = facts["user"]
      session = (
          facts["recall"][0]["session_id"]
          if "recall" in facts
          else user["current_session_id"]
      )
      page.goto(f"{base}/index.html?user={user['user_id']}&session={session}")
      page.wait_for_selector("#graph g.node")
      page.wait_for_timeout(400)
      page.screenshot(path=str(screenshot_out))
    browser.close()
  return video_out


def main(argv: Optional[list[str]] = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
  parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
  args = parser.parse_args(argv)
  video = record(HERE, args.video, args.screenshot)
  print(f"video: {video} ({video.stat().st_size / 1e6:.1f} MB)")
  print(f"screenshot: {args.screenshot}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
