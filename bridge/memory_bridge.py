"""
Cross-System Memory Bridge.

Bridges two fundamentally different memory architectures:

  OpenClaw Memory:
    - Plain Markdown files (MEMORY.md, daily notes, DREAMS.md)
    - memory_search tool with vector similarity + keyword matching
    - Session-scoped with daily reset
    - Canvas for visual memory

  Hermes Memory:
    - SQLite SessionDB with FTS5 full-text search
    - Persistent memory via periodic nudge + extraction
    - Pluggable memory providers (Honcho user modeling)
    - Skill-based procedural memory

This bridge provides:
    - Unified search across both memory systems
    - Cross-system memory injection (feed relevant context from A→B)
    - Memory export/import between formats
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bridge.memory")

OPENCLAW_MEMORY_DIR = Path(os.getenv(
    "OPENCLAW_MEMORY_DIR",
    os.path.expanduser("~/.openclaw/agents/hermes-bridge/memory"),
))
HERMES_MEMORY_DIR = Path(os.getenv(
    "HERMES_MEMORY_DIR",
    os.path.expanduser("~/.hermes/memories"),
))


class MemoryBridge:
    """Bridges OpenClaw and Hermes memory systems."""

    def __init__(self):
        self._hermes_db = None

    def _get_hermes_db(self):
        """Lazy-load Hermes SessionDB."""
        if self._hermes_db is None:
            try:
                from hermes_state import SessionDB
                self._hermes_db = SessionDB()
            except ImportError:
                logger.warning("hermes_state not available for memory search")
        return self._hermes_db

    # ── Unified Search ──

    async def search(
        self,
        query: str,
        sources: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Search across both memory systems and merge results.

        Args:
            query: Search query
            sources: ["hermes", "openclaw"] or None for both
            limit: Max results per source
        """
        sources = sources or ["hermes", "openclaw"]
        results = []

        if "hermes" in sources:
            results.extend(self._search_hermes(query, limit))

        if "openclaw" in sources:
            results.extend(self._search_openclaw(query, limit))

        # Sort by relevance (simple: exact match > partial)
        query_lower = query.lower()
        results.sort(
            key=lambda r: (
                0 if query_lower in r.get("content", "").lower() else 1,
                -r.get("score", 0),
            )
        )
        return results[:limit]

    def _search_hermes(self, query: str, limit: int) -> list[dict]:
        """Search Hermes FTS5 session database."""
        db = self._get_hermes_db()
        if not db:
            return []

        try:
            raw = db.search(query, limit=limit)
            return [
                {
                    "source": "hermes",
                    "type": "session",
                    "content": r.get("content", "")[:500],
                    "role": r.get("role", ""),
                    "session_id": r.get("session_id", ""),
                    "timestamp": r.get("timestamp", ""),
                    "score": r.get("rank", 0),
                }
                for r in raw
            ]
        except Exception as e:
            logger.error(f"Hermes memory search error: {e}")
            return []

    def _search_openclaw(self, query: str, limit: int) -> list[dict]:
        """Search OpenClaw Markdown memory files."""
        results = []
        query_lower = query.lower()

        # Search MEMORY.md
        memory_md = OPENCLAW_MEMORY_DIR / "MEMORY.md"
        if memory_md.exists():
            content = memory_md.read_text(encoding="utf-8")
            if query_lower in content.lower():
                results.append({
                    "source": "openclaw",
                    "type": "long_term",
                    "content": content[:500],
                    "file": "MEMORY.md",
                    "score": 1.0,
                })

        # Search daily notes
        for note in sorted(OPENCLAW_MEMORY_DIR.glob("*.md"), reverse=True):
            if note.name == "MEMORY.md" or note.name == "DREAMS.md":
                continue
            try:
                content = note.read_text(encoding="utf-8")
                if query_lower in content.lower():
                    results.append({
                        "source": "openclaw",
                        "type": "daily_note",
                        "content": content[:500],
                        "file": note.name,
                        "score": 0.8,
                    })
            except Exception:
                continue

            if len(results) >= limit:
                break

        return results[:limit]

    # ── Cross-System Memory Injection ──

    def inject_hermes_context_to_openclaw(
        self, query: str, limit: int = 5
    ) -> str:
        """
        Retrieve relevant Hermes memories and format them for
        injection into an OpenClaw agent's context.

        Returns a formatted string suitable for prepending to
        an OpenClaw agent's system prompt.
        """
        memories = self._search_hermes(query, limit)
        if not memories:
            return ""

        lines = ["[Hermes Agent Memory Context]"]
        for m in memories:
            ts = m.get("timestamp", "")
            content = m.get("content", "")
            lines.append(f"- [{ts}] {content}")
        return "\n".join(lines)

    def inject_openclaw_context_to_hermes(
        self, query: str, limit: int = 5
    ) -> str:
        """
        Retrieve relevant OpenClaw memories and format them for
        injection into a Hermes agent's context.
        """
        memories = self._search_openclaw(query, limit)
        if not memories:
            return ""

        lines = ["[OpenClaw Memory Context]"]
        for m in memories:
            source_file = m.get("file", "unknown")
            content = m.get("content", "")
            lines.append(f"- [{source_file}] {content}")
        return "\n".join(lines)

    # ── Memory Export/Import ──

    def export_hermes_to_markdown(self, output_dir: Optional[Path] = None) -> Path:
        """
        Export Hermes session memories to OpenClaw-compatible
        Markdown format (daily notes).
        """
        output_dir = output_dir or OPENCLAW_MEMORY_DIR
        output_dir.mkdir(parents=True, exist_ok=True)

        db = self._get_hermes_db()
        if not db:
            return output_dir

        try:
            sessions = db.list_sessions(limit=30)
            for session in sessions:
                date = session.get("date", datetime.now().strftime("%Y-%m-%d"))
                messages = db.get_messages(session["id"])

                note_path = output_dir / f"{date}-hermes.md"
                lines = [f"# Hermes Session — {date}\n"]
                for msg in messages:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")[:1000]
                    lines.append(f"**{role}**: {content}\n")

                note_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.error(f"Export error: {e}")

        return output_dir

    def import_openclaw_to_hermes(self) -> int:
        """
        Import OpenClaw MEMORY.md content into Hermes persistent memory.
        """
        memory_md = OPENCLAW_MEMORY_DIR / "MEMORY.md"
        if not memory_md.exists():
            return 0

        content = memory_md.read_text(encoding="utf-8")
        dest = HERMES_MEMORY_DIR / "openclaw-imported.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            f"# Imported from OpenClaw\n\n{content}",
            encoding="utf-8",
        )
        logger.info(f"Imported OpenClaw memory to {dest}")
        return 1
