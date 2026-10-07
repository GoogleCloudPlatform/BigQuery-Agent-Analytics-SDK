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

"""Hermetic tests for the narrated recording in viz/record_demo.py.

Recording needs a browser, macOS ``say`` and ffmpeg, which CI does not have.
These tests cover what decides the result without them: the narration
script, the scene plan, the timing, the captions file and the ffmpeg
command. They also check that the committed narration and captions match
the committed export and each other.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "agent_memory"
sys.path.insert(0, str(EXAMPLE_DIR / "viz"))

import record_demo  # noqa: E402

RECORDED_RUN = EXAMPLE_DIR / "recorded_run"
NARRATION = RECORDED_RUN / "narration.md"
CAPTIONS = RECORDED_RUN / "demo.srt"
EXPORT = EXAMPLE_DIR / "viz" / "data" / "memory_export.json"

SCRIPT = """\
---
label: Test run
voice: Samantha
rate: 180
---

# Narration

- Not a line: no scene has started.

```bash
## not-a-scene
- Not a line: fenced code.
```

## intro

*On screen: the page.*

- First line.
- Second line.

## table-view
- Last line.
"""


@pytest.fixture(scope="module")
def export() -> dict:
  return json.loads(EXPORT.read_text("utf-8"))


@pytest.fixture(scope="module")
def narration() -> record_demo.Narration:
  return record_demo.parse_narration(NARRATION.read_text("utf-8"))


# ---- the script --------------------------------------------------------------


def test_parse_narration_reads_front_matter_scenes_and_lines():
  narration = record_demo.parse_narration(SCRIPT)

  assert (narration.label, narration.voice, narration.rate) == (
      "Test run",
      "Samantha",
      180,
  )
  assert narration.lines == {
      "intro": ("First line.", "Second line."),
      "table-view": ("Last line.",),
  }


@pytest.mark.parametrize(
    "script,message",
    [
        ("## intro\n- A line.\n", "front matter"),
        ("---\nlabel: x\nvoice: y\nrate: 9\n## intro\n- A.\n", "closing ---"),
        ("---\nlabel x\n---\n", "key: value"),
        ("---\nlabel: x\nvoice: y\n---\n## intro\n- A.\n", "missing rate"),
        (
            "---\nlabel: x\nvoice: y\nrate: fast\n---\n## intro\n- A.\n",
            "words per minute",
        ),
        (
            "---\nlabel: x\nvoice: y\nrate: 9\n---\n## intro\n- A.\n"
            "## intro\n- B.\n",
            "twice",
        ),
        (
            "---\nlabel: x\nvoice: y\nrate: 9\n---\n## intro\n*A note.*\n",
            "without lines: intro",
        ),
    ],
    ids=[
        "no-front-matter",
        "unclosed-front-matter",
        "not-key-value",
        "missing-key",
        "rate-not-a-number",
        "duplicate-scene",
        "scene-without-lines",
    ],
)
def test_parse_narration_rejects_malformed_scripts(script, message):
  with pytest.raises(ValueError, match=message):
    record_demo.parse_narration(script)


def test_the_recorded_narration_fits_the_recorded_export(export, narration):
  scenes = record_demo.scene_plan(record_demo.story(export))

  record_demo.check_narration(narration, export, scenes)
  # Same scenes, in the order they are on screen.
  assert list(narration.lines) == [scene.key for scene in scenes]


def test_a_script_for_another_run_is_refused(export, narration):
  scenes = record_demo.scene_plan(record_demo.story(export))

  with pytest.raises(ValueError, match="narrates the export labeled"):
    record_demo.check_narration(
        narration, dict(export, label="Another run"), scenes
    )
  with pytest.raises(ValueError, match="differ from this export's scenes"):
    record_demo.check_narration(narration, export, scenes[:-1])


def _user(user_id, sessions, preferences=(), entities=(), name=None, reuse=()):
  """An exported user; sessions in ``reuse`` recalled SQL to reuse."""
  return {
      "user_id": user_id,
      "name": name,
      "sessions": [
          {"session_id": sid, "row_count": 10, "messages": [], "state": {}}
          for sid in sessions
      ],
      "preferences": list(preferences),
      "entities": list(entities),
      "traces": [
          {
              "session_id": sid,
              "recall": {"text": "- 0.6 trace t: ...\n  reuse: run_sql({})"},
          }
          for sid in reuse
      ],
  }


def _side(session_id, tools):
  return {"session_id": session_id, "tool_calls": tools}


def test_scene_plan_has_only_the_scenes_the_export_has_facts_for():
  export = {"users": [_user("u", ["s-1"])], "comparisons": []}

  facts = record_demo.story(export)

  assert [scene.key for scene in record_demo.scene_plan(facts)] == [
      "intro",
      "week",
      "graph",
      "reasoning",
      "recall",
  ]
  assert facts["focus_session"]["session_id"] == "s-1"


def test_story_picks_the_sessions_and_analysts_each_scene_shows():
  entities = [
      {"name": "Maya  Chen", "mentions": [{}, {}, {}]},  # the analyst
      {"name": "Jeans", "mentions": [{}]},
      {"name": "Women's  Dresses", "mentions": [{}, {}]},
  ]
  replaced = [{"valid_until": "2026-10-05T00:00:00Z"}, {"valid_until": None}]
  users = [
      _user(
          "maya",
          ["m-1", "m-2"],
          preferences=replaced,
          entities=entities,
          name="Maya Chen",
      ),
      _user("raj", ["r-1"], preferences=replaced),
      _user("lena", ["l-1", "l-2"], reuse=["l-2"]),
      _user("tom", ["t-1"]),
      _user("diego", ["d-1"]),
  ]
  export = {
      "run": {"days": [{}, {}], "totals": {"rows": 900, "sessions": 6}},
      "users": users,
      "comparisons": [
          {
              "name": "Maya",
              "day": 3,
              "with_memory": _side("m-2", ["recall_memory", "run_sql"]),
              "without_memory": _side("m-2-ctl", ["run_sql"] * 5),
          },
          {
              # The run without memory read the memory tables: not shown.
              "name": "Lena",
              "day": 4,
              "with_memory": _side("l-2", ["recall_memory", "run_sql"]),
              "without_memory": _side("l-2-ctl", ["run_sql"] * 4),
              "control_read_memory": True,
          },
          {
              "name": "Diego",
              "day": 5,
              "with_memory": _side("d-1", ["recall_memory"] + ["run_sql"] * 2),
              "without_memory": _side("d-1-ctl", ["run_sql"] * 6),
              "control_read_memory": False,
          },
          {
              "name": "Raj",
              "day": 5,
              "with_memory": None,
              "without_memory": None,
          },
      ],
  }

  facts = record_demo.story(export)
  scenes = record_demo.scene_plan(facts)

  assert [scene.key for scene in scenes] == [
      "intro",
      "compare-1",
      "compare-2",
      "week",
      "graph",
      "preferences",
      "reasoning",
      "recall",
      "other-user",
  ]
  assert [pair["name"] for pair in facts["comparisons"]] == ["Maya", "Diego"]
  # The web view's tabs follow the export: Diego's is the third.
  assert (facts["compare_tabs"], facts["flawed_tabs"]) == ([0, 2], [1])
  assert (
      facts["focus_user"]["user_id"],
      facts["focus_session"]["session_id"],
  ) == ("maya", "m-2")
  # The most-mentioned entity other than the analyst.
  assert facts["focus_entity"]["name"] == "Women's  Dresses"
  assert facts["pref_user"]["user_id"] == "raj"
  # Lena's session recalled SQL to reuse; only her run without memory is
  # flawed, so her session with memory can still show reasoning.
  assert (
      facts["reuse_user"]["user_id"],
      facts["reuse_session"]["session_id"],
  ) == ("lena", "l-2")
  assert facts["other_user"]["user_id"] == "tom"
  assert scenes[0].caption.startswith(
      "5 analysts used one ADK data-analyst agent over 2 days: 6 sessions, 900 rows"
  )
  assert (
      "without memory (5 tool calls) and with memory (2)" in scenes[1].caption
  )
  assert scenes[2].caption.startswith("Diego, Day 5")


def test_story_falls_back_when_no_comparison_is_clean_or_reuses_sql():
  users = [_user("maya", ["m-1"]), _user("lena", ["l-1"])]
  export = {
      "users": users,
      "comparisons": [
          {
              "name": name,
              "day": 1,
              "with_memory": _side(sid, ["run_sql"]),
              "without_memory": _side(f"{sid}-ctl", ["run_sql"]),
              "control_read_memory": True,
          }
          for name, sid in (("Maya", "m-1"), ("Lena", "l-1"))
      ],
  }

  facts = record_demo.story(export)

  assert [pair["name"] for pair in facts["comparisons"]] == ["Maya", "Lena"]
  assert (facts["compare_tabs"], facts["flawed_tabs"]) == ([0, 1], [0, 1])
  assert facts["reuse_session"]["session_id"] == "l-1"


def test_entity_ids_and_the_start_page_match_the_web_view():
  facts = {
      "focus_user": {"user_id": "maya.chen"},
      "focus_session": {"session_id": "an-t-d3-maya-2"},
  }

  # app.js keys an entity node by its name, spaces collapsed, lower case.
  assert record_demo._entity_id("Women's  Dresses ") == "e:women's dresses"
  assert record_demo._start_url("http://127.0.0.1:9", facts) == (
      "http://127.0.0.1:9/index.html?user=maya.chen"
      "&session=an-t-d3-maya-2&compare=0"
  )
  # Opens on the tab of the first comparison shown.
  assert record_demo._start_url(
      "http://127.0.0.1:9", dict(facts, compare_tabs=[2, 4])
  ).endswith("&compare=2")


# ---- timing and captions -----------------------------------------------------


def test_place_lines_leads_in_and_spaces_the_lines():
  placed = record_demo.place_lines(10.0, [2.0, 3.0])

  # 0.4 s before the first line, 0.35 s between lines.
  assert [(round(a, 3), round(b, 3)) for a, b in placed] == [
      (10.4, 12.4),
      (12.75, 15.75),
  ]


def test_srt_round_trips_and_counts_hours():
  cues = [
      record_demo.Cue(1.0834, 8.075, "First line."),
      record_demo.Cue(3725.5, 3730.25, "Two\nrows."),
  ]

  text = record_demo.to_srt(cues)
  parsed = record_demo.parse_srt(text)

  assert text.splitlines()[:4] == [
      "1",
      "00:00:01,083 --> 00:00:08,075",
      "First line.",
      "",
  ]
  assert "2\n01:02:05,500 --> 01:02:10,250\nTwo\nrows.\n" in text
  assert [cue.text for cue in parsed] == ["First line.", "Two\nrows."]
  assert [(cue.start, cue.end) for cue in parsed] == [
      pytest.approx((1.083, 8.075)),
      pytest.approx((3725.5, 3730.25)),
  ]


@pytest.mark.parametrize(
    "text",
    [
        "1\n",
        "1\n00:00:01,000\nNo arrow.\n",
        "1\n00:00:01 --> 00:00:02\nNo milliseconds.\n",
    ],
    ids=["no-timing", "no-arrow", "no-milliseconds"],
)
def test_parse_srt_rejects_malformed_blocks(text):
  with pytest.raises(ValueError, match="SRT"):
    record_demo.parse_srt(text)


def test_the_committed_captions_are_the_narration_in_order(narration):
  cues = record_demo.parse_srt(CAPTIONS.read_text("utf-8"))

  assert [cue.text for cue in cues] == [
      line for lines in narration.lines.values() for line in lines
  ]
  assert cues[0].start > 0
  for cue, after in itertools.pairwise(cues):
    assert cue.start < cue.end < after.start
  assert cues[-1].end < 120  # the video stays under two minutes


def test_compose_args_places_each_line_and_burns_each_caption_in_its_window():
  args = record_demo.compose_args(
      Path("walk.webm"),
      [(Path("a.aiff"), 1.0834), (Path("b.aiff"), 8.175)],
      [
          (Path("c1.png"), record_demo.Cue(1.083, 8.075, "First.")),
          (Path("c2.png"), record_demo.Cue(8.175, 16.912, "Second.")),
      ],
      Path("out.mp4"),
  )

  inputs = [args[i + 1] for i, arg in enumerate(args) if arg == "-i"]
  assert inputs == ["walk.webm", "a.aiff", "b.aiff", "c1.png", "c2.png"]
  assert args[args.index("-filter_complex") + 1].split(";") == [
      "[1:a]adelay=delays=1083:all=1[a1]",
      "[2:a]adelay=delays=8175:all=1[a2]",
      "[a1][a2]amix=inputs=2:duration=longest:normalize=0,apad[voice]",
      "[0:v]setpts=PTS-STARTPTS[v0]",
      "[v0][3:v]overlay=x=(W-w)/2:y=H-h-24"
      ":enable='between(t,1.083,8.075)'[v1]",
      "[v1][4:v]overlay=x=(W-w)/2:y=H-h-24"
      ":enable='between(t,8.175,16.912)'[v2]",
      "[v2]format=yuv420p[video]",
  ]
  # The voiceover is padded, so the video decides where the file ends.
  assert "-shortest" in args
  assert args[-1] == "out.mp4"


# ---- the command line ----------------------------------------------------------


def test_cli_writes_the_narrated_video_and_captions_next_to_the_script(
    tmp_path, monkeypatch
):
  monkeypatch.setattr(record_demo.shutil, "which", lambda tool: f"/bin/{tool}")
  video = tmp_path / "demo.mp4"
  video.write_bytes(b"mp4")
  calls = []

  def fake_record(data_dir, video_out, screenshot, narration, srt_out):
    calls.append((video_out, srt_out, narration.voice))
    return video

  monkeypatch.setattr(record_demo, "record", fake_record)

  assert record_demo.main(["--narration", str(NARRATION)]) == 0
  assert calls == [
      (RECORDED_RUN / "demo.mp4", RECORDED_RUN / "demo.srt", "Samantha")
  ]


def test_cli_refuses_a_script_for_another_run(tmp_path, monkeypatch, capsys):
  script = tmp_path / "narration.md"
  script.write_text(SCRIPT, encoding="utf-8")
  monkeypatch.setattr(record_demo.shutil, "which", lambda tool: f"/bin/{tool}")
  monkeypatch.setattr(
      record_demo, "record", lambda *args: pytest.fail("must not record")
  )

  with pytest.raises(SystemExit) as exited:
    record_demo.main(["--narration", str(script)])

  assert exited.value.code == 2
  assert "narrates the export labeled 'Test run'" in capsys.readouterr().err


def test_cli_narration_needs_say_and_ffmpeg(monkeypatch, capsys):
  monkeypatch.setattr(record_demo.shutil, "which", lambda tool: None)

  with pytest.raises(SystemExit):
    record_demo.main(["--narration", str(NARRATION)])

  assert "needs say, ffmpeg, ffprobe" in capsys.readouterr().err


def test_cli_captions_need_a_narration(capsys):
  with pytest.raises(SystemExit):
    record_demo.main(["--srt", "demo.srt"])

  assert "--srt needs --narration" in capsys.readouterr().err
