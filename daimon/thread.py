"""The main thread: one long-running conversation, persisted to disk and compacted as it grows."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

Summarizer = Callable[[str, list[dict[str, Any]]], Awaitable[str]]


class Thread:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log = folder / "messages.jsonl"
        self.summary_file = folder / "summary.md"
        self.messages: list[dict[str, Any]] = []
        if self.log.exists():
            for line in self.log.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.messages.append(json.loads(line))

    @property
    def summary(self) -> str:
        return self.summary_file.read_text(encoding="utf-8") if self.summary_file.exists() else ""

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        with self.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def repair(self) -> None:
        """If a turn was interrupted between a tool call and its result, close the gap so providers accept the
        history."""
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                answered = {r.get("tool_call_id") for r in self.messages[i + 1:] if r.get("role") == "tool"}
                for call in m["tool_calls"]:
                    if call["id"] not in answered:
                        self.append({"role": "tool", "tool_call_id": call["id"], "content": "(interrupted)"})
                return
            if m.get("role") == "user":
                return

    def tokens(self) -> int:
        return len(json.dumps(self.messages, ensure_ascii=False)) // 4

    def _rewrite(self) -> None:
        self.log.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in self.messages),
                            encoding="utf-8")

    async def compact(self, summarize: Summarizer, keep_recent: int) -> bool:
        """Fold everything but the last `keep_recent` messages into summary.md. The cut lands on a user
        message so tool calls and their results are never split."""
        cut = len(self.messages) - keep_recent
        while cut > 0 and self.messages[cut].get("role") != "user":
            cut -= 1
        if cut <= 0:
            return False
        old, self.messages = self.messages[:cut], self.messages[cut:]
        archive = self.folder / "archive"
        archive.mkdir(exist_ok=True)
        (archive / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl").write_text(
            "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in old), encoding="utf-8")
        self.summary_file.write_text(await summarize(self.summary, old), encoding="utf-8")
        self._rewrite()
        return True

    def clear(self) -> None:
        if self.messages:
            archive = self.folder / "archive"
            archive.mkdir(exist_ok=True)
            self.log.replace(archive / f"{time.strftime('%Y%m%d-%H%M%S')}-cleared.jsonl")
        self.messages = []
