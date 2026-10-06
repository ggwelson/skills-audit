"""Build a fake home directory with skills spread across several agents.

`skills-audit --demo` scans it, and the tests and README screenshots use it.
Every name in here is made up.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path

WRITING_KIT = {
    "outline": "Turn rough notes into a structured outline before drafting. Use for docs, posts and design notes.",
    "tighten": "Cut a draft by a third without losing meaning. Use when text runs long.",
    "headline": "Write five candidate titles for a post or PR and pick the clearest.",
    "changelog-voice": "Rewrite changelog entries in the project's house style.",
    "style-guide": "Check prose against the team style guide and list violations with fixes.",
    "readability": "Score a draft for reading level and flag the hardest sentences.",
    "cite-sources": "Add inline citations for every factual claim in a draft.",
    "summarize-thread": "Summarize a long Slack or email thread into decisions and open questions.",
}

LONG_BODY = """# {title}

## When to use

{desc}

## Steps

1. Read the input in full before changing anything.
2. List what the reader needs to know or do, in order.
3. Write the smallest version that covers that list.
4. Check every claim against the source. Drop anything you can't back up.

## Output

Return the result in a fenced block, then one line on what changed and why.
"""


def _skill(folder: Path, name: str, desc: str, **meta: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    extra = "".join(f"{k}: {v}\n" for k, v in meta.items())
    title = name.replace("-", " ").capitalize()
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n{extra}---\n\n" + LONG_BODY.format(title=title, desc=desc))


def _md(path: Path, name: str, desc: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\nInstructions for {name}.\n")


def _link(link: Path, target: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def _jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
           "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def build_demo_home(root: Path) -> Path:
    home = Path(root) / "home"
    now = dt.datetime.now()

    def day(n: int) -> str:
        return (now - dt.timedelta(days=n)).strftime("%Y-%m-%dT10:00:00.000Z")

    # Claude Code: personal skills live in a dotfiles git repo, symlinked in.
    dot = home / "dotfiles"
    skills = dot / "claude/skills"
    _skill(skills / "standup", "standup", "Draft a short daily standup from yesterday's commits, PRs and calendar.")
    _skill(skills / "release-notes", "release-notes", "Turn PRs merged since the last tag into user-facing release notes.")
    _skill(skills / "sql-helper", "sql-helper", "Write and explain read-only SQL against the analytics warehouse. Use for data questions.")
    _skill(skills / "old-reviewer", "old-reviewer", "Review a diff with the 2024 checklist.")
    if shutil.which("git"):
        _git(dot, "init", "-q")
        _git(dot, "add", "-A")
        _git(dot, "commit", "-q", "-m", "skills")
    shutil.rmtree(skills / "old-reviewer")  # deleted but not committed
    _skill(skills / "pr-triage", "pr-triage", "Sort open PRs by risk and staleness and suggest a reviewer for each.")
    _link(home / ".claude/skills", "../dotfiles/claude/skills")
    bucket = skills / "synced/0b5e-demo"
    _skill(bucket / "docx", "docx", "Create and edit Word documents, including tracked changes and comments.")
    _skill(bucket / "pdf", "pdf", "Read, merge, split, fill and OCR PDF files.")

    # Claude Code plugins.
    cache = home / ".claude/plugins/cache"
    wk = cache / "acme-tools/writing-kit/1.4.0"
    for name, desc in WRITING_KIT.items():
        _skill(wk / "skills" / name, name, desc, **({"disable-model-invocation": "true"} if name == "style-guide" else {}))
    _md(wk / "agents/editor.md", "editor", "Line editor that applies the writing-kit skills to a whole document.")
    rt = cache / "acme-tools/review-tools/0.3.1"
    _md(rt / "commands/review-pr.md", "review-pr", "Review the current branch's diff for bugs.")
    _md(rt / "commands/review-security.md", "review-security", "Check the diff for injection, auth and secrets mistakes.")
    _skill(rt / "skills/test-gaps", "test-gaps", "Find changed code paths that no test exercises.")
    fd = cache / "claude-plugins-official/frontend-design/9f2c1a"
    _skill(fd / "skills/frontend-design", "frontend-design", "Guidance for distinctive, intentional visual design when building UI.")
    installed = {
        "writing-kit@acme-tools": wk, "review-tools@acme-tools": rt, "frontend-design@claude-plugins-official": fd,
    }
    (home / ".claude/plugins").mkdir(parents=True, exist_ok=True)
    (home / ".claude/plugins/installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        pid: [{"scope": "user", "installPath": str(path), "version": path.name, "installedAt": day(40 + i * 25)}]
        for i, (pid, path) in enumerate(installed.items())}}, indent=2))
    (home / ".claude/settings.json").write_text(json.dumps({
        "enabledPlugins": {pid: True for pid in installed},
        "extraKnownMarketplaces": {"acme-tools": {"source": {"source": "directory", "path": "~/code/acme-tools"}}},
    }, indent=2))
    _link(home / ".claude/plugins/config.json", "../../dotfiles/claude/plugins/config.json")

    # Shared ~/.agents/skills, managed by `npx skills`.
    agents = home / ".agents/skills"
    _skill(agents / "react-best-practices", "react-best-practices", "React and Next.js performance rules: rendering, data fetching, bundle size.")
    shutil.copytree(skills / "sql-helper", agents / "sql-helper")
    _link(agents / "release-notes", "../../dotfiles/claude/skills/release-notes")
    (home / ".agents/.skill-lock.json").write_text(json.dumps({"version": 3, "skills": {
        "react-best-practices": {"source": "example/agent-skills", "installedAt": day(120)},
        "prompt-golf": {"source": "example/fun-skills", "installedAt": day(200)},
        "tdd-coach": {"source": "example/fun-skills", "installedAt": day(200)},
    }}, indent=2))

    # Codex.
    codex = home / ".codex"
    _skill(codex / "skills/gh-fix-ci", "gh-fix-ci", "Find why a GitHub Actions run failed and propose a fix.")
    _link(codex / "skills/react-best-practices", "../../.agents/skills/react-best-practices")
    _skill(codex / "skills/.system/skill-creator", "skill-creator", "Create or update a skill.")
    _skill(codex / "skills/.system/skill-installer", "skill-installer", "Install skills from a curated list or a repo.")
    _skill(codex / "memories/skills/deploy-checklist", "deploy-checklist", "Steps that worked for the last three staging deploys.")
    (codex / "memories/MEMORY.md").write_text("# Memory\n\n- Staging deploys need the migration flag.\n- Related skill: skills/deploy-checklist/SKILL.md\n")
    cwk = codex / "plugins/cache/acme-tools/writing-kit/1.4.0"
    shutil.copytree(wk / "skills", cwk / "skills")
    _skill(codex / "plugins/cache/openai-bundled/docs/1.0.0/skills/docs-writer", "docs-writer", "Write and format documents.")
    (codex / "config.toml").write_text(
        'model = "example"\n\n[plugins."writing-kit@acme-tools"]\nenabled = true\n\n'
        '[plugins."docs@openai-bundled"]\nenabled = true\n\n'
        '[marketplaces.acme-tools]\nsource_type = "local"\nsource = "~/code/acme-tools"\n')

    # Cursor.
    cur = home / ".cursor"
    _skill(cur / "skills-cursor/create-rule", "create-rule", "Create a Cursor rule file.")
    _skill(cur / "skills-cursor/review", "review", "Review the current changes.")
    shutil.copytree(wk / "skills", cur / "plugins/cache/cursor-public/writing-kit/e5a818/skills")
    lin = cur / "plugins/cache/cursor-public/linear/dac1a0/skills"
    _skill(lin / "linear-triage", "linear-triage", "Triage new Linear issues: label, prioritize, assign.")
    _skill(lin / "linear-standup", "linear-standup", "Summarize your Linear activity for standup.")

    # OMP and Cline.
    _skill(home / ".omp/agent/managed-skills/rebase-helper", "rebase-helper", "Rebase a stack of PRs after the base branch merges.")
    _skill(home / ".omp/agent/managed-skills/flaky-test-hunter", "flaky-test-hunter", "Rerun a failing test 20 times and bisect the flake.")
    _link(home / ".cline/skills/tdd-coach", "../../.agents/skills/tdd-coach")

    # Usage: Claude Code transcripts and Codex session logs.
    rows = []
    for name, count in (("standup", 12), ("frontend-design:frontend-design", 5), ("writing-kit:outline", 3),
                        ("writing-kit:tighten", 2)):
        rows += [{"type": "assistant", "timestamp": day(2 + 3 * i), "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": name}}]}} for i in range(count)]
    rows += [{"type": "user", "timestamp": day(4 + i), "message": {"content": "<command-name>/review-tools:review-pr</command-name>"}}
             for i in range(4)]
    _jsonl(home / ".claude/projects/-home-demo-app/session.jsonl", [{"type": "user", "timestamp": day(45)}] + rows)
    codex_rows = []
    for name, folder, count in (("gh-fix-ci", "~/.codex/skills", 7), ("react-best-practices", "~/.agents/skills", 2)):
        codex_rows += [{"timestamp": day(5 + i), "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command",
            "arguments": json.dumps({"cmd": f"sed -n 1,200p {folder}/{name}/SKILL.md"})}} for i in range(count)]
    start = (now - dt.timedelta(days=90)).strftime("%Y-%m-%d")
    _jsonl(codex / f"sessions/{start[:4]}/{start[5:7]}/{start[8:]}/rollout-{start}T10-00-00-demo.jsonl", codex_rows)
    return home
