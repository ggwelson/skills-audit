import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills_audit import cli
from skills_audit.cli import Scanner, parse_codex_config, parse_frontmatter, write_outputs
from skills_audit.demo import build_demo_home

HAS_GIT = shutil.which("git") is not None


def runs(unit):
    return [s["run"] for s in unit["steps"] if "run" in s]


class DemoScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home = build_demo_home(Path(cls.tmp.name))
        cls.data = Scanner(home, offline=True).scan()
        cls.units = {u["id"]: u for u in cls.data["units"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_finds_every_agent(self):
        self.assertEqual(set(self.data["harnesses"]), {"claude", "shared", "codex", "cursor", "omp", "cline", "cleanup"})

    @unittest.skipUnless(HAS_GIT, "needs git")
    def test_tracked_skill_is_removed_with_git(self):
        unit = self.units["claude-skill:~/.claude/skills/standup"]
        self.assertEqual(runs(unit), ["git -C ~/dotfiles rm -r -q claude/skills/standup"])
        self.assertEqual(unit["backup"], [])
        self.assertEqual(unit["repo"], {"root": "~/dotfiles", "state": "tracked"})

    @unittest.skipUnless(HAS_GIT, "needs git")
    def test_untracked_skill_is_backed_up_then_deleted(self):
        unit = self.units["claude-skill:~/.claude/skills/pr-triage"]
        self.assertEqual(runs(unit), ["command rm -rf ~/dotfiles/claude/skills/pr-triage"])
        self.assertEqual(unit["backup"], ["dotfiles/claude/skills/pr-triage"])

    def test_symlink_removal_only_touches_the_link(self):
        link = self.units["shared-skill:~/.agents/skills/release-notes"]
        self.assertEqual(link["type"], "symlink")
        self.assertEqual(runs(link), ["command rm ~/.agents/skills/release-notes"])

    def test_removing_a_target_also_removes_links_to_it(self):
        target = self.units["shared-skill:~/.agents/skills/react-best-practices"]
        self.assertIn("command rm ~/.codex/skills/react-best-practices", runs(target))
        self.assertTrue(any("skill-lock.json" in s.get("do", "") for s in target["steps"]))

    def test_shared_marketplace_is_kept(self):
        unit = self.units["claude-plugin:writing-kit@acme-tools"]
        self.assertEqual(runs(unit), ["claude plugin uninstall writing-kit@acme-tools"])
        self.assertEqual(len(unit["items"]), 9)

    def test_codex_plugins(self):
        self.assertEqual(runs(self.units["codex-plugin:writing-kit@acme-tools"]),
                         ["codex plugin remove writing-kit@acme-tools", "codex plugin marketplace remove acme-tools"])
        bundled = self.units["codex-plugin:docs@openai-bundled"]
        self.assertEqual(runs(bundled), [])
        self.assertIn("enabled = false", bundled["steps"][0]["do"])

    def test_cursor_plugins_are_manual(self):
        unit = self.units["cursor-plugin:cursor-public/linear"]
        self.assertIn("manual", unit["steps"][0])
        self.assertIsNone(unit["usage"])

    def test_locked_entries(self):
        for uid in ("claude-synced:docx", "codex-system", "cursor-builtin"):
            self.assertFalse(self.units[uid]["removable"], uid)

    def test_usage_counts(self):
        self.assertEqual(self.units["claude-skill:~/.claude/skills/standup"]["usage"], 12)
        self.assertEqual(self.units["claude-plugin:writing-kit@acme-tools"]["usage"], 5)
        self.assertEqual(self.units["claude-plugin:review-tools@acme-tools"]["usage"], 4)
        self.assertEqual(self.units["codex-skill:~/.codex/skills/gh-fix-ci"]["usage"], 7)
        self.assertEqual(self.units["omp-skill:~/.omp/agent/managed-skills/rebase-helper"]["usage"], None)

    def test_memory_skill_cleans_memory_index(self):
        unit = self.units["codex-skill:~/.codex/memories/skills/deploy-checklist"]
        self.assertTrue(any("MEMORY.md" in s.get("do", "") for s in unit["steps"]))
        self.assertIn("returns", unit)

    def test_fixes(self):
        ids = {u["id"] for u in self.data["units"] if u["harness"] == "cleanup"}
        self.assertIn("cleanup:agents-lock-stale", ids)
        self.assertIn("cleanup:broken-link:~/.claude/plugins/config.json", ids)
        if HAS_GIT:
            self.assertIn("cleanup:gitignore-synced", ids)
            self.assertIn("cleanup:uncommitted-deletions", ids)
        self.assertEqual(self.units["cline-skill:~/.cline/skills/tdd-coach"]["type"], "broken-link")

    def test_duplicates_are_flagged(self):
        copy = self.units["shared-skill:~/.agents/skills/sql-helper"]
        self.assertIn("claude-skill:~/.claude/skills/sql-helper", copy["items"][0]["alsoIn"])

    def test_html_embeds_the_data(self):
        with tempfile.TemporaryDirectory() as out:
            page = write_outputs(self.data, Path(out))
            html = page.read_text()
        self.assertNotIn("/*__DATA__*/null", html)
        start = html.index("const DATA = ") + len("const DATA = ")
        payload = html[start:html.index(";\nconst COLORS")]
        self.assertEqual(len(json.loads(payload)["units"]), len(self.data["units"]))


class ParserTest(unittest.TestCase):
    def test_frontmatter_block_scalar(self):
        meta, body = parse_frontmatter("---\nname: x\ndescription: >-\n  one\n  two\nuser-invocable: false\n---\n\nBody\n")
        self.assertEqual(meta["description"], "one two")
        self.assertEqual(meta["user-invocable"], "false")
        self.assertEqual(body, "Body\n")

    def test_codex_config_without_tomllib(self):
        text = '[plugins."a@m"]\nenabled = true\n\n[plugins."b@m"]\nenabled = false\n\n[marketplaces.m]\nsource = "x"\n'
        with mock.patch.object(cli, "tomllib", None):
            cfg = parse_codex_config(text)
        self.assertEqual(cfg["plugins"], {"a@m": {"enabled": True}, "b@m": {"enabled": False}})
        self.assertEqual(set(cfg["marketplaces"]), {"m"})


if __name__ == "__main__":
    unittest.main()
