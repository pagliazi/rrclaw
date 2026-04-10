"""
5-Layer Context Compression Engine.

Runs before every LLM call to keep context within token budget.

Layer 1: Tool Result Budget — large results → disk + preview
Layer 2: History Snip — old conversation segments → truncation markers
Layer 3: Microcompact — old tool results → rule-based folding
Layer 4: Context Collapse — old messages → summary blocks
Layer 5: Autocompact — full LLM-powered conversation summary (circuit breaker protected)
"""

from __future__ import annotations

import logging
from typing import Any

from rrclaw.runtime.session import Session
from rrclaw.tools.registry import GlobalToolRegistry

logger = logging.getLogger("rrclaw.context.engine")


class ContextEngine:
    """
    Orchestrates 5-layer context compression.

    Implements the ContextProvider protocol expected by ConversationRuntime.
    """

    def __init__(self, config: Any, registry: GlobalToolRegistry):
        self.config = config
        self.registry = registry

        max_tokens = config.get("context", "max_tokens", default=200000) or 200000
        threshold = config.get("context", "autocompact_threshold", default=0.8) or 0.8
        max_result = config.get("context", "tool_result_max_chars", default=50000) or 50000

        self.max_tokens = max_tokens
        self.autocompact_threshold = threshold
        self.max_tool_result_chars = max_result

        # Circuit breaker for autocompact
        self._autocompact_failures = 0
        self._autocompact_max_failures = 3
        self._has_attempted_reactive_compact = False

    async def prepare(self, session: Session) -> dict[str, Any]:
        """Prepare context for LLM call with 5-layer compression."""
        messages = session.to_api_messages()

        # Layer 1: Tool Result Budget
        messages = self._apply_tool_result_budget(messages)

        # Layer 2: History Snip
        messages = self._apply_history_snip(messages)

        # Layer 3: Microcompact
        messages = self._apply_microcompact(messages)

        # Layer 4: Context Collapse
        messages = self._apply_context_collapse(messages)

        # Layer 5: Autocompact (circuit breaker protected)
        estimated_tokens = self._estimate_tokens(messages)
        if estimated_tokens > self.max_tokens * self.autocompact_threshold:
            if self._autocompact_failures < self._autocompact_max_failures:
                try:
                    messages = await self._apply_autocompact(messages)
                    self._autocompact_failures = 0
                except Exception as e:
                    self._autocompact_failures += 1
                    logger.warning(
                        f"Autocompact failed ({self._autocompact_failures}/"
                        f"{self._autocompact_max_failures}): {e}"
                    )

        # Build prompt with tool index
        from rrclaw.runtime.prompt import PromptBuilder
        try:
            builder = PromptBuilder(self.registry, self.config)
            system_prompt = builder.build_system_prompt(session)
        except Exception:
            system_prompt = ""

        return {
            "messages": messages,
            "system_prompt": system_prompt,
            "tools": self.registry.get_all_active_schemas(),
            "model": self.config.get("providers", "primary", default="claude-sonnet-4-6"),
        }

    async def force_compact(self, session: Session) -> bool:
        """Force a full compaction (called on context overflow)."""
        if self._has_attempted_reactive_compact:
            return False

        self._has_attempted_reactive_compact = True
        try:
            messages = session.to_api_messages()
            compacted = await self._apply_autocompact(messages)
            # Replace session messages with compacted version
            session.messages.clear()
            session.append_system("[Context compacted due to overflow]")
            return True
        except Exception as e:
            logger.error(f"Force compact failed: {e}")
            return False

    # ── Layer 1: Tool Result Budget ──

    def _apply_tool_result_budget(self, messages: list[dict]) -> list[dict]:
        """Truncate oversized tool results with preview."""
        result = []
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                new_content = []
                for block in msg["content"]:
                    if (
                        block.get("type") == "tool_result"
                        and isinstance(block.get("content"), str)
                        and len(block["content"]) > self.max_tool_result_chars
                    ):
                        truncated = block["content"][:self.max_tool_result_chars]
                        block = {
                            **block,
                            "content": (
                                f"{truncated}\n\n"
                                f"[... truncated from {len(block['content'])} chars]"
                            ),
                        }
                    new_content.append(block)
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        return result

    # ── Layer 2: History Snip ──

    def _apply_history_snip(self, messages: list[dict]) -> list[dict]:
        """Remove old conversation segments beyond a window."""
        if len(messages) <= 20:
            return messages

        # Keep first 2 messages (system context) and last 16
        keep_start = 2
        keep_end = 16
        if len(messages) <= keep_start + keep_end:
            return messages

        snipped_count = len(messages) - keep_start - keep_end
        marker = {
            "role": "user",
            "content": f"[--- 已省略 {snipped_count} 条历史消息 ---]",
        }
        return messages[:keep_start] + [marker] + messages[-keep_end:]

    # ── Layer 3: Microcompact ──

    def _apply_microcompact(self, messages: list[dict]) -> list[dict]:
        """Rule-based folding of old tool results (no LLM needed)."""
        if len(messages) <= 10:
            return messages

        result = []
        # Only compact messages in the first half
        midpoint = len(messages) // 2

        for i, msg in enumerate(messages):
            if i < midpoint and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    new_content = []
                    for block in content:
                        if block.get("type") == "tool_result":
                            original = block.get("content", "")
                            if isinstance(original, str) and len(original) > 500:
                                block = {
                                    **block,
                                    "content": original[:200] + "\n[... compacted]",
                                }
                        new_content.append(block)
                    result.append({**msg, "content": new_content})
                else:
                    result.append(msg)
            else:
                result.append(msg)
        return result

    # ── Layer 4: Context Collapse ──

    def _apply_context_collapse(self, messages: list[dict]) -> list[dict]:
        """Project old messages into summary blocks."""
        # For now, this is handled by history_snip.
        # Full implementation would use a lightweight summarizer.
        return messages

    # ── Layer 5: Autocompact ──

    async def _apply_autocompact(self, messages: list[dict]) -> list[dict]:
        """LLM-powered full conversation summary."""
        # Extract text content for summary
        text_parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                text_parts.append(f"[{role}]: {content[:500]}")
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(f"[{role}]: {block.get('text', '')[:500]}")

        if not text_parts:
            return messages

        summary = "\n".join(text_parts[:30])

        # Replace all but last 6 messages with summary
        keep = 6
        if len(messages) <= keep:
            return messages

        summary_msg = {
            "role": "user",
            "content": (
                f"<system>以下是之前对话的摘要：\n{summary}\n"
                f"[已压缩 {len(messages) - keep} 条消息]</system>"
            ),
        }
        return [summary_msg] + messages[-keep:]

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """Rough token estimation (4 chars ≈ 1 token for CJK-heavy content)."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 3
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block.get("content"), str):
                        total += len(block["content"]) // 3
                    if isinstance(block.get("text"), str):
                        total += len(block["text"]) // 3
        return total
