"""Unit tests for compose_sessions.py.

Hermetic: they build a tiny synthetic PinchBench root under a temp dir, so no
real checkout is required. The real sessions are additionally exercised by the
``--check`` invocation documented in the README.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compose_sessions as cs  # noqa: E402


def task_md(task_id, *, grading_type="automated", prompt=None,
            grade=None, fixtures=None, extra_meta=""):
    if prompt is None:
        prompt = f"Do the thing and save to `out_{task_id}.txt`."
    if grade is None:
        grade = (
            "def grade(transcript, workspace_path):\n"
            "    from pathlib import Path\n"
            "    workspace = Path(workspace_path)\n"
            f"    target = workspace / \"out_{task_id}.txt\"\n"
            "    return {\"created\": 1.0 if target.exists() else 0.0}\n"
        )
    fm = [
        f"id: {task_id}",
        f"grading_type: {grading_type}",
        extra_meta.rstrip(),
    ]
    if fixtures is not None:
        fm.append("workspace_files:")
        fm.append(fixtures)
    front = "\n".join(line for line in fm if line != "") + "\n"
    return (
        f"---\n{front}---\n\n"
        "## Prompt\n\n" + prompt + "\n\n"
        "## Automated Checks\n\n```python\n" + grade + "```\n"
    )


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        (self.root / "tasks").mkdir(parents=True)
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def write_task(self, task_id, **kwargs):
        (self.root / "tasks" / f"{task_id}.md").write_text(task_md(task_id, **kwargs))

    def write_manifest(self, text, name="mini-day"):
        path = self.sessions / f"{name}.yaml"
        path.write_text(text)
        return path

    def basic_manifest(self, **overrides):
        manifest = {
            "name": "mini-day",
            "category": "coding",
            "threads": {"a": ["task_alpha"], "b": ["task_beta"]},
            "order": ["a.0", "b.0", "synth.0"],
            "synthesis": [
                {"id": "wrap", "prompt": "sum up",
                 "checks": [{"key": "recall",
                             "python": 'return 1.0 if "alpha" in final_text(transcript).lower() else 0.0\n'}]},
            ],
        }
        return manifest

    def dump_manifest(self, manifest):
        import yaml
        return yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)

    def test_compose_and_grade_runs(self):
        self.write_task("task_alpha")
        self.write_task("task_beta", grading_type="hybrid")
        manifest = self.dump_manifest(self.basic_manifest())
        name, rendered = cs.compose_one(self.write_manifest(manifest), self.root)
        self.assertEqual(name, "mini-day")
        self.assertIn("id: task_mini-day", rendered)

        import re
        code = re.search(r"## Automated Checks\s*```python\s*(.*?)\s*```", rendered, re.S).group(1)
        ns = {}
        exec("import json,sys\n" + code, ns)
        scores = ns["grade"]([{"type": "message", "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "alpha"}]}}],
                             tempfile.mkdtemp())
        self.assertIn("a_0/task_alpha/created", scores)
        self.assertIn("b_0/task_beta/created", scores)
        self.assertIn("synth/wrap/recall", scores)
        self.assertEqual(scores["synth/wrap/recall"], 1.0)
        for value in scores.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_deterministic(self):
        self.write_task("task_alpha")
        self.write_task("task_beta")
        manifest = self.dump_manifest(self.basic_manifest())
        path = self.write_manifest(manifest)
        _, first = cs.compose_one(path, self.root)
        _, second = cs.compose_one(path, self.root)
        self.assertEqual(first, second)

    def test_fixture_dedupe_same_source(self):
        fixtures = "- source: csvs/data.csv\n  dest: data.csv"
        self.write_task("task_alpha", fixtures=fixtures)
        self.write_task("task_beta", fixtures=fixtures)
        manifest = self.dump_manifest(self.basic_manifest())
        _, rendered = cs.compose_one(self.write_manifest(manifest), self.root)
        self.assertEqual(rendered.count("dest: data.csv"), 1)

    def test_fixture_conflict_rejected(self):
        self.write_task("task_alpha", fixtures="- source: one/data.csv\n  dest: data.csv")
        self.write_task("task_beta", fixtures="- source: two/data.csv\n  dest: data.csv")
        manifest = self.dump_manifest(self.basic_manifest())
        with self.assertRaises(cs.ComposeError):
            cs.compose_one(self.write_manifest(manifest), self.root)

    def test_output_collision_rejected(self):
        self.write_task("task_alpha", prompt="save to `report.md`")
        self.write_task("task_beta", prompt="save to `report.md`")
        manifest = self.dump_manifest(self.basic_manifest())
        with self.assertRaises(cs.ComposeError):
            cs.compose_one(self.write_manifest(manifest), self.root)

    def test_glob_conflict_rejected(self):
        glob_grade = (
            "def grade(transcript, workspace_path):\n"
            "    from pathlib import Path\n"
            "    workspace = Path(workspace_path)\n"
            "    files = list(workspace.glob(\"*.py\"))\n"
            "    return {\"any\": 1.0 if files else 0.0}\n"
        )
        self.write_task("task_alpha", prompt="list the python files", grade=glob_grade)
        self.write_task("task_beta", prompt="save to `helper.py`")
        manifest = self.dump_manifest(self.basic_manifest())
        with self.assertRaises(cs.ComposeError):
            cs.compose_one(self.write_manifest(manifest), self.root)

    def test_order_coverage_required(self):
        self.write_task("task_alpha")
        self.write_task("task_beta")
        bad = self.basic_manifest()
        bad["order"] = ["a.0", "synth.0"]  # b.0 missing
        with self.assertRaises(cs.ComposeError):
            cs.compose_one(self.write_manifest(self.dump_manifest(bad)), self.root)

    def test_network_task_rejected(self):
        self.write_task("task_stock")
        manifest = self.dump_manifest(self.basic_manifest())
        manifest = manifest.replace("task_alpha", "task_stock")
        with self.assertRaises(cs.ComposeError):
            cs.compose_one(self.write_manifest(manifest), self.root)

    def test_llm_judge_only_rejected(self):
        (self.root / "tasks" / "task_alpha.md").write_text(
            "---\nid: task_alpha\ngrading_type: llm_judge\n---\n\n## Prompt\n\ndo it\n"
        )
        manifest = self.dump_manifest(self.basic_manifest())
        with self.assertRaises(cs.ComposeError):
            cs.compose_one(self.write_manifest(manifest), self.root)

    def test_read_ws_lenient(self):
        self.write_task("task_alpha")
        self.write_task("task_beta")
        manifest = {
            "name": "mini-day", "category": "coding",
            "threads": {"a": ["task_alpha"]},
            "order": ["a.0", "synth.0"],
            "synthesis": [{"id": "wrap", "prompt": "sum",
                           "checks": [{"key": "missing_file",
                                       "python": 'return 1.0 if read_ws(workspace_path, "nope.txt") else 0.0\n'}]}],
        }
        _, rendered = cs.compose_one(self.write_manifest(self.dump_manifest(manifest)), self.root)
        import re
        code = re.search(r"## Automated Checks\s*```python\s*(.*?)\s*```", rendered, re.S).group(1)
        ns = {}
        exec("import json,sys\n" + code, ns)
        scores = ns["grade"]([], tempfile.mkdtemp())
        self.assertEqual(scores["synth/wrap/missing_file"], 0.0)

    def test_check_mode_detects_drift(self):
        self.write_task("task_alpha")
        self.write_task("task_beta")
        self.write_manifest(self.dump_manifest(self.basic_manifest()))
        rc = cs.main(["--root", str(self.root), "--sessions-dir", str(self.sessions),
                      "--out-dir", str(self.out)])
        self.assertEqual(rc, 0)
        rc = cs.main(["--root", str(self.root), "--sessions-dir", str(self.sessions),
                      "--out-dir", str(self.out), "--check"])
        self.assertEqual(rc, 0)
        (self.out / "tasks" / "task_mini-day.md").write_text("tampered")
        with self.assertRaises(SystemExit):
            cs.main(["--root", str(self.root), "--sessions-dir", str(self.sessions),
                     "--out-dir", str(self.out), "--check"])


if __name__ == "__main__":
    unittest.main()
