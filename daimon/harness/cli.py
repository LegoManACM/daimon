"""Any agent with a headless command line (Codex, Gemini CLI, Cursor, Aider, `claude -p`, your own script).

It can't be lent our tools or asked for consent mid-run, so it asks questions and leaves project notes with
QUESTION:/NOTES: lines (see run.py), and its own flags decide what it may do. Options:

  command:   argv for a fresh run, e.g. [codex, exec, --json, "{prompt}"]
  resume:    argv to continue a session, e.g. [codex, exec, resume, "{session}", --json, "{prompt}"]
             (omit if the agent can't resume: every run then starts fresh from the project notes)
  stdin:     true to send the prompt on stdin instead of as {prompt}
  output:    text (default) | json | jsonl
  result:    key path to the final message in json/jsonl output, e.g. result or item.text
  session:   key path to the session id in json/jsonl output, or session_regex: a regex on the output
  cost:      key path to a USD cost, if it reports one
  env:       extra environment; hide_env: variable names to remove (e.g. [OPENAI_API_KEY] to use a subscription)
  timeout_minutes: default 120
Placeholders: {prompt} (instructions + message), {session}, {workspace}, {model}.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from typing import Any

from . import Driver, Job, Outcome

RATE_LIMIT = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b", re.I)
RATE_LIMIT_FALLBACK_SECONDS = 15 * 60


def dig(data: Any, path: str | None) -> Any:
    if not path:
        return None
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


class Cli(Driver):
    def argv(self, job: Job, prompt: str) -> list[str]:
        template = self.options.get("resume") if job.session else self.options.get("command")
        if not template:
            raise ValueError(f"harness {self.name} has no `command` in config.yaml")
        values = {"prompt": prompt, "session": job.session or "", "workspace": str(job.workspace),
                  "model": job.model or self.options.get("model") or ""}
        args = [str(a).format(**values) for a in template]
        return [shutil.which(args[0]) or args[0], *args[1:]]

    def parse(self, out: str) -> tuple[str, str | None, float]:
        o, kind = self.options, self.options.get("output", "text")
        text, session, cost = out.strip(), None, 0.0
        if kind in ("json", "jsonl"):
            rows = [out] if kind == "json" else [ln for ln in out.splitlines() if ln.strip().startswith("{")]
            text = ""
            for row in rows:
                try:
                    data = json.loads(row)
                except json.JSONDecodeError:
                    continue
                text = str(dig(data, o.get("result")) or text)
                session = dig(data, o.get("session")) or session
                cost = float(dig(data, o.get("cost")) or cost)
        if o.get("session_regex"):
            m = re.search(o["session_regex"], out)
            session = m.group(1) if m and m.groups() else (m.group(0) if m else session)
        return text, (str(session) if session else None), cost

    async def run(self, job: Job) -> Outcome:
        # A resumed session already has the instructions; a fresh one gets them before the message.
        prompt = job.prompt if job.session else f"{job.system}\n\n---\n\n{job.prompt}"
        env = {**os.environ, **{k: str(v) for k, v in (self.options.get("env") or {}).items()}}
        for name in self.options.get("hide_env") or []:
            env.pop(name, None)
        try:
            argv = self.argv(job, prompt)
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(job.workspace), env=env,
                stdin=asyncio.subprocess.PIPE if self.options.get("stdin") else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError) as e:
            return Outcome("", error=f"couldn't start {self.name}: {e}", started=False)
        job.event(f"{self.name} started")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode() if self.options.get("stdin") else None),
                float(self.options.get("timeout_minutes", 120)) * 60)
        except asyncio.TimeoutError:
            proc.kill()
            return Outcome("", job.session, error="timed out")
        except asyncio.CancelledError:
            proc.kill()
            raise
        out_text, err_text = out.decode(errors="replace"), err.decode(errors="replace")
        text, session, cost = self.parse(out_text)
        session = session or job.session
        if proc.returncode != 0:
            detail = (err_text.strip() or out_text.strip())[-1500:]
            retry_at = time.time() + RATE_LIMIT_FALLBACK_SECONDS if RATE_LIMIT.search(detail) else None
            return Outcome(text, session, cost, error=f"exit {proc.returncode}: {detail}", retry_at=retry_at,
                           started=bool(text))
        return Outcome(text, session, cost)
