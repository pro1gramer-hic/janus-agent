"""The only commands the agent runs: the project's tests, from an allowlist.

The model never gets a shell. The scanners run in-process. The test process gets the
environment without provider keys or anything that looks like a credential.
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass

ALLOWED_PREFIXES = [
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("pytest",),
]
SENSITIVE_ENV = re.compile(r"API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|^JANUS_|^ANTHROPIC_|^OPENAI_|^GROQ_",
                           re.I)


class CommandRefused(Exception):
    pass


@dataclass
class CommandResult:
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def check_command(command: list) -> list:
    if not command or not all(isinstance(part, str) for part in command):
        raise CommandRefused("the test command must be a list of strings")
    for prefix in ALLOWED_PREFIXES:
        if tuple(command[:len(prefix)]) == prefix:
            break
    else:
        raise CommandRefused(f"command not in the allowlist: {' '.join(command)}")
    if any(re.search(r"[;&|`$<>]", part) for part in command):
        raise CommandRefused("shell characters are not allowed in the test command")
    if command[0] in ("python", "python3"):
        return [sys.executable, *command[1:]]
    return command


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not SENSITIVE_ENV.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_tests(command: list, cwd, timeout: float) -> CommandResult:
    argv = check_command(command)
    try:
        proc = subprocess.run(argv, cwd=cwd, env=clean_env(), capture_output=True, text=True,
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return CommandResult(124, f"tests timed out after {timeout:.0f}s")
    except FileNotFoundError as exc:
        return CommandResult(127, f"cannot run tests: {exc}")
    return CommandResult(proc.returncode, (proc.stdout + proc.stderr)[-20000:])
