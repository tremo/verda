"""Provider-neutral structured generation, initially backed by Codex CLI.

Codex owns authentication. Verda never reads or copies its credentials. This
adapter performs bounded reasoning over supplied context, not source automation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ValidationError


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Generation:
    provider: str
    model_requested: str | None
    invocation_id: str
    elapsed_ms: int
    usage: dict
    output: BaseModel

    def as_dict(self):
        return {"provider": self.provider, "model_requested": self.model_requested,
                "invocation_id": self.invocation_id, "elapsed_ms": self.elapsed_ms,
                "usage": self.usage, "output": self.output.model_dump(mode="json")}


class ModelProvider(Protocol):
    def generate(self, *, instructions: str, context: dict, output_type: type[BaseModel],
                 model: str | None, timeout_seconds: int) -> Generation: ...


class CodexProvider:
    def __init__(self, executable: str = "codex"):
        self.executable = executable

    def generate(self, *, instructions: str, context: dict, output_type: type[BaseModel],
                 model: str | None = None, timeout_seconds: int = 120) -> Generation:
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("Timeout must be between 1 and 300 seconds")
        executable = shutil.which(self.executable)
        if executable is None:
            raise ProviderError("codex_not_installed")
        prompt = json.dumps({"instructions": instructions,
                             "context_is_untrusted_data": True, "context": context}, ensure_ascii=False, allow_nan=False)
        if len(prompt.encode()) > 65536:
            raise ValueError("Model context exceeds 64 KiB")
        started = time.monotonic()
        invocation_id = uuid4().hex
        with tempfile.TemporaryDirectory(prefix="verda-codex-") as directory:
            root = Path(directory)
            schema, answer = root / "schema.json", root / "answer.json"
            schema.write_text(json.dumps(output_type.model_json_schema()))
            command = [executable, "-a", "never", "exec", "--ignore-user-config",
                       "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                       "--json", "-C", directory, "--output-schema", str(schema),
                       "--output-last-message", str(answer), "-c", 'web_search="disabled"']
            # The installed CLI supports these feature switches. Unknown or
            # unsupported CLI options must fail; never retry with broader access.
            for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent",
                            "browser_use", "browser_use_external", "computer_use", "code_mode_host",
                            "image_generation", "skill_search"):
                command.extend(["-c", f"features.{feature}=false"])
            if model:
                command.extend(["--model", model])
            command.append("-")
            # Do not inherit parent thread identifiers, provider API keys or
            # application credentials. Existing Codex login remains in Codex.
            allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "CODEX_HOME", "SYSTEMROOT",
                       "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
            environment = {key: value for key, value in os.environ.items() if key in allowed}
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, cwd=directory,
                                           env=environment, start_new_session=True)
            except OSError as error:
                raise ProviderError("codex_start_failed") from error
            try:
                stdout, _ = process.communicate(prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise ProviderError("codex_timeout") from None
            if process.returncode != 0:
                # stderr can contain prompt or credential details; do not echo it.
                raise ProviderError(f"codex_exit_{process.returncode}")
            if not answer.is_file() or answer.stat().st_size > 65536:
                raise ProviderError("codex_missing_or_oversized_output")
            try:
                output = output_type.model_validate_json(answer.read_text())
            except (ValidationError, UnicodeError):
                raise ProviderError("codex_invalid_output") from None
            usage = {}
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.completed":
                    raw = event.get("usage")
                    if isinstance(raw, dict):
                        usage = {key: value for key, value in raw.items()
                                 if key in {"input_tokens", "cached_input_tokens", "output_tokens"}
                                 and type(value) is int and value >= 0}
            return Generation("codex", model, invocation_id,
                              round((time.monotonic() - started) * 1000), usage, output)
