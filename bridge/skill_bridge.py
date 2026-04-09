"""
Cross-System Skill Synchronization.

Both OpenClaw and Hermes use the AgentSkills-compatible SKILL.md format,
making bidirectional skill sharing straightforward.

This module:
  - Exports Hermes skills (118 built-in + learned) into OpenClaw workspace
  - Imports OpenClaw skills (5,400+ on ClawHub) into Hermes skill directory
  - Translates tool references between the two registries
  - Tracks sync state to avoid redundant copies
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bridge.skills")

# Default skill directories
HERMES_SKILLS_DIR = Path(os.getenv(
    "HERMES_SKILLS_DIR",
    os.path.expanduser("~/.hermes/skills"),
))
OPENCLAW_SKILLS_DIR = Path(os.getenv(
    "OPENCLAW_SKILLS_DIR",
    os.path.expanduser("~/.openclaw/skills"),
))
SYNC_STATE_FILE = Path(os.getenv(
    "SKILL_SYNC_STATE",
    os.path.expanduser("~/.hermes/bridge_skill_sync.json"),
))

# Tool name mapping between systems
TOOL_NAME_MAP = {
    # Hermes tool → OpenClaw equivalent
    "web_search": "webSearch",
    "web_extract": "webExtract",
    "terminal": "exec",
    "read_file": "read",
    "write_file": "write",
    "patch": "edit",
    "search_files": "glob",
    "browser_navigate": "browserNavigate",
    "browser_click": "browserClick",
    "browser_type": "browserType",
    "image_generate": "imageGenerate",
    "image_analyze": "imageAnalyze",
    "delegation": "agentDelegate",
    "execute_code": "exec",
    "session_search": "memorySearch",
}

# Reverse mapping
REVERSE_TOOL_MAP = {v: k for k, v in TOOL_NAME_MAP.items()}


class SkillBridge:
    """Synchronizes skills between Hermes and OpenClaw."""

    def __init__(
        self,
        hermes_dir: Path = HERMES_SKILLS_DIR,
        openclaw_dir: Path = OPENCLAW_SKILLS_DIR,
    ):
        self.hermes_dir = hermes_dir
        self.openclaw_dir = openclaw_dir
        self._sync_state: dict = {}
        self._load_sync_state()

    def _load_sync_state(self):
        if SYNC_STATE_FILE.exists():
            try:
                self._sync_state = json.loads(
                    SYNC_STATE_FILE.read_text(encoding="utf-8")
                )
            except Exception:
                self._sync_state = {}

    def _save_sync_state(self):
        SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SYNC_STATE_FILE.write_text(
            json.dumps(self._sync_state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── Hermes → OpenClaw ──

    def export_hermes_skills(
        self, categories: Optional[list[str]] = None, overwrite: bool = False
    ) -> int:
        """
        Export Hermes skills into OpenClaw workspace.

        Hermes skills use the same SKILL.md format as OpenClaw,
        so the main work is:
          1. Copy the skill folder
          2. Translate tool references in SKILL.md
          3. Add openclaw-specific frontmatter fields if missing
        """
        if not self.hermes_dir.exists():
            logger.warning(f"Hermes skills directory not found: {self.hermes_dir}")
            return 0

        self.openclaw_dir.mkdir(parents=True, exist_ok=True)
        exported = 0

        for skill_dir in self.hermes_dir.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            skill_name = skill_dir.name
            if categories:
                category = skill_dir.parent.name
                if category not in categories:
                    continue

            # Check sync state
            state_key = f"hermes→openclaw:{skill_name}"
            src_mtime = skill_md.stat().st_mtime
            if not overwrite and state_key in self._sync_state:
                if self._sync_state[state_key].get("mtime", 0) >= src_mtime:
                    continue  # Already synced, no changes

            # Copy and translate
            dest_dir = self.openclaw_dir / "hermes-bridge" / skill_name
            dest_dir.mkdir(parents=True, exist_ok=True)

            content = skill_md.read_text(encoding="utf-8")
            translated = self._translate_tools_hermes_to_oc(content)

            (dest_dir / "SKILL.md").write_text(translated, encoding="utf-8")

            # Copy any supporting files
            for f in skill_dir.iterdir():
                if f.name != "SKILL.md" and f.is_file():
                    shutil.copy2(f, dest_dir / f.name)

            self._sync_state[state_key] = {
                "mtime": src_mtime,
                "synced_at": datetime.now().isoformat(),
            }
            exported += 1

        self._save_sync_state()
        logger.info(f"Exported {exported} Hermes skills to OpenClaw")
        return exported

    # ── OpenClaw → Hermes ──

    def import_openclaw_skills(
        self, categories: Optional[list[str]] = None, overwrite: bool = False
    ) -> int:
        """
        Import OpenClaw skills into Hermes skill directory.

        OpenClaw has 5,400+ skills on ClawHub.  This imports from
        the local OpenClaw skill directory (managed skills).
        """
        if not self.openclaw_dir.exists():
            logger.warning(f"OpenClaw skills directory not found: {self.openclaw_dir}")
            return 0

        imported = 0
        target_base = self.hermes_dir / "openclaw-imported"
        target_base.mkdir(parents=True, exist_ok=True)

        for skill_path in self.openclaw_dir.rglob("SKILL.md"):
            skill_dir = skill_path.parent
            skill_name = skill_dir.name

            if categories:
                # Check if skill category matches
                content = skill_path.read_text(encoding="utf-8")
                if not any(cat in content.lower() for cat in categories):
                    continue

            state_key = f"openclaw→hermes:{skill_name}"
            src_mtime = skill_path.stat().st_mtime
            if not overwrite and state_key in self._sync_state:
                if self._sync_state[state_key].get("mtime", 0) >= src_mtime:
                    continue

            dest_dir = target_base / skill_name
            dest_dir.mkdir(parents=True, exist_ok=True)

            content = skill_path.read_text(encoding="utf-8")
            translated = self._translate_tools_oc_to_hermes(content)
            (dest_dir / "SKILL.md").write_text(translated, encoding="utf-8")

            for f in skill_dir.iterdir():
                if f.name != "SKILL.md" and f.is_file():
                    shutil.copy2(f, dest_dir / f.name)

            self._sync_state[state_key] = {
                "mtime": src_mtime,
                "synced_at": datetime.now().isoformat(),
            }
            imported += 1

        self._save_sync_state()
        logger.info(f"Imported {imported} OpenClaw skills to Hermes")
        return imported

    # ── Tool reference translation ──

    def _translate_tools_hermes_to_oc(self, content: str) -> str:
        """Replace Hermes tool names with OpenClaw equivalents in SKILL.md."""
        for hermes_name, oc_name in TOOL_NAME_MAP.items():
            content = content.replace(f"`{hermes_name}`", f"`{oc_name}`")
            content = content.replace(f"tools: [{hermes_name}", f"tools: [{oc_name}")
        return content

    def _translate_tools_oc_to_hermes(self, content: str) -> str:
        """Replace OpenClaw tool names with Hermes equivalents in SKILL.md."""
        for oc_name, hermes_name in REVERSE_TOOL_MAP.items():
            content = content.replace(f"`{oc_name}`", f"`{hermes_name}`")
            content = content.replace(f"tools: [{oc_name}", f"tools: [{hermes_name}")
        return content

    # ── Listing ──

    def list_hermes_skills(self) -> list[dict]:
        """List all available Hermes skills."""
        return self._list_skills_in(self.hermes_dir)

    def list_openclaw_skills(self) -> list[dict]:
        """List all available OpenClaw skills."""
        return self._list_skills_in(self.openclaw_dir)

    @staticmethod
    def _list_skills_in(base_dir: Path) -> list[dict]:
        results = []
        if not base_dir.exists():
            return results
        for skill_md in base_dir.rglob("SKILL.md"):
            name = skill_md.parent.name
            desc = ""
            try:
                for line in skill_md.read_text(encoding="utf-8").split("\n"):
                    if line.strip().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"\'')
                        break
            except Exception:
                pass
            results.append({"name": name, "description": desc})
        return results

    def get_sync_status(self) -> dict:
        """Return current sync state summary."""
        h2o = sum(1 for k in self._sync_state if k.startswith("hermes→openclaw"))
        o2h = sum(1 for k in self._sync_state if k.startswith("openclaw→hermes"))
        return {
            "hermes_to_openclaw": h2o,
            "openclaw_to_hermes": o2h,
            "total_synced": h2o + o2h,
            "hermes_skills_available": len(self.list_hermes_skills()),
            "openclaw_skills_available": len(self.list_openclaw_skills()),
        }
