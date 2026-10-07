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
with ffmpeg. The cursor is injected for the recording only; the page has
none.

Without ``--narration``, a caption bar written from the export itself
describes whatever run ``data/memory_export.json`` holds. With
``--narration FILE``, the walkthrough is narrated from that script instead:
every line is spoken with macOS ``say`` into the audio track, each scene
stays on screen until its lines end, and the captions are written to an
``.srt`` file and burned into the video. ffmpeg builds without libass cannot
burn an ``.srt`` directly, so each caption is drawn as an image in the same
headless Chromium and composited with ffmpeg's ``overlay`` filter at the
times the ``.srt`` gives.

  pip install playwright && python -m playwright install chromium
  python examples/agent_memory/viz/record_demo.py
  python examples/agent_memory/viz/record_demo.py --narration \\
      examples/agent_memory/recorded_run/narration.md
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import http.server
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_VIDEO = HERE.parent / "demo.mp4"
DEFAULT_SCREENSHOT = HERE / "screenshot.png"
VIDEO_SIZE = {"width": 1280, "height": 800}

# Narrated timing, in seconds of video.
LEAD_S = 0.4  # before a scene's first line
GAP_S = 0.35  # between two lines of one scene
HOLD_S = 0.7  # after a scene's last line, before the next scene
CAPTION_TAIL_S = 0.25  # a caption stays up this long after its line ends

# Burned-in captions look like the caption bar, a little larger.
CAPTION_CSS = (
    "display:inline-block;max-width:1000px;padding:12px 22px;"
    "border-radius:10px;background:rgba(18,24,32,.88);color:#fff;"
    "font:500 19px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif;"
    "text-align:center;white-space:pre-line"
)
CAPTION_BOTTOM = 24  # px between a caption and the bottom of the frame

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


def _find(users: list[dict], session_id: str) -> tuple[dict, dict]:
  for user in users:
    for session in user["sessions"]:
      if session["session_id"] == session_id:
        return user, session
  raise KeyError(session_id)


def _entity_id(name: str) -> str:
  """The graph node id app.js gives an entity."""
  return "e:" + " ".join(name.split()).lower()


def _reuses_sql(users: list[dict], session_id: str) -> bool:
  """Whether the session's recalled memory offered SQL to reuse."""
  return any(
      trace["session_id"] == session_id
      and "reuse: " in ((trace.get("recall") or {}).get("text") or "")
      for user in users
      for trace in user.get("traces", [])
  )


def story(export: dict) -> dict[str, Any]:
  """What the walkthrough shows, chosen from the export.

  The first two before/after comparisons whose run without memory did not
  read the memory tables (any two if none qualify). The first one's
  analyst and session for the week and the long-term memory, with the
  entity mentioned most other than the analyst; an analyst with a
  replaced preference (another one if possible); for reasoning and
  recalled memory, a session from another comparison whose recall offered
  SQL to reuse (else the second comparison's); and one more analyst for
  isolation. Absent ones are None.
  """
  users = export["users"]
  run = export.get("run") or {}
  totals = run.get("totals") or {}
  # The web view has one tab per comparison, in this order.
  tabs = export.get("comparisons") or []
  pairs = [c for c in tabs if c.get("with_memory") and c.get("without_memory")]
  clean = [c for c in pairs if not c.get("control_read_memory")]
  shown = (clean or pairs)[:2]
  facts: dict[str, Any] = {
      "users": users,
      "days": run.get("days") or [],
      "rows": totals.get("rows")
      or sum(s["row_count"] for u in users for s in u["sessions"]),
      "sessions": totals.get("sessions")
      or sum(len(u["sessions"]) for u in users),
      "comparisons": shown,
      # Tab indexes of the comparisons shown, and of the flawed ones.
      "compare_tabs": [
          next(i for i, tab in enumerate(tabs) if tab is pair) for pair in shown
      ],
      "flawed_tabs": [
          i for i, tab in enumerate(tabs) if tab.get("control_read_memory")
      ],
  }
  if shown:
    facts["focus_user"], facts["focus_session"] = _find(
        users, shown[0]["with_memory"]["session_id"]
    )
  else:
    facts["focus_user"] = users[0]
    facts["focus_session"] = users[0]["sessions"][-1]
  focus = facts["focus_user"]
  # The analyst's own name is usually the most-mentioned entity; skip it.
  own = _entity_id(focus.get("name") or "")
  facts["focus_entity"] = max(
      (e for e in focus["entities"] if _entity_id(e["name"]) != own),
      key=lambda e: len(e["mentions"]),
      default=None,
  )
  replaced = [
      u for u in users if any(p["valid_until"] for p in u["preferences"])
  ]
  # Prefer another analyst than the focus one, so the scene shows someone new.
  facts["pref_user"] = next(
      (u for u in replaced if u is not focus), replaced[0] if replaced else None
  )
  reuse = next(
      (
          c["with_memory"]["session_id"]
          for c in pairs
          if c not in shown
          and _reuses_sql(users, c["with_memory"]["session_id"])
      ),
      shown[1]["with_memory"]["session_id"] if len(shown) > 1 else None,
  )
  facts["reuse_user"], facts["reuse_session"] = (
      _find(users, reuse) if reuse else (focus, facts["focus_session"])
  )
  seen = {id(focus), id(facts["pref_user"]), id(facts["reuse_user"])}
  facts["other_user"] = next((u for u in users if id(u) not in seen), None)
  return facts


@dataclasses.dataclass(frozen=True)
class Scene:
  """One step of the walkthrough.

  ``caption`` is the caption bar's text when there is no narration; None
  keeps the previous caption up.
  """

  key: str
  caption: Optional[str]


def scene_plan(facts: dict[str, Any]) -> list[Scene]:
  """The scenes of the walkthrough for this export, in order."""
  days = len(facts["days"])
  scenes = [
      Scene(
          "intro",
          f"{len(facts['users'])} analysts used one ADK data-analyst agent"
          f"{f' over {days} days' if days else ''}: {facts['sessions']}"
          f" sessions, {facts['rows']} rows logged by the BigQuery Agent"
          " Analytics plugin.",
      )
  ]
  for number, pair in enumerate(facts["comparisons"], start=1):
    scenes.append(
        Scene(
            f"compare-{number}",
            f"{pair['name']}, Day {pair['day']}: the same question without"
            f" memory ({len(pair['without_memory']['tool_calls'])} tool"
            " calls) and with memory"
            f" ({len(pair['with_memory']['tool_calls'])}).",
        )
    )
  scenes.append(
      Scene(
          "week",
          "The week, by analyst and day. Arcs show the earlier sessions the"
          " selected session's memory recall cited.",
      )
  )
  scenes.append(
      Scene(
          "graph",
          "Long-term memory: entities and facts that AI.GENERATE extracted"
          " from the logged messages, each with its source row.",
      )
  )
  if facts["pref_user"] is not None:
    scenes.append(
        Scene(
            "preferences",
            "Preferences the agent saved as ADK user: state; a replaced"
            " version stays in the history.",
        )
    )
  scenes.append(
      Scene(
          "reasoning",
          "Reasoning: recall_memory returned a past analysis with its SQL,"
          " and the agent ran it again.",
      )
  )
  scenes.append(
      Scene(
          "recall",
          "What the agent read: preferences, facts, similar past analyses"
          " and earlier failures, each with its session and span.",
      )
  )
  if facts["other_user"] is not None:
    scenes.append(
        Scene(
            "other-user",
            "Each analyst is read with their own TraceFilter(user_id=...):"
            " no one else's rows.",
        )
    )
  return scenes


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


def _act(key: str, page: Any, d: _Director, facts: dict[str, Any]) -> None:
  """What the cursor does in one scene."""
  if key == "intro":
    d.point_at(page.locator("#run-facts"), 3500)
  elif key.startswith("compare-"):
    number = int(key.split("-")[1]) - 1
    tab = facts["compare_tabs"][number]
    d.scroll_to("#compare-panel", 700)
    d.click(page.locator(f'#compare-tabs button[data-compare="{tab}"]'), 700)
    d.point_at(page.locator("#compare-without .stat-row"), 3200)
    d.point_at(page.locator("#compare-with .stat-row"), 2200)
    d.point_at(page.locator("#compare-with .recalled .chip"), 2400)
    if number == len(facts["compare_tabs"]) - 1 and facts["flawed_tabs"]:
      # The tabs of comparisons whose run without memory read memory.
      flawed = facts["flawed_tabs"][0]
      d.point_at(
          page.locator(f'#compare-tabs button[data-compare="{flawed}"]'), 2400
      )
  elif key == "week":
    session = facts["focus_session"]["session_id"]
    d.scroll_to("#week-panel", 900)
    d.point_at(page.locator(f'#week g[data-session="{session}"]'), 2600)
    d.point_at(page.locator(f'#week g[data-session="{session}:control"]'), 2400)
  elif key == "graph":
    d.scroll_to("#graph-h", 900)
    d.point_at(page.locator('#graph g.node[data-id="self"]'), 2400)
    entity = facts["focus_entity"]
    if entity is not None:
      d.point_at(
          page.locator(
              f'#graph g.node[data-id="{_entity_id(entity["name"])}"]'
          ),
          2600,
      )
    d.point_at(page.locator("#memory-lists .item-list button.link").last, 2400)
  elif key == "preferences":
    user = facts["pref_user"]["user_id"]
    d.click(page.locator(f'#user-switch button[data-user="{user}"]'), 900)
    d.scroll_to("#graph-h", 900)
    d.point_at(page.locator("#memory-lists .pref-old").first, 3600)
  elif key == "reasoning":
    user = facts["reuse_user"]["user_id"]
    session = facts["reuse_session"]["session_id"]
    d.click(page.locator(f'#user-switch button[data-user="{user}"]'), 700)
    d.click(page.locator(f'.session-btn[data-session="{session}"]'), 700)
    d.scroll_to("#trace-h", 900)
    d.point_at(page.locator('#traces g[aria-label^="recall_memory"]'), 2400)
    d.point_at(page.locator('#traces g[aria-label^="run_sql"]'), 2600)
  elif key == "recall":
    d.scroll_to("#context-h", 900)
    d.pause(4200)
  elif key == "other-user":
    user = facts["other_user"]["user_id"]
    d.click(page.locator(f'#user-switch button[data-user="{user}"]'), 900)
    d.scroll_to("#graph-h", 900)
    d.point_at(page.locator("#filter-note"), 3000)
  else:
    raise ValueError(f"unknown scene {key!r}")


# ---- narration ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Narration:
  """A narration script: the voice, and the spoken lines of each scene."""

  label: str  # the label of the export the lines were written for
  voice: str  # a macOS ``say`` voice
  rate: int  # words per minute
  lines: dict[str, tuple[str, ...]]


@dataclasses.dataclass(frozen=True)
class Cue:
  """One caption and when it is on screen, in seconds of video."""

  start: float
  end: float
  text: str


@dataclasses.dataclass(frozen=True)
class _Line:
  """One spoken line and its audio."""

  text: str
  audio: Path
  seconds: float


def parse_narration(text: str) -> Narration:
  """Reads a narration script.

  The script opens with front matter (``label``, ``voice`` and ``rate``
  between ``---`` lines). Each ``## <scene>`` heading starts a scene, and
  the ``- `` bullets under it are its spoken lines, one caption each. Other
  text, and anything inside fenced code, is for readers and is skipped.
  """
  rows = text.splitlines()
  if not rows or rows[0].strip() != "---":
    raise ValueError("the script must start with front matter (---)")
  try:
    close = rows.index("---", 1)
  except ValueError:
    raise ValueError("the front matter has no closing ---") from None
  meta = {}
  for row in rows[1:close]:
    key, sep, value = row.partition(":")
    if not sep:
      raise ValueError(f"front matter line {row!r} is not 'key: value'")
    meta[key.strip()] = value.strip()
  missing = sorted({"label", "voice", "rate"} - meta.keys())
  if missing:
    raise ValueError(f"the front matter is missing {', '.join(missing)}")
  if not meta["rate"].isdigit():
    raise ValueError(f"rate must be words per minute, got {meta['rate']!r}")

  lines: dict[str, list[str]] = {}
  scene = None
  fenced = False
  for row in rows[close + 1 :]:
    if row.startswith("```"):
      fenced = not fenced
    elif fenced:
      continue
    elif row.startswith("## "):
      scene = row[3:].strip()
      if scene in lines:
        raise ValueError(f"scene {scene!r} appears twice")
      lines[scene] = []
    elif row.startswith("- ") and scene is not None:
      lines[scene].append(row[2:].strip())
  silent = [key for key, spoken in lines.items() if not spoken]
  if silent:
    raise ValueError(f"scenes without lines: {', '.join(silent)}")
  return Narration(
      label=meta["label"],
      voice=meta["voice"],
      rate=int(meta["rate"]),
      lines={key: tuple(spoken) for key, spoken in lines.items()},
  )


def check_narration(
    narration: Narration, export: dict, scenes: list[Scene]
) -> None:
  """Raises ValueError unless the narration was written for this export."""
  if narration.label != export.get("label"):
    raise ValueError(
        f"the script narrates the export labeled {narration.label!r}, not"
        f" {export.get('label')!r}"
    )
  keys = [scene.key for scene in scenes]
  if set(narration.lines) != set(keys):
    raise ValueError(
        f"the script's scenes {sorted(narration.lines)} differ from this"
        f" export's scenes {keys}"
    )


def place_lines(
    start: float, durations: list[float]
) -> list[tuple[float, float]]:
  """When each spoken line of a scene that starts at ``start`` plays."""
  placed = []
  at = start + LEAD_S
  for seconds in durations:
    placed.append((at, at + seconds))
    at += seconds + GAP_S
  return placed


def srt_timestamp(seconds: float) -> str:
  ms = round(seconds * 1000)
  hours, ms = divmod(ms, 3_600_000)
  minutes, ms = divmod(ms, 60_000)
  secs, ms = divmod(ms, 1000)
  return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
  return "\n".join(
      f"{number}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n"
      f"{cue.text}\n"
      for number, cue in enumerate(cues, start=1)
  )


_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d),(\d\d\d)")


def _srt_seconds(stamp: str) -> float:
  match = _SRT_TIME.fullmatch(stamp.strip())
  if match is None:
    raise ValueError(f"bad SRT timestamp {stamp!r}")
  hours, minutes, secs, ms = (int(part) for part in match.groups())
  return hours * 3600 + minutes * 60 + secs + ms / 1000


def parse_srt(text: str) -> list[Cue]:
  cues = []
  for block in text.replace("\r\n", "\n").strip().split("\n\n"):
    rows = block.split("\n")
    start, sep, end = (
        rows[1].partition(" --> ") if len(rows) > 2 else ("", "", "")
    )
    if not sep:
      raise ValueError(f"bad SRT block {block!r}")
    cues.append(
        Cue(_srt_seconds(start), _srt_seconds(end), "\n".join(rows[2:]))
    )
  return cues


def compose_args(
    webm: Path,
    voice: list[tuple[Path, float]],
    captions: list[tuple[Path, Cue]],
    out: Path,
) -> list[str]:
  """The ffmpeg command that adds the voiceover and burns in the captions.

  ``voice`` holds each spoken line's audio file and start time, and
  ``captions`` each caption's image and cue. Inputs are numbered in that
  order after the video: 0 is the video, then the audio, then the images.
  """
  args = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm)]
  graph = []
  for number, (audio, start) in enumerate(voice, start=1):
    args += ["-i", str(audio)]
    graph.append(
        f"[{number}:a]adelay=delays={round(start * 1000)}:all=1[a{number}]"
    )
  graph.append(
      "".join(f"[a{number}]" for number in range(1, len(voice) + 1))
      + f"amix=inputs={len(voice)}:duration=longest:normalize=0,apad[voice]"
  )
  graph.append("[0:v]setpts=PTS-STARTPTS[v0]")
  for number, (image, cue) in enumerate(captions, start=1):
    args += ["-i", str(image)]
    graph.append(
        f"[v{number - 1}][{len(voice) + number}:v]overlay="
        f"x=(W-w)/2:y=H-h-{CAPTION_BOTTOM}"
        f":enable='between(t,{cue.start:.3f},{cue.end:.3f})'[v{number}]"
    )
  graph.append(f"[v{len(captions)}]format=yuv420p[video]")
  return args + [
      "-filter_complex",
      ";".join(graph),
      *"-map [video] -map [voice] -c:v libx264 -preset slow -crf 30".split(),
      *"-c:a aac -b:a 96k -ar 44100 -ac 1 -shortest -movflags +faststart".split(),
      str(out),
  ]


def _seconds(audio: Path) -> float:
  probe = subprocess.run(
      [
          *"ffprobe -v error -show_entries format=duration -of csv=p=0".split(),
          str(audio),
      ],
      check=True,
      capture_output=True,
      text=True,
  )
  return float(probe.stdout)


def _speak(narration: Narration, folder: Path) -> dict[str, list[_Line]]:
  """Speaks every line of the script to an AIFF file with macOS ``say``."""
  voice = ["-v", narration.voice, "-r", str(narration.rate)]
  spoken: dict[str, list[_Line]] = {}
  for key, lines in narration.lines.items():
    spoken[key] = []
    for number, text in enumerate(lines, start=1):
      script = folder / f"{key}-{number}.txt"
      script.write_text(text, encoding="utf-8")
      audio = script.with_suffix(".aiff")
      subprocess.run(
          ["say", *voice, "-f", str(script), "-o", str(audio)], check=True
      )
      spoken[key].append(_Line(text, audio, _seconds(audio)))
  return spoken


def _caption_images(browser: Any, cues: list[Cue], folder: Path) -> list[Path]:
  """Draws each caption as a PNG with a transparent surround."""
  page = browser.new_page(viewport=VIDEO_SIZE)
  page.set_content(
      '<body style="margin:0;background:transparent">'
      f'<div id="caption" style="{CAPTION_CSS}"></div></body>'
  )
  images = []
  for number, cue in enumerate(cues, start=1):
    page.evaluate(
        "t => { document.getElementById('caption').textContent = t; }",
        cue.text,
    )
    image = folder / f"caption-{number:02d}.png"
    page.locator("#caption").screenshot(path=str(image), omit_background=True)
    images.append(image)
  page.close()
  return images


# ---- recording ---------------------------------------------------------------


def _walkthrough(
    page: Any,
    base: str,
    facts: dict[str, Any],
    scenes: list[Scene],
    spoken: Optional[dict[str, list[_Line]]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> list[tuple[_Line, float]]:
  """Plays the scenes; with ``spoken`` lines, returns when each one starts.

  Narrated, a scene's lines start as the scene does, and the scene stays on
  screen until its last line has ended.
  """
  d = _Director(page)
  page.goto(_start_url(base, facts))
  page.wait_for_selector("#graph g.node")
  d.pause(600)
  placed: list[tuple[_Line, float]] = []
  for scene in scenes:
    if spoken is None:
      if scene.caption is not None:
        d.caption(scene.caption)
      _act(scene.key, page, d, facts)
      continue
    lines = spoken[scene.key]
    times = place_lines(clock(), [line.seconds for line in lines])
    placed += [
        (line, start) for line, (start, _) in zip(lines, times, strict=True)
    ]
    _act(scene.key, page, d, facts)
    wait = times[-1][1] + HOLD_S - clock()
    if wait > 0:
      d.pause(round(wait * 1000))
  d.caption("")
  d.pause(600)
  return placed


def _start_url(base: str, facts: dict[str, Any]) -> str:
  """The page with the first comparison and its session open."""
  user = facts["focus_user"]["user_id"]
  session = facts["focus_session"]["session_id"]
  tab = (facts.get("compare_tabs") or [0])[0]
  return f"{base}/index.html?user={user}&session={session}&compare={tab}"


def _encode(webm: Path, video_out: Path) -> Path:
  if not shutil.which("ffmpeg"):
    video_out = video_out.with_suffix(".webm")
    shutil.copyfile(webm, video_out)
    return video_out
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
  return video_out


def record(
    data_dir: Path,
    video_out: Path,
    screenshot_out: Optional[Path],
    narration: Optional[Narration] = None,
    srt_out: Optional[Path] = None,
) -> Path:
  """Records the walkthrough; with ``narration``, voiced and captioned.

  A narrated recording also writes its captions to ``srt_out`` and burns in
  exactly what that file says.
  """
  from playwright.sync_api import sync_playwright

  export = json.loads(
      (data_dir / "data" / "memory_export.json").read_text("utf-8")
  )
  facts = story(export)
  scenes = scene_plan(facts)
  if narration is not None:
    check_narration(narration, export, scenes)
    if srt_out is None:
      raise ValueError("a narrated recording needs srt_out")
  with tempfile.TemporaryDirectory() as tmp_name:
    tmp = Path(tmp_name)
    spoken = _speak(narration, tmp) if narration is not None else None
    with serve(data_dir) as base, sync_playwright() as p:
      browser = p.chromium.launch()
      context = browser.new_context(
          viewport=VIDEO_SIZE,
          record_video_dir=str(tmp / "video"),
          record_video_size=VIDEO_SIZE,
      )
      context.add_init_script(OVERLAY_JS)
      page = context.new_page()
      # The video starts with the page; narration times count from here.
      started = time.monotonic()
      placed = _walkthrough(
          page,
          base,
          facts,
          scenes,
          spoken,
          clock=lambda: time.monotonic() - started,
      )
      webm = Path(page.video.path())
      context.close()
      video_out.parent.mkdir(parents=True, exist_ok=True)
      if spoken is None:
        video_out = _encode(webm, video_out)
      else:
        srt_out.write_text(
            to_srt(
                [
                    Cue(start, start + line.seconds + CAPTION_TAIL_S, line.text)
                    for line, start in placed
                ]
            ),
            encoding="utf-8",
        )
        cues = parse_srt(srt_out.read_text("utf-8"))
        images = _caption_images(browser, cues, tmp)
        subprocess.run(
            compose_args(
                webm,
                [(line.audio, start) for line, start in placed],
                list(zip(images, cues, strict=True)),
                video_out,
            ),
            check=True,
        )
      if screenshot_out is not None:
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(_start_url(base, facts))
        page.wait_for_selector("#graph g.node")
        page.wait_for_timeout(400)
        page.screenshot(path=str(screenshot_out))
      browser.close()
  return video_out


def main(argv: Optional[list[str]] = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--narration",
      type=Path,
      help="narration script: voice the walkthrough and burn in its captions",
  )
  parser.add_argument(
      "--video",
      type=Path,
      help=(
          f"default: {DEFAULT_VIDEO.name} next to the narration script, else"
          f" {DEFAULT_VIDEO}"
      ),
  )
  parser.add_argument(
      "--srt",
      type=Path,
      help="captions file to write; default: demo.srt next to the script",
  )
  parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
  args = parser.parse_args(argv)
  narration = None
  video, srt = args.video or DEFAULT_VIDEO, None
  if args.narration is not None:
    missing = [t for t in ("say", "ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
      parser.error(f"--narration needs {', '.join(missing)} on the PATH")
    export = json.loads(
        (HERE / "data" / "memory_export.json").read_text("utf-8")
    )
    try:
      narration = parse_narration(args.narration.read_text("utf-8"))
      check_narration(narration, export, scene_plan(story(export)))
    except ValueError as error:
      parser.error(f"{args.narration}: {error}")
    video = args.video or args.narration.parent / DEFAULT_VIDEO.name
    srt = args.srt or args.narration.parent / "demo.srt"
  elif args.srt is not None:
    parser.error("--srt needs --narration")
  video = record(HERE, video, args.screenshot, narration, srt)
  print(f"video: {video} ({video.stat().st_size / 1e6:.1f} MB)")
  if srt is not None:
    print(f"captions: {srt}")
  print(f"screenshot: {args.screenshot}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
