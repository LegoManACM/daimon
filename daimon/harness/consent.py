"""Consent for a harness's own actions. Inside its workspace a harness works freely; actions that reach outside it
(pushing, opening PRs, publishing, writing files elsewhere, other MCP servers) get a level like any tool.

Rules match "Tool(argument)" with globs, e.g. "Bash(*git push*)": ask, "mcp__github__get_*": auto. The argument
is the command for shell tools and the path for file tools. First match wins: the worker's `harness_consent`,
then the harness entry's `consent`, then DEFAULTS. Writes outside the workspace ask unless a rule says otherwise.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

DEFAULTS = {
    "Bash(*git push*)": "ask",
    "Bash(*gh pr create*)": "ask",
    "Bash(*gh pr merge*)": "ask",
    "Bash(*gh release*)": "ask",
    "Bash(*publish*)": "ask",
    "Bash(*deploy*)": "ask",
    "mcp__daimon__*": "auto",  # our own tools; they have their own levels
    "mcp__*": "ask",       # the harness's other MCP servers can act on real accounts
}
SHELL = {"Bash", "Shell", "shell", "run_shell_command", "exec_command"}
WRITES = {"Edit", "Write", "MultiEdit", "NotebookEdit", "write_file", "replace", "apply_patch"}


def describe(tool: str, args: dict[str, Any]) -> str:
    arg = args.get("command") or args.get("file_path") or args.get("notebook_path") or args.get("path") or ""
    return f"{tool}({arg})" if arg else tool


def level(tool: str, args: dict[str, Any], workspace: Path, *rule_sets: dict[str, str] | None) -> str:
    action = describe(tool, args)
    for rules in (*rule_sets, DEFAULTS):
        for pattern, lvl in (rules or {}).items():
            if fnmatch.fnmatchcase(action, pattern) or fnmatch.fnmatchcase(tool, pattern):
                return lvl
    if tool in WRITES:
        target = args.get("file_path") or args.get("notebook_path") or args.get("path")
        if target and not inside(Path(target), workspace):
            return "ask"
    return "auto"


def inside(path: Path, workspace: Path) -> bool:
    try:
        path = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
        ws = workspace.resolve()
        return path == ws or ws in path.parents
    except OSError:
        return False
