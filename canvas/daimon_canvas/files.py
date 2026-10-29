"""The downloads folder ($DAIMON_CANVAS_HOME/files): what canvas_open saved, readable a page at a time.

The agent may have no file tools of its own, so read_canvas_file reads these back (PDFs, Word, slides... as text).
"""
from __future__ import annotations

import re
from pathlib import Path

from .client import CanvasError
from .extract import Unreadable, extract_text

CACHE_SIZE = 32  # extracted text, keyed by path + mtime, so paging through a PDF doesn't re-parse it


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} MB"


def safe_name(name: str, default: str = "file") -> str:
    """A filename that's safe on any OS, keeping the extension."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return name[:80] or default
    return f"{stem[:70].rstrip(' ._') or default}.{ext[:10]}"


def folder_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*.\x00-\x1f]+', " ", name)).strip()[:50] or "untitled"


class Files:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self._text_cache: dict[tuple[Path, int, int], str] = {}

    def resolve(self, rel: str) -> Path:
        rel = rel.strip().replace("\\", "/").lstrip("/")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise CanvasError("That path isn't in the Canvas downloads folder.")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def save_bytes(self, rel: str, data: bytes) -> str:
        p = self.resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return self.rel(p)

    def write(self, rel: str, text: str) -> str:
        return self.save_bytes(rel, text.encode("utf-8"))

    def text(self, p: Path) -> str:
        st = p.stat()
        key = (p, st.st_mtime_ns, st.st_size)
        if key not in self._text_cache:
            if len(self._text_cache) >= CACHE_SIZE:
                self._text_cache.pop(next(iter(self._text_cache)))
            self._text_cache[key] = extract_text(p)
        return self._text_cache[key]

    def read(self, rel: str, start_line: int = 1, max_chars: int = 6000) -> str:
        """The file's text from start_line, up to max_chars, with a note saying where it continues."""
        p = self.resolve(rel)
        if not p.is_file():
            raise CanvasError(f"No downloaded file '{rel}'. canvas_open saves files; the paths it gives work here.")
        try:
            lines = self.text(p).splitlines()
        except Unreadable as e:
            raise CanvasError(f"Can't read {rel}: {e}")
        except OSError as e:
            raise CanvasError(f"Can't read {rel}: {e.strerror or e}")
        start = max(1, start_line)
        out, used = [], 0
        for i in range(start - 1, len(lines)):
            line = lines[i]
            if out and used + len(line) + 1 > max_chars:
                return "\n".join(out) + f"\n(continues at line {i + 1} of {len(lines)})"
            out.append(line[:max_chars])
            used += len(line) + 1
        return "\n".join(out) or "(empty file)"
