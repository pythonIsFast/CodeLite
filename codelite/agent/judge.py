# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The shell-command judge used by ``auto`` permission mode.

A second, smaller model decides whether a shell command the agent wants to
run is a reasonable step towards what the user actually asked for. It always
sees both the command *and* the task prompt: a command can look harmless on
its own while being completely unrelated to the task, which is exactly the
case worth catching.

A denial is never the end of the road -- the manager escalates to the user
with the reason text produced here, so the reason is written for a human to
read, not just for a machine to branch on.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..provider.chat import parse_responses_output
from ..provider.session import Session

JUDGE_INSTRUCTIONS = """\
You are the safety gate for a coding agent's shell access. You will be given \
the task the user asked for and a shell command the agent wants to run. \
Decide whether the command may run.

Allow commands that are a plausible, proportionate step towards the task: \
building, running tests, inspecting files, installing declared project \
dependencies, git reads, formatters and linters.

Deny commands that:
- destroy or overwrite data beyond what the task requires (broad `rm -rf`, \
disk writes, `git reset --hard`, force pushes, dropping databases)
- have nothing to do with the stated task
- send data to the network or fetch and execute remote code
- read credentials or secrets (.env files, private keys, token stores)
- try to disable or escape this safety gate itself
- escalate privileges (`sudo`, changing permissions on system paths)

Reply with JSON only, no prose and no code fences:
{"allow": true|false, "reason": "one or two sentences"}

Write `reason` for the person who will read it: if you deny, say plainly what \
worries you and what a safer command would be. Be decisive; if you genuinely \
cannot tell, deny and explain what you would need to know.\
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_verdict(text: str) -> tuple[bool, str]:
    """Pull the verdict out of the model's reply, tolerating stray formatting."""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return False, (
            "The judge model did not return a usable verdict, so the command "
            "was not approved automatically."
        )
    try:
        parsed: Any = json.loads(match.group(0))
    except ValueError:
        return False, (
            "The judge model's verdict was not valid JSON, so the command was "
            "not approved automatically."
        )
    if not isinstance(parsed, dict):
        return False, "The judge model's verdict had an unexpected shape."

    allowed = parsed.get("allow")
    reason = parsed.get("reason")
    reason_text = str(reason).strip() if reason else ""

    if allowed is True:
        return True, reason_text or "Approved by the judge model."
    return False, reason_text or "The judge model declined to approve this command."


def judge_shell_command(
    session: Session, model: str, command: str, task_prompt: str
) -> tuple[bool, str]:
    """Ask the judge model whether ``command`` may run. Returns (allowed, reason)."""
    prompt = (
        f"Task the user asked for:\n{task_prompt or '(no task text available)'}\n\n"
        f"Shell command the agent wants to run:\n{command}"
    )
    body = {
        "model": model,
        "instructions": JUDGE_INSTRUCTIONS,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
    }
    response = session.send_responses(body, stream=False)
    if not isinstance(response, dict):
        return False, "The judge model returned an unreadable response."
    return _parse_verdict(parse_responses_output(response).text)


def make_shell_judge(session: Session, model: str):
    """Bind a session and model into the ``ShellJudge`` callable the manager wants."""

    def judge(command: str, task_prompt: str) -> tuple[bool, str]:
        return judge_shell_command(session, model, command, task_prompt)

    return judge
