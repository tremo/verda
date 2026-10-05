import json
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from verda.providers import CodexProvider, Generation, ProviderError
from verda.runtime import AgentModel, ManagerAdvice, Runtime, RuntimeConfig, manager_preview


class FakeProvider:
    def __init__(self, steps=None):
        self.calls = []
        self.steps = steps or ["lookup"]

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return Generation("test", kwargs["model"], "synthetic-call", 1, {},
                          ManagerAdvice(explanation="Örnek öneri", next_steps=self.steps, questions_for_user=[]))


def test_each_agent_uses_its_selected_provider_and_model():
    codex, other = FakeProvider(), FakeProvider()
    config = RuntimeConfig(default=AgentModel(model="configured-default"),
                           agents={"assessment": AgentModel(provider="other", model="another-model")})
    runtime = Runtime(config, providers={"codex": codex, "other": other})
    manager_preview(runtime, {}, ["lookup"])
    runtime.execute("assessment", "explain", {}, instructions="Explain", output_type=ManagerAdvice)
    assert len(codex.calls) == len(other.calls) == 1
    assert codex.calls[0]["model"] == "configured-default"
    assert other.calls[0]["model"] == "another-model"


def test_provider_configuration_never_changes_rule_evaluation_into_model_call():
    runtime = Runtime(RuntimeConfig(default=AgentModel(provider="unregistered")), providers={})
    result = runtime.execute("assessment", "assess", {})
    assert result["executor"] == "deterministic" and result["provider"] is None
    assert result["output"]["state"] == "research_required"


def test_connector_can_execute_without_models_and_missing_connector_does_not_fallback():
    provider = FakeProvider()
    runtime = Runtime(providers={"codex": provider},
                      handlers={("geography", "route"): lambda facts: {"minutes": facts["minutes"]}})
    result = runtime.execute("geography", "route", {"minutes": 40})
    assert result["output"]["minutes"] == 40 and not provider.calls
    with pytest.raises(ProviderError, match="connector_not_implemented"):
        runtime.execute("geography", "elevation", {})
    assert not provider.calls


def test_missing_provider_never_silently_uses_codex():
    provider = FakeProvider()
    runtime = Runtime(RuntimeConfig(agents={"manager": AgentModel(provider="missing")}), providers={"codex": provider})
    with pytest.raises(ProviderError, match="provider_not_registered"):
        manager_preview(runtime, {}, ["lookup"])
    assert not provider.calls


def test_manager_cannot_propose_out_of_scope_actions():
    runtime = Runtime(providers={"codex": FakeProvider(["send_message"])})
    with pytest.raises(ProviderError, match="unavailable_action"):
        manager_preview(runtime, {}, ["lookup"])


def test_invalid_actions_and_schema_less_model_calls_fail():
    runtime = Runtime(providers={})
    with pytest.raises(ValueError, match="capabilities"):
        runtime.execute("manager", "send_message", {})
    with pytest.raises(ValueError, match="schema"):
        runtime.execute("manager", "propose_plan", {})


def test_toml_profiles_inherit_default_fields(tmp_path):
    path = tmp_path / "runtime.toml"
    path.write_text('[default]\nprovider="codex"\nmodel="chosen"\ntimeout_seconds=45\n[agents.manager]\ntimeout_seconds=60\n')
    config = RuntimeConfig.load(path)
    assert config.for_agent("manager").model == "chosen"
    assert config.for_agent("manager").timeout_seconds == 60
    assert config.for_agent("assessment").timeout_seconds == 45


def test_invalid_config_does_not_silently_revert_to_defaults():
    with pytest.raises(ValidationError):
        RuntimeConfig(agents={"typo": AgentModel()})
    with pytest.raises(ValidationError):
        AgentModel(timeout_seconds=0)


@pytest.fixture
def process_mock(monkeypatch):
    processes = []
    class Process:
        returncode = 0
        pid = 424242
        payload = '{"explanation":"sentetik", "next_steps":[], "questions_for_user":[]}'
        def __init__(self, command, **kwargs):
            self.command, self.kwargs = command, kwargs
            processes.append(self)
        def communicate(self, prompt, timeout):
            self.prompt = prompt
            Path(self.command[self.command.index("--output-last-message") + 1]).write_text(self.payload)
            return json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}), "sensitive stderr"
    monkeypatch.setattr("verda.providers.shutil.which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr("verda.providers.subprocess.Popen", Process)
    return Process, processes


def test_codex_uses_stdin_schema_existing_auth_and_constrained_process(process_mock, monkeypatch):
    _, processes = process_mock
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-inherit")
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-parent-thread")
    result = CodexProvider().generate(instructions="Test", context={"demo": True}, output_type=ManagerAdvice)
    process = processes[0]
    assert process.command[-1] == "-" and '"demo": true' in process.prompt
    assert "--ignore-user-config" in process.command and "--ephemeral" in process.command
    assert "read-only" in process.command and "features.shell_tool=false" in process.command
    assert "--dangerously-bypass-approvals-and-sandbox" not in process.command
    assert "--model" not in process.command
    assert "OPENAI_API_KEY" not in process.kwargs["env"] and "CODEX_THREAD_ID" not in process.kwargs["env"]
    assert not Path(process.kwargs["cwd"]).exists()  # temporary prompt/schema/output removed
    assert result.usage["input_tokens"] == 5


def test_codex_passes_explicit_model_without_substitution(process_mock):
    _, processes = process_mock
    CodexProvider().generate(instructions="Test", context={}, output_type=ManagerAdvice, model="chosen-model")
    command = processes[0].command
    assert command[command.index("--model") + 1] == "chosen-model"


def test_codex_error_does_not_expose_raw_stderr(process_mock):
    process_mock[0].returncode = 1
    with pytest.raises(ProviderError, match="^codex_exit_1$"):
        CodexProvider().generate(instructions="Test", context={}, output_type=ManagerAdvice)


def test_codex_rejects_invalid_result(process_mock):
    process_mock[0].payload = '{"explanation":"missing required fields"}'
    with pytest.raises(ProviderError, match="invalid_output"):
        CodexProvider().generate(instructions="Test", context={}, output_type=ManagerAdvice)


def test_codex_timeout_terminates_child_group(process_mock, monkeypatch):
    killed = []
    def communicate(self, prompt=None, timeout=None):
        if timeout:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return "", ""
    monkeypatch.setattr(process_mock[0], "communicate", communicate)
    monkeypatch.setattr("verda.providers.os.killpg", lambda pid, sig: killed.append(pid))
    with pytest.raises(ProviderError, match="codex_timeout"):
        CodexProvider().generate(instructions="Test", context={}, output_type=ManagerAdvice, timeout_seconds=1)
    assert killed == [424242]
