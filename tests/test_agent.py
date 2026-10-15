import json, sys
from pathlib import Path
import anthropic, httpx2, pytest
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent


def text(t):
    return BetaTextBlock.model_construct(type="text", text=t)

def tool(id, name, **inp):
    return BetaToolUseBlock.model_construct(type="tool_use", id=id, name=name, input=inp)

def msg(blocks, stop_reason):
    return BetaMessage.model_construct(id="m", type="message", role="assistant", model="fake",
                                       content=blocks, stop_reason=stop_reason, stop_details=None)

class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = self
        self.messages = self
    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "WORKDIR", tmp_path / "sb")
    agent.WORKDIR.mkdir()
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path / "logs")
    return tmp_path

def run(script, env, user="go", messages=None):
    client, messages = FakeClient(script), messages if messages is not None else []
    agent.add_user_text(messages, user)
    log = agent.new_log_path()
    agent.run_turn(client, messages, log)
    return client, messages, log

def check_pairing(messages):
    roles = [m["role"] for m in messages]
    assert all(a != b for a, b in zip(roles, roles[1:]))
    for i, m in enumerate(messages):
        if m["role"] == "assistant":
            ids = [b.id for b in m["content"] if b.type == "tool_use"]
            if ids:
                got = [b["tool_use_id"] for b in messages[i + 1]["content"] if b["type"] == "tool_result"]
                assert got == ids

def test_hello(env):
    script = [msg([tool("a", "write_file", path="hello.py", content="print('hello')")], "tool_use"),
              msg([tool("b", "bash", command="python3 hello.py")], "tool_use"),
              msg([text("done")], "end_turn")]
    _, messages, _ = run(script, env)
    assert (agent.WORKDIR / "hello.py").exists()
    assert "hello" in messages[4]["content"][0]["content"]
    assert len(messages) == 6
    check_pairing(messages)

def test_two_tool_uses_one_user_message(env):
    script = [msg([tool("a", "write_file", path="x.txt", content="1"), tool("b", "bash", command="ls")], "tool_use"),
              msg([text("ok")], "end_turn")]
    _, messages, _ = run(script, env)
    assert [b["tool_use_id"] for b in messages[2]["content"]] == ["a", "b"]
    check_pairing(messages)

def test_errors(env):
    assert agent.execute_tool("nope", {})["is_error"] is True
    assert agent.execute_tool("read_file", {"path": "missing.txt"})["is_error"] is True
    assert agent.execute_tool("read_file", {"wrong": 1})["is_error"] is True
    r = agent.execute_tool("bash", {"command": "exit 3"})
    assert "is_error" not in r and "exit code: 3" in r["content"]
    assert agent.execute_tool("bash", {"command": "sleep 2", "timeout": 1})["is_error"] is True

def test_truncation(env):
    r = agent.execute_tool("bash", {"command": "python3 -c \"print('x'*50000)\""})
    assert "截斷了" in r["content"] and len(r["content"]) < 11000

def test_max_turns_then_repl_append(env, monkeypatch):
    monkeypatch.setattr(agent, "MAX_TURNS", 2)
    loop = lambda i: msg([tool(f"t{i}", "bash", command="echo hi")], "tool_use")
    _, messages, _ = run([loop(1), loop(2)], env)
    assert messages[-1]["role"] == "user"
    n = len(messages)
    agent.add_user_text(messages, "繼續")
    assert len(messages) == n and messages[-1]["content"][-1] == {"type": "text", "text": "繼續"}
    check_pairing(messages)
    client, messages, _ = run([msg([text("好")], "end_turn")], env, user="再來", messages=messages)
    check_pairing(messages)

def test_log(env):
    script = [msg([tool("a", "bash", command="echo hi")], "tool_use"), msg([text("ok")], "end_turn")]
    _, _, log = run(script, env)
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    for i, l in enumerate(lines, 1):
        d = json.loads(l)
        assert d["turn"] == i and set(d["request"]) == {"model", "system", "tools", "messages"} and "response" in d

def test_request_kwargs(env, monkeypatch):
    client, _, _ = run([msg([text("ok")], "end_turn")], env)
    k = client.calls[0]
    assert k["output_config"] == {"effort": agent.EFFORT} and k["fallbacks"] == "default"
    assert "thinking" not in k and "temperature" not in k and "tool_choice" not in k
    monkeypatch.setattr(agent, "USE_FALLBACKS", False)
    client, _, _ = run([msg([text("ok")], "end_turn")], env)
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]

@pytest.mark.parametrize("reason", ["max_tokens", "refusal", "stop_sequence"])
def test_tool_use_with_abnormal_stop_is_closed(env, reason):
    _, messages, _ = run([msg([tool("a", "bash", command="ls")], reason)], env)
    agent.add_user_text(messages, "再來")
    assert messages[-1]["content"][-1] == {"type": "text", "text": "再來"}
    assert messages[-1]["content"][0]["is_error"] is True
    check_pairing(messages)

def test_empty_assistant_removed(env):
    _, messages, _ = run([msg([], "end_turn")], env)
    assert [m["role"] for m in messages] == ["user"]
    agent.add_user_text(messages, "再來")
    check_pairing(messages)

def test_api_error_keeps_history_valid(env):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "http://x"))
    _, messages, _ = run([msg([tool("a", "bash", command="ls")], "tool_use"), err], env)
    assert messages[-1]["role"] == "user"
    agent.add_user_text(messages, "重試")
    check_pairing(messages)

def test_keyboard_interrupt_in_tool_keeps_history_valid(env, monkeypatch):
    def boom(name, args):
        raise KeyboardInterrupt
    monkeypatch.setattr(agent, "execute_tool", boom)
    messages = []
    with pytest.raises(KeyboardInterrupt):
        run([msg([tool("a", "bash", command="ls")], "tool_use")], env, messages=messages)
    agent.add_user_text(messages, "再來")
    check_pairing(messages)
