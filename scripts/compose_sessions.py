#!/usr/bin/env python3
"""Compose long-session multi-turn PinchBench tasks from existing single-turn
tasks.

A *session manifest* (sessions/<name>.yaml) declares a set of problem
*threads*, each an ordered list of existing collectible PinchBench task IDs,
plus an explicit interleaving `order`, plus a few hand-written `synthesis`
turns. This tool deterministically emits one composed task markdown
(tasks/task_<name>.md) that:

  * merges every referenced task's workspace fixture,
  * lists every referenced task's prompt verbatim, in the manifest order,
  * defines one aggregated grade(transcript, workspace_path) that runs each
    referenced task's own grade() (namespaced) and the synthesis checks.

Only deterministic grading is consumed: `grading_type: automated` in full and
`hybrid` by its automated grade() half. `llm_judge`-only tasks (no
`## Automated Checks` python block) are rejected by construction.

Design authority:
docs/customization/benchmarks/pinchbench/multi-turn-personas-design.md

Dependency: PyYAML (the upstream PinchBench task frontmatter uses block
scalars and inline content, so a hand-rolled parser is not safe).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dependency guard
    sys.stderr.write(
        "compose_sessions.py requires PyYAML (pip install pyyaml)\n"
    )
    raise


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = str(REPO_ROOT)

# Tasks that require external network access or fws/gh prerequisites. They are
# excluded so the composed session is deterministic and replayable. Source:
# cases-summary.md "外网" column and the fws fragment.
NETWORK_OR_PREREQ = {
    "task_stock",
    "task_weather",
    "task_market_research",
    "task_polymarket_briefing",
    "task_earnings_analysis",
    "task_financial_ratio_calculation",
    "task_executive_lookup",
    "task_codebase_navigation",
    "task_cicd_pipeline_debug",
    "task_test_generation",
    "task_gh_issue_triage",
    "task_gws_cross_service",
    "task_gws_email_triage",
    "task_gws_task_management",
}

# File extensions a task prompt may name as an output. Kept broad; fixture
# names are subtracted before output accounting.
OUTPUT_EXTENSIONS = (
    "md", "json", "csv", "txt", "ics", "py", "yml", "yaml", "html", "log",
    "js", "ts", "sh", "toml", "xml", "ini", "cfg",
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
SECTION_RE = re.compile(r"(?m)^##\s+(.+)$")
PYTHON_BLOCK_RE = re.compile(r"```python\s*(.*?)\s*```", re.DOTALL)
BACKTICK_FILE_RE = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:" + "|".join(OUTPUT_EXTENSIONS) + r"))`"
)
FIXED_PATH_RE = re.compile(r'workspace\s*/\s*"([^"]+)"(?:\s*/\s*"([^"]+)")?')
GLOB_RE = re.compile(r'\.(?:glob|rglob)\(\s*"([^"]+)"\s*\)')
ITERDIR_RE = re.compile(r"\.iterdir\(\s*\)")


class ComposeError(Exception):
    """A manifest cannot be composed (fail-closed)."""


def utf8_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@dataclass
class TaskFile:
    id: str
    grading_type: str
    workspace_files: list
    prompt: str
    session_prompts: list
    grade_code: str
    path: Path

    @property
    def fixture_names(self) -> set:
        names = set()
        for entry in self.workspace_files:
            if entry.get("path"):
                names.add(entry["path"])
            if entry.get("dest"):
                names.add(entry["dest"])
        return names


def parse_task_file(path: Path) -> TaskFile:
    text = utf8_text(path)
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise ComposeError(f"{path.name}: no --- frontmatter block")
    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2)
    task_id = str(meta.get("id", "")).strip()
    if not task_id:
        raise ComposeError(f"{path.name}: frontmatter id is required")

    sections = {}
    headings = list(SECTION_RE.finditer(body))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[heading.group(1).strip()] = body[heading.end():end]

    prompt = (sections.get("Prompt") or "").strip()
    if not prompt:
        raise ComposeError(f"{task_id}: no ## Prompt section")
    checks = sections.get("Automated Checks") or ""
    code_match = PYTHON_BLOCK_RE.search(checks)
    if not code_match:
        raise ComposeError(
            f"{task_id}: no ```python grading block (llm_judge-only is excluded)"
        )

    session_prompts = []
    for entry in meta.get("sessions") or []:
        value = str((entry or {}).get("prompt", "")).strip()
        if value:
            session_prompts.append(value)

    return TaskFile(
        id=task_id,
        grading_type=str(meta.get("grading_type", "automated") or "automated"),
        workspace_files=list(meta.get("workspace_files") or []),
        prompt=prompt,
        session_prompts=session_prompts,
        grade_code=code_match.group(1).strip("\n"),
        path=path,
    )


def fixture_identity(entry: dict) -> tuple:
    """Return (name, identity). identity is the source path or inline content."""
    if entry.get("path"):
        return entry["path"], ("inline", entry.get("content", ""))
    return entry.get("dest", ""), ("source", entry.get("source", ""))


def extract_outputs(task: TaskFile) -> set:
    """Best-effort set of workspace-relative files a task writes.

    Heuristic over the prompt (backticked filenames) and the grade code (fixed
    `workspace / "X"` paths), minus the task's own fixtures.
    """
    fixtures = task.fixture_names
    names = set()
    for candidate in BACKTICK_FILE_RE.findall(task.prompt):
        names.add(candidate)
    for first, second in FIXED_PATH_RE.findall(task.grade_code):
        names.add(first if second == "" else f"{first}/{second}")
    return {n for n in names if n not in fixtures}


def extract_globs(task: TaskFile) -> set:
    """Glob patterns a grader enumerates (top-level `*.ext` form)."""
    globs = set()
    for pattern in GLOB_RE.findall(task.grade_code):
        if "/" not in pattern:
            globs.add(pattern)
    return globs


def glob_matches(glob_pattern: str, output_name: str) -> bool:
    """Whether a top-level `*.ext` glob could match an output file."""
    if "/" in output_name:
        return False
    if glob_pattern == "*":
        return True
    if glob_pattern.startswith("*."):
        return output_name.endswith(glob_pattern[1:])
    return output_name == glob_pattern


@dataclass
class ResolvedStep:
    slot: str
    task: TaskFile


@dataclass
class Composed:
    name: str
    category: str
    timeout_seconds: int
    steps: list = field(default_factory=list)
    synthesis: list = field(default_factory=list)
    fixtures: list = field(default_factory=list)


def load_manifest(path: Path) -> dict:
    manifest = yaml.safe_load(utf8_text(path))
    if not isinstance(manifest, dict):
        raise ComposeError(f"{path.name}: manifest must be a mapping")
    for key in ("name", "threads", "order"):
        if key not in manifest:
            raise ComposeError(f"{path.name}: manifest is missing {key!r}")
    if not isinstance(manifest["threads"], dict) or not manifest["threads"]:
        raise ComposeError(f"{path.name}: threads must be a non-empty mapping")
    if not isinstance(manifest["order"], list) or not manifest["order"]:
        raise ComposeError(f"{path.name}: order must be a non-empty list")
    return manifest


def resolve_steps(manifest: dict, root: Path) -> tuple:
    """Resolve order entries to (references, synthesis) with validation."""
    threads = manifest["threads"]
    synthesis = manifest.get("synthesis") or []
    synth_ids = [str(s.get("id", "")).strip() for s in synthesis]
    if any(not s for s in synth_ids):
        raise ComposeError("every synthesis entry needs an id")

    refs = []          # (slot, task_id, thread)
    synth_order = []   # (slot, synth_index)
    seen = set()
    for entry in manifest["order"]:
        entry = str(entry).strip()
        if entry.startswith("synth."):
            index = int(entry.split(".", 1)[1])
            if index < 0 or index >= len(synthesis):
                raise ComposeError(f"order references unknown synthesis {entry!r}")
            seen.add(("synth", index))
            synth_order.append((f"synth_{index}", index))
            continue
        if "." not in entry:
            raise ComposeError(f"order entry {entry!r} must be <thread>.<index> or synth.<index>")
        thread, index_text = entry.rsplit(".", 1)
        if thread not in threads:
            raise ComposeError(f"order references unknown thread {thread!r}")
        index = int(index_text)
        steps = threads[thread]
        if index < 0 or index >= len(steps):
            raise ComposeError(f"order references out-of-range step {entry!r}")
        seen.add((thread, index))
        refs.append((f"{thread}_{index}", str(steps[index]), thread))

    expected = {(t, i) for t, steps in threads.items() for i in range(len(steps))}
    expected |= {("synth", i) for i in range(len(synthesis))}
    missing = expected - seen
    if missing:
        raise ComposeError(f"order does not cover these steps: {sorted(missing)}")
    if len(seen) != len(manifest["order"]):
        raise ComposeError("order contains duplicate entries")
    return refs, synth_order


def load_referenced_tasks(refs: list, root: Path) -> list:
    tasks = []
    for slot, task_id, thread in refs:
        if task_id in NETWORK_OR_PREREQ:
            raise ComposeError(
                f"{task_id} requires network/prerequisites; excluded from deterministic sessions"
            )
        path = root / "tasks" / f"{task_id}.md"
        if not path.exists():
            raise ComposeError(f"{task_id}: task file not found at {path}")
        task = parse_task_file(path)
        if task.id != task_id:
            raise ComposeError(f"{task_id}: frontmatter id {task.id!r} does not match")
        if task.grading_type not in ("automated", "hybrid"):
            raise ComposeError(f"{task_id}: grading_type {task.grading_type!r} is not collectible")
        tasks.append((slot, task, thread))
    return tasks


def merge_fixtures(tasks: list) -> list:
    """Union fixtures, deduping identical names+content; reject real conflicts."""
    by_name = {}
    order = []
    for _slot, task, _thread in tasks:
        for entry in task.workspace_files:
            name, identity = fixture_identity(entry)
            if not name:
                raise ComposeError(f"{task.id}: workspace_files entry has no path/dest")
            if name in by_name:
                if by_name[name][1] != identity:
                    other = by_name[name][2]
                    raise ComposeError(
                        f"fixture {name!r} declared by both {other} and {task.id} "
                        "with different content"
                    )
                continue
            by_name[name] = (entry, identity, task.id)
            order.append(entry)
    return order


def validate_conflicts(tasks: list) -> None:
    outputs = {}
    for _slot, task, _thread in tasks:
        for name in extract_outputs(task):
            if name in outputs:
                raise ComposeError(
                    f"output {name!r} produced by both {outputs[name]} and {task.id}"
                )
            outputs[name] = task.id

    # A globbing grader (e.g. workspace.glob("*.py")) must not share the
    # workspace with any other task producing a top-level file it could match.
    for _slot, task, _thread in tasks:
        if ITERDIR_RE.search(task.grade_code):
            others = [n for n, owner in outputs.items() if owner != task.id]
            if others:
                raise ComposeError(
                    f"{task.id} enumerates the whole workspace (iterdir) and cannot "
                    f"share a session with output-producing tasks: {sorted(others)}"
                )
        for pattern in extract_globs(task):
            for name, owner in outputs.items():
                if owner != task.id and glob_matches(pattern, name):
                    raise ComposeError(
                        f"{task.id} globs {pattern!r} which would match {name!r} "
                        f"produced by {owner}"
                    )


def indent_block(code: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join((pad + line) if line.strip() else "" for line in code.splitlines())


def wrap_reference(slot: str, task: TaskFile) -> str:
    body = indent_block(task.grade_code, 4)
    return (
        f"def _ref_{slot}(transcript, workspace_path):\n"
        f"{body}\n"
        f"    return grade(transcript, workspace_path)\n"
    )


def wrap_synthesis(entry: dict) -> str:
    checks = entry.get("checks") or []
    lines = [f"def _synth_{entry['id']}(transcript, workspace_path):", "    scores = {}"]
    for check in checks:
        key = str(check.get("key", "")).strip()
        code = str(check.get("python", "")).strip("\n")
        if not key or not code:
            raise ComposeError(f"synthesis {entry['id']}: each check needs key and python")
        lines.append(f"    def _check_{key}(transcript, workspace_path):")
        lines.append(indent_block(code, 8))
        lines.append(f'    scores["{key}"] = _check_{key}(transcript, workspace_path)')
    lines.append("    return scores")
    return "\n".join(lines) + "\n"


HELPERS = '''\
def _assistant_chunks(transcript):
    for event in transcript:
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            yield "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )


def final_text(transcript):
    texts = list(_assistant_chunks(transcript))
    return texts[-1] if texts else ""


def all_assistant_text(transcript):
    return "\\n".join(_assistant_chunks(transcript))


def read_ws(workspace_path, relative):
    import os
    path = os.path.join(workspace_path, relative)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def exists_ws(workspace_path, relative):
    import os
    return os.path.exists(os.path.join(workspace_path, relative))
'''


def build_grade(tasks: list, synth_order: list, synthesis: list) -> str:
    parts = [HELPERS, ""]
    for slot, task, _thread in tasks:
        parts.append(wrap_reference(slot, task))
    for _, index in synth_order:
        parts.append(wrap_synthesis(synthesis[index]))

    lines = ["def grade(transcript, workspace_path):", "    scores = {}"]
    for slot, task, _thread in tasks:
        lines.append(
            f'    for key, value in _ref_{slot}(transcript, workspace_path).items():'
        )
        lines.append(f'        scores["{slot}/{task.id}/" + key] = value')
    for _, index in synth_order:
        name = synthesis[index]["id"]
        lines.append(
            f'    for key, value in _synth_{name}(transcript, workspace_path).items():'
        )
        lines.append(f'        scores["synth/{name}/" + key] = value')
    lines.append("    return scores")
    parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


def render_task(manifest: dict, tasks: list, synth_order: list) -> str:
    synthesis = manifest.get("synthesis") or []
    prompts = []
    synth_cursor = 0
    ref_by_slot = {slot: (task, thread) for slot, task, thread in tasks}
    for entry in manifest["order"]:
        entry = str(entry).strip()
        if entry.startswith("synth."):
            index = int(entry.split(".", 1)[1])
            prompts.append(
                {"id": f"turn_{len(prompts):02d}", "prompt": synthesis[index]["prompt"]}
            )
            synth_cursor += 1
        else:
            slot = entry.replace(".", "_")
            task, _thread = ref_by_slot[slot]
            expanded = task.session_prompts if task.session_prompts else [task.prompt]
            for prompt in expanded:
                prompts.append({"id": f"turn_{len(prompts):02d}", "prompt": prompt})

    name = str(manifest["name"]).strip()
    frontmatter = {
        "id": f"task_{name}",
        "name": name,
        "category": str(manifest.get("category", "coding")).strip(),
        "grading_type": "automated",
        "timeout_seconds": int(manifest.get("timeout_seconds", 3600)),
        "multi_session": True,
        "workspace_files": merge_fixtures(tasks),
        "sessions": prompts,
    }
    meta = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10**9,
    )
    body = (
        "## Prompt\n\n"
        "This is a composed multi-session task. See the `sessions` field in the "
        "frontmatter for the ordered turns.\n\n"
        "## Automated Checks\n\n"
        "```python\n" + build_grade(tasks, synth_order, synthesis) + "```\n"
    )
    return f"---\n{meta}---\n\n{body}"


def validate_rendered(rendered: str, name: str) -> None:
    """Re-parse the emitted task and assert the adapter's load constraints."""
    match = FRONTMATTER_RE.match(rendered)
    if not match:
        raise ComposeError(f"task_{name}: rendered task has no frontmatter")
    meta = yaml.safe_load(match.group(1)) or {}
    if meta.get("id") != f"task_{name}":
        raise ComposeError(f"task_{name}: rendered id {meta.get('id')!r} does not match file")
    sessions = meta.get("sessions") or []
    if not sessions or any(not str(s.get("prompt", "")).strip() for s in sessions):
        raise ComposeError(f"task_{name}: rendered sessions are missing or empty")
    for entry in meta.get("workspace_files") or []:
        inline = bool(str(entry.get("path", "")).strip())
        copied = bool(str(entry.get("source", "")).strip() or str(entry.get("dest", "")).strip())
        if inline == copied:
            raise ComposeError(
                f"task_{name}: workspace_files entry must be exactly one of "
                "{path,content} or {source,dest}: {entry!r}"
            )
    if "## Automated Checks" not in rendered or "def grade(" not in rendered:
        raise ComposeError(f"task_{name}: rendered task has no aggregated grade")
    compile(rendered.split("## Automated Checks", 1)[1].split("```python", 1)[1].split("```", 1)[0],
            f"task_{name}-grade", "exec")


def compose_one(manifest_path: Path, root: Path) -> tuple:
    manifest = load_manifest(manifest_path)
    refs, synth_order = resolve_steps(manifest, root)
    tasks = load_referenced_tasks(refs, root)
    validate_conflicts(tasks)
    merge_fixtures(tasks)  # fail early on real fixture conflicts
    name = str(manifest["name"]).strip()
    rendered = render_task(manifest, tasks, synth_order)
    validate_rendered(rendered, name)
    return name, rendered


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("ARIES_PINCHBENCH_ROOT", DEFAULT_ROOT),
                        help="pinned PinchBench checkout containing tasks/")
    parser.add_argument("--sessions-dir", default=str(REPO_ROOT / "sessions"),
                        help="directory of session manifests")
    parser.add_argument("--out-dir", default=str(REPO_ROOT),
                        help="directory to write tasks/task_<name>.md into")
    parser.add_argument("--only", action="append", default=None,
                        help="compose only the named manifest(s), repeatable")
    parser.add_argument("--check", action="store_true",
                        help="verify committed tasks match a fresh composition")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not (root / "tasks").is_dir():
        parser.error(f"pinchbench root {root} has no tasks/ directory")
    sessions_dir = Path(args.sessions_dir)
    manifests = sorted(sessions_dir.glob("*.yaml"))
    if args.only:
        manifests = [m for m in manifests if m.stem in set(args.only)]
    if not manifests:
        parser.error(f"no manifests found in {sessions_dir}")

    out_tasks = Path(args.out_dir) / "tasks"
    out_tasks.mkdir(parents=True, exist_ok=True)

    failed = False
    for manifest_path in manifests:
        try:
            name, rendered = compose_one(manifest_path, root)
        except ComposeError as error:
            print(f"FAIL {manifest_path.name}: {error}", file=sys.stderr)
            failed = True
            continue
        target = out_tasks / f"task_{name}.md"
        if args.check:
            if not target.exists() or utf8_text(target) != rendered:
                print(f"DRIFT {target}", file=sys.stderr)
                failed = True
            else:
                print(f"ok   {target}")
        else:
            target.write_text(rendered, encoding="utf-8")
            print(f"wrote {target}")
    if failed:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
