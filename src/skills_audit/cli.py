"""Find every agent skill installed on this machine and pick which ones to remove.

Scans Claude Code, Codex, Cursor, the shared ~/.agents folder and other agents'
skill folders, then writes skills.json and a self-contained skills-browser.html.
"""
from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from collections import defaultdict
from pathlib import Path

from . import __version__

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

PREVIEW_CHARS = 1200
TEMPLATE = Path(__file__).with_name("browser.html")

# User-level skill folders of other agents (the ones `npx skills` writes to).
# Each is scanned only if it exists.
OTHER_AGENT_DIRS = [
    ("cline", "Cline", ".cline/skills"),
    ("opencode", "OpenCode", ".config/opencode/skills"),
    ("gemini", "Gemini CLI", ".gemini/skills"),
    ("copilot", "GitHub Copilot", ".copilot/skills"),
    ("windsurf", "Windsurf", ".codeium/windsurf/skills"),
    ("goose", "Goose", ".config/goose/skills"),
    ("roo", "Roo Code", ".roo/skills"),
    ("kiro", "Kiro", ".kiro/skills"),
    ("qwen", "Qwen Code", ".qwen/skills"),
    ("factory", "Factory", ".factory/skills"),
    ("amp", "Amp", ".config/agents/skills"),
]

HARNESS_LABELS = {
    "claude": "Claude Code",
    "shared": "~/.agents",
    "codex": "Codex",
    "cursor": "Cursor",
    "omp": "OMP",
    "cleanup": "Fixes",
    **{key: label for key, label, _ in OTHER_AGENT_DIRS},
}

BUNDLED_CODEX_MARKETS = ("openai-bundled", "openai-primary-runtime")
BLOCK_SCALARS = {">", ">-", ">+", "|", "|-", "|+", ""}


def run(cmd, timeout=60, ok=(0,)):
    """Run a command and return stdout, or None if it is missing or fails."""
    if not shutil.which(cmd[0]):
        return None
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode in ok else None


def iso_date(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def estimate_tokens(chars):
    return round(chars / 3.5) + 8


def parse_frontmatter(text):
    """Parse the simple YAML frontmatter used by SKILL.md files."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    lines = text[3:end].strip("\n").splitlines()
    body = text[end + 4:].split("\n", 1)[-1].lstrip("\n")
    meta, i = {}, 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in BLOCK_SCALARS:
            buf = []
            while i < len(lines) and (lines[i][:1] in (" ", "\t") or not lines[i].strip()):
                buf.append(lines[i].strip())
                i += 1
            val = ("\n" if val.startswith("|") else " ").join(b for b in buf if b).strip()
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        meta[key] = val
    return meta, body


def parse_codex_config(text):
    """Read the plugin and marketplace tables from ~/.codex/config.toml."""
    if tomllib:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            pass
    cfg, section = {"plugins": {}, "marketplaces": {}}, None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^\[(plugins|marketplaces)\.(?:"([^"]+)"|([A-Za-z0-9_.-]+))\]$', s)
        if m:
            section = cfg[m.group(1)].setdefault(m.group(2) or m.group(3), {})
        elif s.startswith("["):
            section = None
        elif section is not None:
            kv = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*(.+)$', s)
            if kv:
                raw = kv.group(2).strip()
                section[kv.group(1)] = {"true": True, "false": False}.get(raw, raw.strip('"'))
    return cfg


def skill_files(root, max_depth=4):
    root = Path(root)
    if not root.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git")] if depth < max_depth else []
        if "SKILL.md" in filenames:
            found.append(Path(dirpath) / "SKILL.md")
    return sorted(found)


class Git:
    def __init__(self):
        self.roots = {}

    def root(self, path):
        path = Path(path)
        d = path if path.is_dir() else path.parent
        if d not in self.roots:
            out = run(["git", "-C", str(d), "rev-parse", "--show-toplevel"])
            self.roots[d] = Path(out.strip()).resolve() if out else None
        return self.roots[d]

    def state(self, root, path):
        out = run(["git", "-C", str(root), "status", "--porcelain", "--ignored", "--", str(path)])
        if out is None:
            return "unknown"
        codes = {line[:2] for line in out.splitlines() if line}
        if not codes:
            return "tracked"
        if codes == {"??"}:
            return "untracked"
        if codes == {"!!"}:
            return "ignored"
        return "modified"

    def deleted(self, root, path):
        out = run(["git", "-C", str(root), "ls-files", "--deleted", "--", str(path)])
        return out.split() if out else []


class Chezmoi:
    def __init__(self, enabled):
        self.managed = set()
        if enabled:
            out = run(["chezmoi", "managed", "--path-style", "absolute"])
            self.managed = {Path(p) for p in out.splitlines()} if out else set()

    def manages(self, path):
        return Path(path) in self.managed

    def source_path(self, target):
        out = run(["chezmoi", "source-path", str(target)])
        return Path(out.strip()) if out else None

    def rendered_json(self, target):
        out = run(["chezmoi", "cat", str(target)])
        try:
            return json.loads(out) if out else None
        except ValueError:
            return None


class Scanner:
    def __init__(self, home, offline=False, usage=True):
        self.home = Path(home).resolve()
        self.offline = offline
        self.usage = usage
        self.git = Git()
        self.chezmoi = Chezmoi(enabled=not offline)
        self.units = []
        self.lock = {"skills": {}}
        self.skills_repo = None

    # ------------------------------------------------------------ helpers
    def tilde(self, p):
        h = str(self.home)
        for cand in (Path(p), Path(p).resolve()):
            s = str(cand)
            if s == h or s.startswith(h + "/"):
                return "~" + s[len(h):]
        return str(p)

    def home_rel(self, p):
        p = Path(p)
        return str(p.relative_to(self.home)) if p.is_relative_to(self.home) else None

    def item(self, md, kind="skill", usage_key=None):
        text = md.read_text(errors="replace")
        meta, body = parse_frontmatter(text)
        name = meta.get("name") or (md.parent.name if kind == "skill" else md.stem)
        desc = meta.get("description", "")
        flags = []
        if str(meta.get("disable-model-invocation", "")).lower() == "true":
            flags.append("manual-only")
        if str(meta.get("user-invocable", "")).lower() == "false":
            flags.append("model-only")
        return {
            "name": name,
            "kind": kind,
            "description": desc,
            "path": self.tilde(md),
            "alwaysOnTok": estimate_tokens(len(name) + len(desc)),
            "invokeTok": estimate_tokens(len(text)),
            "preview": body[:PREVIEW_CHARS] + ("\n…" if len(body) > PREVIEW_CHARS else ""),
            "flags": flags,
            "usageKey": usage_key or name,
            "dirName": md.parent.name if kind == "skill" else md.stem,
        }

    def plugin_items(self, install, prefix):
        install = Path(install)
        items = [self.item(p, "skill", f"{prefix}:{p.parent.name}") for p in skill_files(install / "skills")]
        for kind, sub in (("agent", "agents"), ("command", "commands")):
            d = install / sub
            if d.is_dir():
                items += [self.item(p, kind, f"{prefix}:{p.stem}") for p in sorted(d.glob("*.md"))]
        return items

    def add(self, **unit):
        unit.setdefault("removable", True)
        unit.setdefault("notes", [])
        unit.setdefault("items", [])
        unit.setdefault("steps", [])
        unit.setdefault("backup", [])
        self.units.append(unit)
        return unit

    def delete_folder(self, path):
        """Steps, backup paths and notes for deleting a skill folder or symlink."""
        path = Path(path)
        if path.is_symlink():
            return [{"run": f"command rm {self.tilde(path)}"}], [], []
        real = path.resolve()
        if self.chezmoi.manages(path) or self.chezmoi.manages(path / "SKILL.md"):
            return ([{"run": f"chezmoi destroy --force {self.tilde(path)}"}], [],
                    ["chezmoi manages this folder, so destroy removes it from the source state too."])
        root = self.git.root(real)
        if root:
            rel = real.relative_to(root)
            state = self.git.state(root, rel)
            if state in ("tracked", "modified"):
                force = " -f" if state == "modified" else ""
                backup = [self.home_rel(real)] if state == "modified" and self.home_rel(real) else []
                return ([{"run": f"git -C {self.tilde(root)} rm -r -q{force} {rel}"}], backup,
                        [f"Tracked in the git repo {self.tilde(root)}; git keeps the history."])
        backup = [self.home_rel(real)] if self.home_rel(real) else []
        notes = [f"In the git repo {self.tilde(root)} but not tracked, so git has no copy. The backup step covers it."] if root else []
        return [{"run": f"command rm -rf {self.tilde(real)}"}], backup, notes

    def folder_unit(self, path, harness, group, what, **extra):
        path = Path(path)
        broken = path.is_symlink() and not path.exists()
        steps, backup, notes = self.delete_folder(path)
        if path.is_symlink():
            notes.append(f"Symlink to {os.readlink(path)}" + (" (broken)." if broken else ". Only the link goes; the target stays."))
        if broken:
            what = f"Broken symlink {self.tilde(path)}; its target is gone"
        md = path / "SKILL.md"
        items = [] if broken or not md.exists() else [self.item(md)]
        mtime = path.lstat().st_mtime
        return self.add(id=f"{harness}-skill:{self.tilde(path)}", harness=harness, group=group, label=path.name,
                        type="broken-link" if broken else ("symlink" if path.is_symlink() else "skill"),
                        path=self.tilde(path), what=what, installed=iso_date(mtime),
                        steps=steps + extra.pop("steps", []), backup=backup,
                        notes=notes + extra.pop("notes", []), items=items, **extra)

    # ------------------------------------------------------------ sources
    def claude(self):
        claude = self.home / ".claude"
        skills = claude / "skills"
        if skills.is_dir():
            root = self.git.root(skills.resolve())
            if root:
                self.skills_repo = (root, skills.resolve())
            for d in sorted(skills.iterdir()):
                if d.name == "synced" or not (d / "SKILL.md").exists():
                    continue
                unit = self.folder_unit(d, "claude", "Claude Code · your skills", f'Claude Code skill "{d.name}" in {self.tilde(skills)}')
                root = self.git.root(d.resolve())
                if root:
                    unit["repo"] = {"root": self.tilde(root), "state": self.git.state(root, d.resolve().relative_to(root))}
            for md in skill_files(skills / "synced", 3):
                self.add(id=f"claude-synced:{md.parent.name}", harness="claude", group="Claude Code · synced from claude.ai",
                         label=md.parent.name, type="synced", path=self.tilde(md.parent),
                         what=f'Skill "{md.parent.name}" synced from claude.ai', removable=False,
                         lockedReason="Synced from your claude.ai account. Deleting the folder just gets it re-synced, so turn it off in claude.ai instead.",
                         items=[self.item(md, usage_key=f"anthropic-skills:{md.parent.name}")])
        self.claude_plugins(claude)

    def claude_plugins(self, claude):
        manifest = claude / "plugins/installed_plugins.json"
        if not manifest.exists():
            return
        installed = json.loads(manifest.read_text()).get("plugins", {})
        settings_path = claude / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        markets = settings.get("extraKnownMarketplaces", {})
        source = self.chezmoi.source_path(settings_path) if self.chezmoi.manages(settings_path) else None
        source_text = source.read_text() if source and source.exists() else ""
        for pid, entries in sorted(installed.items()):
            entry = entries[0]
            name, market = pid.split("@", 1)
            items = self.plugin_items(entry["installPath"], name)
            if not items:
                continue
            steps = [{"run": f"claude plugin uninstall {pid}"}]
            notes = []
            if market in markets and not any(k.endswith("@" + market) and k != pid for k in installed):
                steps.append({"run": f"claude plugin marketplace remove {market}"})
            if source:
                if f'"{pid}"' in source_text:
                    steps.append({"do": f'Delete the "{pid}" entry from enabledPlugins in {self.tilde(source)} so `chezmoi apply` doesn\'t re-enable it.'})
                else:
                    notes.append(f"Your chezmoi source ({self.tilde(source)}) doesn't list this plugin, so nothing to change there.")
            counts = defaultdict(int)
            for it in items:
                counts[it["kind"]] += 1
            parts = [f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items()]
            self.add(id=f"claude-plugin:{pid}", harness="claude", group="Claude Code · plugins", label=pid, type="plugin",
                     path=self.tilde(entry["installPath"]), what=f"Claude Code plugin {pid} ({', '.join(parts)})",
                     installed=entry.get("installedAt", "")[:10], steps=steps, notes=notes, items=items,
                     reportedAlwaysOnTok=self.claude_plugin_cost(pid))

    def claude_plugin_cost(self, pid):
        if self.offline:
            return None
        out = run(["claude", "plugin", "details", pid])
        m = re.search(r"Always-on:\s+~([\d,]+) tok", out or "")
        return int(m.group(1).replace(",", "")) if m else None

    def shared(self):
        base = self.home / ".agents"
        lock_path = base / ".skill-lock.json"
        self.lock = json.loads(lock_path.read_text()) if lock_path.exists() else {"skills": {}}
        skills = base / "skills"
        if not skills.is_dir():
            return
        claude_skills = self.home / ".claude/skills"
        for p in sorted(skills.iterdir()):
            if p.name.startswith("."):
                continue
            meta = self.lock.get("skills", {}).get(p.name)
            steps, notes = [], []
            if meta:
                steps.append({"do": f'Remove the "{p.name}" key from "skills" in ~/.agents/.skill-lock.json.'})
                notes.append(f"Installed with `npx skills` from {meta.get('source', 'unknown source')}.")
            twin = claude_skills / p.name
            if not p.is_symlink() and twin.is_dir():
                same = not filecmp.dircmp(p, twin).diff_files
                notes.append(f"A copy of {self.tilde(twin)} ({'identical' if same else 'differs'}).")
            kind = "Symlink" if p.is_symlink() else "Skill folder"
            unit = self.folder_unit(p, "shared", "Shared · ~/.agents/skills",
                                    f'{kind} "{p.name}" in ~/.agents/skills, the shared folder Codex and other agents read',
                                    steps=steps, notes=notes)
            if meta:
                unit["installed"] = meta.get("installedAt", "")[:10] or unit["installed"]
                unit["returns"] = "Running `npx skills add` for it again reinstalls it."

    def codex(self):
        codex = self.home / ".codex"
        skills = codex / "skills"
        if skills.is_dir():
            for p in sorted(skills.iterdir()):
                if p.name.startswith(".") or not (p.is_symlink() or (p / "SKILL.md").exists()):
                    continue
                self.folder_unit(p, "codex", "Codex · ~/.codex/skills", f'Codex skill "{p.name}" in ~/.codex/skills')
            system = [self.item(md) for md in skill_files(skills / ".system", 2)]
            if system:
                self.add(id="codex-system", harness="codex", group="Codex · built-in", label="Codex system skills",
                         type="builtin", path="~/.codex/skills/.system", what="Skills that ship with Codex",
                         removable=False, lockedReason="Ships with Codex.", items=system)
        memory = codex / "memories/MEMORY.md"
        memory_text = memory.read_text() if memory.exists() else ""
        for md in skill_files(codex / "memories/skills", 2):
            name = md.parent.name
            extra = []
            if f"skills/{name}/SKILL.md" in memory_text:
                extra.append({"do": f'Delete the lines in ~/.codex/memories/MEMORY.md that point at "skills/{name}/SKILL.md".'})
            unit = self.folder_unit(md.parent, "codex", "Codex · memory skills",
                                    f'Skill "{name}" that Codex wrote from its memories', steps=extra)
            unit["returns"] = "Codex writes skills from its memories and may write a similar one later."
        self.codex_plugins(codex)

    def codex_plugins(self, codex):
        cfg_path = codex / "config.toml"
        cfg = parse_codex_config(cfg_path.read_text()) if cfg_path.exists() else {}
        enabled = {k for k, v in cfg.get("plugins", {}).items() if isinstance(v, dict) and v.get("enabled")}
        markets = set(cfg.get("marketplaces", {}))
        cache = codex / "plugins/cache"
        if not cache.is_dir():
            return
        for market in sorted(p for p in cache.iterdir() if p.is_dir()):
            for plug in sorted(p for p in market.iterdir() if p.is_dir()):
                pid = f"{plug.name}@{market.name}"
                remote = (plug / ".codex-remote-plugin-install.json").exists()
                if pid not in enabled and not remote:
                    continue
                versions = sorted((v for v in plug.iterdir() if v.is_dir()), key=lambda v: v.stat().st_mtime)
                items = self.plugin_items(versions[-1], plug.name) if versions else []
                if not items:
                    continue
                if remote:
                    steps = [{"manual": "In the Codex app, open Plugins and uninstall it. It was installed from your ChatGPT workspace."}]
                elif market.name in BUNDLED_CODEX_MARKETS:
                    steps = [{"do": f'Bundled with Codex, so disable it instead: set `enabled = false` under [plugins."{pid}"] in ~/.codex/config.toml.'}]
                else:
                    steps = [{"run": f"codex plugin remove {pid}"}]
                    if market.name in markets and not any(k.endswith("@" + market.name) and k != pid for k in enabled):
                        steps.append({"run": f"codex plugin marketplace remove {market.name}"})
                self.add(id=f"codex-plugin:{pid}", harness="codex", group="Codex · plugins", label=pid, type="plugin",
                         path=self.tilde(versions[-1]), what=f"Codex plugin {pid} ({len(items)} skills)",
                         installed=iso_date(versions[-1].stat().st_mtime), steps=steps, items=items)

    def cursor(self):
        cursor = self.home / ".cursor"
        user = cursor / "skills"
        if user.is_dir():
            for p in sorted(user.iterdir()):
                if p.is_symlink() or (p / "SKILL.md").exists():
                    self.folder_unit(p, "cursor", "Cursor · ~/.cursor/skills", f'Cursor skill "{p.name}" in ~/.cursor/skills')
        builtin = [self.item(md) for md in skill_files(cursor / "skills-cursor", 2)]
        if builtin:
            self.add(id="cursor-builtin", harness="cursor", group="Cursor · built-in", label="Cursor built-in skills",
                     type="builtin", path="~/.cursor/skills-cursor", what="Skills that ship with Cursor",
                     removable=False, lockedReason="Cursor syncs these itself.", items=builtin)
        cache = cursor / "plugins/cache"
        if not cache.is_dir():
            return
        manifest_path = cache / ".cloud-plugin-manifest.json"
        manifest = {}
        if manifest_path.exists():
            manifest = {str(p["pluginId"]): p for p in json.loads(manifest_path.read_text()).get("plugins", [])}
        for market in sorted(p for p in cache.iterdir() if p.is_dir()):
            plugs = sorted(p for p in market.iterdir() if p.is_dir())
            team = any(p.name in manifest for p in plugs)
            for plug in plugs:
                if team and plug.name not in manifest:
                    continue  # stale copy; team marketplaces list live plugins by id in the manifest
                versions = sorted((v for v in plug.iterdir() if v.is_dir()), key=lambda v: v.stat().st_mtime)
                if not versions:
                    continue
                name = manifest.get(plug.name, {}).get("name", plug.name)
                items = self.plugin_items(versions[-1], name)
                if not items:
                    continue
                self.add(id=f"cursor-plugin:{market.name}/{name}", harness="cursor", group="Cursor · plugins",
                         label=f"{name}@{market.name}", type="plugin", path=self.tilde(versions[-1]),
                         what=f"Cursor plugin {name} from the {market.name} marketplace ({len(items)} skills)",
                         installed=iso_date(versions[-1].stat().st_mtime), items=items,
                         steps=[{"manual": f"In Cursor, open Settings > Plugins and uninstall {name}. Cursor has no command for this."}])

    def omp(self):
        for md in skill_files(self.home / ".omp/agent/managed-skills", 2):
            unit = self.folder_unit(md.parent, "omp", "OMP · managed skills",
                                    f'Skill "{md.parent.name}" that OMP wrote in ~/.omp/agent/managed-skills')
            unit["returns"] = "OMP writes these itself and may write a similar one later."

    def other_agents(self):
        for key, label, rel in OTHER_AGENT_DIRS:
            d = self.home / rel
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if p.name.startswith(".") or not (p.is_symlink() or (p / "SKILL.md").exists()):
                    continue
                self.folder_unit(p, key, f"{label} · ~/{rel}", f'{label} skill "{p.name}" in ~/{rel}')

    def fixes(self):
        stale = sorted(n for n in self.lock.get("skills", {}) if not (self.home / ".agents/skills" / n).exists())
        if stale:
            self.add(id="cleanup:agents-lock-stale", harness="cleanup", group="Fixes", type="cleanup",
                     label=f"{len(stale)} stale entries in ~/.agents/.skill-lock.json", path="~/.agents/.skill-lock.json",
                     what="Entries in the `npx skills` lock file whose folders are already gone",
                     steps=[{"do": 'Remove these keys from "skills" in ~/.agents/.skill-lock.json: ' + ", ".join(stale) + "."}],
                     notes=["Already deleted: " + ", ".join(stale)])
        plugins_dir = self.home / ".claude/plugins"
        for p in sorted(plugins_dir.iterdir()) if plugins_dir.is_dir() else []:
            if p.is_symlink() and not p.exists():
                self.add(id=f"cleanup:broken-link:{self.tilde(p)}", harness="cleanup", group="Fixes", type="cleanup",
                         label=f"Broken symlink {self.tilde(p)}", path=self.tilde(p),
                         what=f"Broken symlink {self.tilde(p)} pointing at {os.readlink(p)}, which doesn't exist",
                         steps=[{"run": f"command rm {self.tilde(p)}"}])
        self.chezmoi_drift()
        self.skills_repo_fixes()

    def chezmoi_drift(self):
        settings = self.home / ".claude/settings.json"
        if not settings.exists() or not self.chezmoi.manages(settings):
            return
        rendered = self.chezmoi.rendered_json(settings)
        if rendered is None:
            return
        live = set(json.loads(settings.read_text()).get("enabledPlugins", {}))
        repo = set(rendered.get("enabledPlugins", {}))
        source = self.chezmoi.source_path(settings)
        where = self.tilde(source) if source else "your chezmoi source for ~/.claude/settings.json"
        steps, notes = [], []
        if repo - live:
            steps.append({"do": f"In {where}, delete these from enabledPlugins: {', '.join(sorted(repo - live))}."})
            notes.append("`chezmoi apply` would re-enable plugins you've already removed.")
        if live - repo:
            steps.append({"do": f"In {where}, add these to enabledPlugins: {', '.join(sorted(live - repo))}."})
            notes.append("`chezmoi apply` would drop plugins you have installed now.")
        if steps:
            self.add(id="cleanup:chezmoi-plugin-drift", harness="cleanup", group="Fixes", type="cleanup",
                     label="Plugin list drift between ~/.claude/settings.json and chezmoi", path=where,
                     what="Claude Code's enabledPlugins differs between the live settings.json and your chezmoi source",
                     steps=steps, notes=notes)

    def skills_repo_fixes(self):
        if not self.skills_repo:
            return
        root, skills_dir = self.skills_repo
        rel = skills_dir.relative_to(root)
        synced = skills_dir / "synced"
        if synced.is_dir() and self.git.state(root, synced.relative_to(root)) == "untracked":
            self.add(id="cleanup:gitignore-synced", harness="cleanup", group="Fixes", type="cleanup",
                     label=f"claude.ai-synced skills land in {self.tilde(root)}", path=self.tilde(synced),
                     what=f"~/.claude/skills points into the git repo {self.tilde(root)}, so skills synced from claude.ai show up there as untracked files",
                     steps=[{"do": f"Add the line `{rel}/synced/` to {self.tilde(root)}/.gitignore."}])
        gone = sorted({Path(f).relative_to(rel).parts[0] for f in self.git.deleted(root, rel)})
        if gone:
            self.add(id="cleanup:uncommitted-deletions", harness="cleanup", group="Fixes", type="cleanup",
                     label=f"{len(gone)} deleted skills not committed in {self.tilde(root)}", path=self.tilde(skills_dir),
                     what=f"Skill folders deleted from {self.tilde(skills_dir)} but still in git",
                     steps=[{"run": f"git -C {self.tilde(root)} add -A -- {rel}"}],
                     notes=["Deleted: " + ", ".join(gone)])

    # ------------------------------------------------------------ usage
    def claude_usage(self):
        use = defaultdict(lambda: [0, ""])
        skill_re = re.compile(r'"name":"Skill","input":\{"skill":"([^"]+)"')
        cmd_re = re.compile(r"<command-name>/([^<\s]+)</command-name>")
        ts_re = re.compile(r'"timestamp":"([0-9T:.\-]+)')
        oldest = ""
        for f in (self.home / ".claude/projects").rglob("*.jsonl"):
            first = True
            with open(f, errors="replace") as fh:
                for line in fh:
                    if first:
                        m = ts_re.search(line)
                        if m and (not oldest or m.group(1)[:10] < oldest):
                            oldest = m.group(1)[:10]
                        first = not m
                    if '"Skill"' not in line and "<command-name>" not in line:
                        continue
                    names = skill_re.findall(line) + cmd_re.findall(line)
                    ts = ts_re.search(line)
                    for n in names:
                        use[n][0] += 1
                        use[n][1] = max(use[n][1], ts.group(1)[:10] if ts else "")
        return dict(use), oldest

    def codex_usage(self):
        """Count SKILL.md reads in Codex session logs (a read means Codex used the skill)."""
        use = defaultdict(lambda: [0, ""])
        dirs = [d for d in (self.home / ".codex/sessions", self.home / ".codex/archived_sessions") if d.is_dir()]
        pattern = r'^\{"timestamp":"([^"]+)".*?"type":"function_call".*?/([A-Za-z0-9_.:-]+)/SKILL\.md'
        out = run(["rg", "--no-filename", "-o", "-r", "$1 $2", pattern, *map(str, dirs)], timeout=300, ok=(0, 1)) if dirs else ""
        if out is None:  # no ripgrep, or it failed: scan in Python
            rx, lines = re.compile(pattern), []
            for d in dirs:
                for f in d.rglob("*.jsonl"):
                    with open(f, errors="replace") as fh:
                        lines += [f"{m.group(1)} {m.group(2)}" for line in fh
                                  if "SKILL.md" in line and (m := rx.search(line))]
            out = "\n".join(lines)
        for line in out.splitlines():
            ts, name = line.split(" ", 1)
            use[name][0] += 1
            use[name][1] = max(use[name][1], ts[:10])
        dates = [m.group(1) for d in dirs for p in d.rglob("rollout-*.jsonl")
                 if (m := re.search(r"rollout-(\d{4}-\d{2}-\d{2})", p.name))]
        return dict(use), min(dates, default="")

    # ------------------------------------------------------------ scan
    def scan(self):
        for source in (self.claude, self.shared, self.codex, self.cursor, self.omp, self.other_agents, self.fixes):
            source()

        claude_use, claude_since = self.claude_usage() if self.usage else ({}, "")
        codex_use, codex_since = self.codex_usage() if self.usage else ({}, "")
        for u in self.units:
            tracked = self.usage and u["harness"] in ("claude", "shared", "codex")
            for it in u["items"]:
                c = claude_use.get(it["usageKey"], [0, ""]) if u["harness"] == "claude" else [0, ""]
                x = codex_use.get(it["dirName"], [0, ""]) if u["harness"] in ("codex", "shared") else [0, ""]
                it["usage"] = {"claude": c[0], "codex": x[0], "last": max(c[1], x[1])} if tracked else None
            u["usage"] = sum(i["usage"]["claude"] + i["usage"]["codex"] for i in u["items"]) if tracked else None
            u["lastUsed"] = max((i["usage"]["last"] for i in u["items"] if i["usage"]), default="")
            u["alwaysOnTok"] = sum(i["alwaysOnTok"] for i in u["items"])

        self.link_symlinks()
        names = defaultdict(set)
        for u in self.units:
            for it in u["items"]:
                if it["kind"] == "skill" and u["type"] != "builtin":
                    names[it["dirName"]].add(u["id"])
        for u in self.units:
            for it in u["items"]:
                it["alsoIn"] = sorted(names.get(it["dirName"], set()) - {u["id"]}) if it["kind"] == "skill" else []
                del it["usageKey"], it["dirName"]
                if it["path"].startswith(u["path"] + "/"):
                    it["path"] = it["path"][len(u["path"]) + 1:]

        return {
            "tool": "skills-audit",
            "version": __version__,
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "claudeUsageSince": claude_since,
            "codexUsageSince": codex_since,
            "harnesses": {k: v for k, v in HARNESS_LABELS.items() if any(u["harness"] == k for u in self.units)},
            "units": self.units,
        }

    def link_symlinks(self):
        """Tell each skill folder about symlinks elsewhere that point at it."""
        targets = {}
        for u in self.units:
            if u["type"] == "skill":
                targets[(self.home / u["path"][2:] if u["path"].startswith("~/") else Path(u["path"])).resolve()] = u
        for u in self.units:
            if u["type"] != "symlink":
                continue
            link = self.home / u["path"][2:]
            target = targets.get(link.resolve())
            if target and target is not u:
                target["notes"].append(f"{u['path']} links here. Remove that link too, or it will break.")
                target["steps"].append({"run": f"command rm {u['path']}"})


def default_out_dir():
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "skills-audit"


def write_outputs(data, out):
    out.mkdir(parents=True, exist_ok=True)
    (out / "skills.json").write_text(json.dumps(data, indent=1))
    payload = json.dumps(data).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", payload)
    page = out / "skills-browser.html"
    page.write_text(html)
    return page


def parse_args(argv):
    p = argparse.ArgumentParser(prog="skills-audit", description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, help="output folder (default: ~/.cache/skills-audit)")
    p.add_argument("--open", action="store_true", help="open the browser page when done")
    p.add_argument("--json", action="store_true", help="print the inventory as JSON instead of writing files")
    p.add_argument("--no-usage", action="store_true", help="skip reading Claude and Codex transcripts")
    p.add_argument("--offline", action="store_true", help="don't call the claude or chezmoi CLIs")
    p.add_argument("--home", type=Path, help="scan this folder instead of your home directory (implies --offline)")
    p.add_argument("--demo", action="store_true", help="scan a generated example home directory")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    out = args.out or default_out_dir()
    if args.demo:
        from .demo import build_demo_home
        home = build_demo_home(Path(tempfile.mkdtemp(prefix="skills-audit-demo-")))
        offline, out = True, (args.out or default_out_dir() / "demo")
    else:
        home = args.home.expanduser().resolve() if args.home else Path.home()
        offline = args.offline or bool(args.home)
    data = Scanner(home, offline=offline, usage=not args.no_usage).scan()
    if args.json:
        json.dump(data, sys.stdout, indent=1)
        print()
        return 0
    page = write_outputs(data, out)
    n_items = sum(len(u["items"]) for u in data["units"])
    print(f"Found {n_items} skills, agents and commands in {len(data['units'])} entries.")
    print(f"Browser: {page}")
    if args.open:
        webbrowser.open(page.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
