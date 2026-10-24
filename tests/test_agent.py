import os
from types import SimpleNamespace
import json, sys
from pathlib import Path
import anthropic, httpx2, pytest
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock, BetaUsage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent


def text(t):
    return BetaTextBlock.model_construct(type="text", text=t)

def tool(id, name, **inp):
    return BetaToolUseBlock.model_construct(type="tool_use", id=id, name=name, input=inp)

def msg(blocks, stop_reason, usage=None):
    extra = {"usage": BetaUsage.model_construct(input_tokens=usage[0], output_tokens=usage[1])} if usage else {}
    return BetaMessage.model_construct(id="m", type="message", role="assistant", model="fake",
                                       content=blocks, stop_reason=stop_reason, stop_details=None, **extra)

class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = self
        self.messages = self
    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})   # 拷貝：loop 之後還會往原 list 加東西
        r = self.script.pop(0)
        if isinstance(r, BaseException):   # 含 KeyboardInterrupt
            raise r
        return r

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "WORKDIR", tmp_path / "sb")
    agent.WORKDIR.mkdir()
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(agent, "PERMISSION_MODE", "allow_all")   # v1 的測試不關心權限；v2 的測試用 ask_env 改回 ask
    monkeypatch.setattr(agent, "SESSION_ALLOW", set())
    monkeypatch.setattr(agent, "LAST_USAGE", None)
    monkeypatch.setattr(agent, "COMPACT_COOLDOWN", 0)
    monkeypatch.setattr(agent, "SESSION_SYSTEM", agent.SYSTEM_PROMPT)   # main 會改它；測完還原，避免測試間污染
    monkeypatch.setattr(agent, "SESSION_MEMORY", None)
    monkeypatch.setattr(agent, "TODOS", [])
    monkeypatch.setattr(agent, "SUB_COUNT", 0)
    monkeypatch.setattr(agent, "HOOKS", {e: [] for e in agent.HOOK_EVENTS})   # hooks 測試自己用 set_hooks 設定
    monkeypatch.setattr(agent, "HOOKS_FILE", tmp_path / "no-hooks.json")   # 不要讀到使用者真實的 .agent/hooks.json
    monkeypatch.setattr(agent, "COMPACT_AT_TOKENS", 20000)   # 預設值是給真實測驗用的 8000；單元測試用固定值
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
    lines = [l for l in log.read_text().splitlines() if "request" in l]   # permission、tool_result 行另外測
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


# ==== v1：edit / read_file / grep / glob / fixture / summarize_log ====
def call(name, **args):
    return agent.execute_tool(name, args)

def put(rel, content):
    p = agent.WORKDIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)

def test_edit_unique_match(env):
    put("a.py", "x = 1\ny = 2\nz = 3\n")
    r = call("edit", path="a.py", old_string="y = 2", new_string="y = 20")
    assert "is_error" not in r and "已替換 1 處" in r["content"]
    assert "     2\ty = 20" in r["content"] and "     1\tx = 1" in r["content"]
    assert (agent.WORKDIR / "a.py").read_text() == "x = 1\ny = 20\nz = 3\n"

def test_edit_no_match(env):
    put("a.py", "x = 1\n")
    r = call("edit", path="a.py", old_string="nope", new_string="y")
    assert r["is_error"] and "找不到 old_string" in r["content"]
    assert call("edit", path="a.py", old_string="", new_string="y")["is_error"]

def test_edit_multiple_matches(env):
    put("a.py", "a = 1\na = 1\n")
    r = call("edit", path="a.py", old_string="a = 1", new_string="a = 2")
    assert r["is_error"] and "出現 2 次" in r["content"]
    assert (agent.WORKDIR / "a.py").read_text() == "a = 1\na = 1\n"   # 失敗不得改檔

def test_edit_replace_all(env):
    put("a.py", "a = 1\na = 1\n")
    r = call("edit", path="a.py", old_string="a = 1", new_string="a = 2", replace_all=True)
    assert "已替換 2 處" in r["content"]
    assert (agent.WORKDIR / "a.py").read_text() == "a = 2\na = 2\n"

def test_edit_same_string(env):
    put("a.py", "a = 1\n")
    assert call("edit", path="a.py", old_string="a", new_string="a")["is_error"]

def test_read_file_line_numbers(env):
    put("a.txt", "one\ntwo\nthree\n")
    assert call("read_file", path="a.txt")["content"] == "     1\tone\n     2\ttwo\n     3\tthree"

def test_read_file_offset_limit_truncation(env):
    put("a.txt", "".join(f"L{i}\n" for i in range(1, 11)))
    c = call("read_file", path="a.txt", offset=3, limit=2)["content"]
    assert c.splitlines() == ["     3\tL3", "     4\tL4", "[... 讀到第 4 行，檔案共 10 行，用 offset=5 繼續讀 ...]"]
    assert "繼續讀" not in call("read_file", path="a.txt", offset=9)["content"]

def test_grep_format_and_glob_filter(env):
    put("pkg/a.py", "def foo():\n    pass\n")
    put("pkg/b.txt", "def foo in text\n")
    c = call("grep", pattern=r"def foo")["content"]
    assert c.splitlines() == ["pkg/a.py:1: def foo():", "pkg/b.txt:1: def foo in text"]
    assert call("grep", pattern="foo", glob="*.py")["content"] == "pkg/a.py:1: def foo():"
    assert call("grep", pattern="foo", path="pkg/b.txt")["content"] == "pkg/b.txt:1: def foo in text"

def test_grep_skips_dirs_and_binary(env):
    put(".git/x.py", "needle\n")
    put("node_modules/y.py", "needle\n")
    put("ok.py", "needle\n")
    (agent.WORKDIR / "bin.dat").write_bytes(b"\xff\xfe needle")
    assert call("grep", pattern="needle")["content"] == "ok.py:1: needle"

def test_grep_invalid_regex_and_no_match(env):
    put("a.py", "x\n")
    assert call("grep", pattern="(")["is_error"] is True
    r = call("grep", pattern="zzz")
    assert "is_error" not in r and "沒有符合" in r["content"]

def test_grep_result_cap(env):
    put("a.txt", "hit\n" * 250)
    c = call("grep", pattern="hit")["content"]
    assert "共 250 筆" in c and len(c.splitlines()) == 201

def test_glob_sorted_relative(env):
    put("b.py", ""); put("a/z.py", ""); put("a/y.txt", ""); put(".venv/v.py", "")
    assert call("glob", pattern="**/*.py")["content"].splitlines() == ["a/z.py", "b.py"]

def test_edit_preserves_crlf(env):
    (agent.WORKDIR / "w.txt").write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
    r = call("edit", path="w.txt", old_string="a = 1\nb = 2", new_string="a = 1\nb = 20")   # 模型給的是 \n
    assert "is_error" not in r
    assert (agent.WORKDIR / "w.txt").read_bytes() == b"a = 1\r\nb = 20\r\nc = 3\r\n"
    assert "\r" not in call("read_file", path="w.txt")["content"]

def test_read_file_big_file_cut_at_line_boundary(env):
    put("big.txt", "".join(f"line {i} " + "x" * 40 + "\n" for i in range(1, 1001)))
    c = call("read_file", path="big.txt")["content"]
    assert "截斷了" not in c and len(c) < agent.MAX_TOOL_OUTPUT_CHARS
    body, footer = c.rsplit("\n", 1)
    last = int(body.splitlines()[-1].split("\t")[0])
    assert body.endswith("x" * 40)                       # 停在整行結尾
    assert footer == f"[... 讀到第 {last} 行，檔案共 1000 行，用 offset={last + 1} 繼續讀 ...]"
    nxt = call("read_file", path="big.txt", offset=last + 1)["content"]
    assert nxt.startswith(f"{last + 1:>6}\tline {last + 1} ")

def test_read_file_edge_cases(env):
    put("empty.txt", "")
    assert call("read_file", path="empty.txt")["content"] == "(空檔案)"
    put("a.txt", "1\n2\n")
    r = call("read_file", path="a.txt", offset=3)
    assert r["is_error"] and "offset 超出檔尾，檔案共 2 行" in r["content"]
    assert call("read_file", path="a.txt", limit=0)["is_error"]
    assert "is_error" not in call("read_file", path="a.txt", offset=2)
    (agent.WORKDIR / "b.bin").write_bytes(b"\xff\xfe\x00")
    for r in (call("read_file", path="b.bin"), call("edit", path="b.bin", old_string="a", new_string="b")):
        assert r["is_error"] and "不是 UTF-8 文字檔" in r["content"]

def test_grep_missing_path_and_recursive_glob(env):
    put("pkg/sub/a.py", "needle\n"); put("top.py", "needle\n"); put("pkg/n.txt", "needle\n")
    r = call("grep", pattern="x", path="nope")
    assert r["is_error"] and "路徑不存在" in r["content"]
    both = ["pkg/sub/a.py:1: needle", "top.py:1: needle"]
    assert sorted(call("grep", pattern="needle", glob="**/*.py")["content"].splitlines()) == both
    assert sorted(call("grep", pattern="needle", glob="*.py")["content"].splitlines()) == both
    assert call("grep", pattern="needle", glob="pkg/*.txt")["content"] == "pkg/n.txt:1: needle"

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "mini_repo"

def copy_fixture(dst):
    import shutil
    shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns("__pycache__"))

def run_check(repo):
    import subprocess
    return subprocess.run(["python3", "check.py"], cwd=repo, capture_output=True, text=True,
                          env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"})

# 正確答案：bug 在 shop/pricing/discounts.py 的 bulk_discount，比較運算子 > 應為 >=（其他模組的類似比較是對的）
FIX = dict(path="mini_repo/shop/pricing/discounts.py",
           old_string="if amount > BULK_DISCOUNT_THRESHOLD:", new_string="if amount >= BULK_DISCOUNT_THRESHOLD:")

def test_fixture_fails_on_exactly_one_behavior(env):
    copy_fixture(agent.WORKDIR / "mini_repo")
    r = run_check(agent.WORKDIR / "mini_repo")
    fails = [l for l in r.stderr.splitlines() if l.startswith("FAIL")]
    assert r.returncode != 0 and len(fails) == 1 and "訂單 #A17 總額錯誤：預期 900，實際 1000" in fails[0]
    assert not any(w in r.stderr + r.stdout for w in ("剛好", "門檻", "邊界"))

def test_fixture_fix_with_edit_gives_all_ok(env):
    copy_fixture(agent.WORKDIR / "mini_repo")
    assert "is_error" not in call("edit", **FIX)
    r = run_check(agent.WORKDIR / "mini_repo")
    assert r.returncode == 0 and "ALL OK" in r.stdout

def test_summarize_log(env, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    script = [msg([tool("a", "read_file", path="h.py")], "tool_use"),
              msg([tool("b", "write_file", path="h.py", content="1"), tool("c", "grep", pattern="x")], "tool_use"),
              msg([text("ok")], "end_turn")]
    _, _, log = run(script, env)
    summarize_log.main([str(log)])
    out = capsys.readouterr().out
    assert "read_file" in out and "write_file" in out and "grep" in out and "h.py" in out


# ---- summarize_log 的完成標準判定：造 repo 與假 log ----
def judge_setup(tmp_path, fixed=True):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    repo, fx = tmp_path / "sandbox" / "mini_repo", tmp_path / "fixtures" / "mini_repo"
    import shutil
    shutil.copytree(FIXTURE, fx, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    if fixed:
        f = repo / "shop/pricing/discounts.py"
        f.write_text(f.read_text().replace(FIX["old_string"], FIX["new_string"]))
    return summarize_log, repo, fx

def fake_log(tmp_path, calls):
    """calls: [(name, input)]，每個呼叫一輪；結果全是成功。"""
    lines, results = [], []
    for i, (name, inp) in enumerate(calls, 1):
        lines.append({"turn": i, "request": {"messages": [{"role": "user", "content": results}] if results else []},
                      "response": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})
        results = results + [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]
    lines.append({"turn": len(calls) + 1, "request": {"messages": [{"role": "user", "content": results}]},
                  "response": {"stop_reason": "end_turn", "content": []}})
    p = tmp_path / "log.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines))
    return p

def verdicts(mod, log, repo, fx, capsys):
    mod.main([str(log), "--repo", str(repo), "--fixture", str(fx)])
    out = capsys.readouterr().out
    return {l.split()[2][0]: l.split()[1] for l in out.splitlines() if l.startswith(("✅", "❌"))}, out

GOOD = [("grep", {"pattern": "bulk"}),
        ("read_file", {"path": "mini_repo/shop/pricing/discounts.py"}),
        ("edit", FIX),
        ("bash", {"command": "cd mini_repo && python3 check.py"})]

def test_judge_all_pass(tmp_path, capsys):
    mod, repo, fx = judge_setup(tmp_path)
    v, out = verdicts(mod, fake_log(tmp_path, GOOD), repo, fx, capsys)
    assert v == {k: "PASS" for k in "123456"}, out

def test_judge_item5_write_file_overwrite(tmp_path, capsys):
    mod, repo, fx = judge_setup(tmp_path)
    calls = [("write_file", {"path": "./mini_repo/shop/pricing/discounts.py", "content": "x"})]
    v, out = verdicts(mod, fake_log(tmp_path, calls), repo, fx, capsys)
    assert v["5"] == "FAIL" and v["4"] == "FAIL"

def test_judge_item6_peeking(tmp_path, capsys):
    mod, repo, fx = judge_setup(tmp_path)
    calls = [("bash", {"command": "cat ../fixtures/mini_repo/shop/pricing/discounts.py"})] + GOOD
    v, out = verdicts(mod, fake_log(tmp_path, calls), repo, fx, capsys)
    assert v["6"] == "FAIL" and v["1"] == "PASS"
    assert "觀察" in out

def test_judge_unfixed_and_tampered(tmp_path, capsys):
    mod, repo, fx = judge_setup(tmp_path, fixed=False)
    (repo / "check.py").write_text("print('ALL OK')\n")
    v, _ = verdicts(mod, fake_log(tmp_path, GOOD[:2]), repo, fx, capsys)
    assert v["2"] == "FAIL" and v["3"] == "FAIL" and v["4"] == "FAIL"


# ---- 無憑證／API 錯誤／summarize_log 的防呆 ----
def test_main_without_key_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(agent, "load_env", lambda: None)   # 不讓真正的 .env 影響測試
    with pytest.raises(SystemExit) as e:
        agent.main()
    assert e.value.code == 1 and "找不到 API key" in capsys.readouterr().out

def test_auth_typeerror_is_caught(env, capsys):
    err = TypeError('"Could not resolve authentication method. Expected one of api_key..."')
    _, messages, log = run([err], env)
    assert "認證錯誤" in capsys.readouterr().out
    check_pairing(messages)
    assert messages[-1]["role"] == "user"

def test_other_typeerror_propagates(env):
    with pytest.raises(TypeError):
        run([TypeError("boom")], env)

def test_api_error_writes_error_log_line(env):
    e = anthropic.APIConnectionError(request=httpx2.Request("POST", "http://x"))
    _, _, log = run([e], env)
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["turn"] == 1 and "messages" in lines[0]["request"]
    assert lines[0]["error"]["type"] == "APIConnectionError" and "response" not in lines[0]

def test_no_log_file_before_first_write(env):
    agent.new_log_path()
    assert list(agent.LOG_DIR.glob("*.jsonl")) == []

def test_summarize_error_line(env, capsys):
    mod, repo, fx = judge_setup(env)
    p = fake_log(env, GOOD)
    err = {"turn": 9, "request": {"messages": []}, "error": {"type": "APIConnectionError", "message": "down"}}
    p.write_text(p.read_text() + "\n" + json.dumps(err))
    assert mod.main([str(p), "--repo", str(repo), "--fixture", str(fx)]) != 2
    assert "#9 ERROR APIConnectionError: down" in capsys.readouterr().out

def test_summarize_refuses_log_older_than_repo(env, capsys):
    import os
    mod, repo, fx = judge_setup(env)
    p = fake_log(env, GOOD)
    os.utime(p, (1000, 1000))
    assert mod.main([str(p), "--repo", str(repo), "--fixture", str(fx)]) == 2
    out = capsys.readouterr().out
    assert "比 repo 的複製時間還舊" in out and "PASS" not in out and "FAIL" not in out

def test_summarize_refuses_non_mini_repo_log(env, capsys):
    mod, repo, fx = judge_setup(env)
    p = fake_log(env, [("read_file", {"path": "h.py"})])
    assert mod.main([str(p), "--repo", str(repo), "--fixture", str(fx)]) == 2
    out = capsys.readouterr().out
    assert "不是 mini_repo 測驗" in out and "PASS" not in out and "FAIL" not in out


# ---- .env 載入與 key 不外洩給 bash ----
def test_load_env_parses_and_shell_wins(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# 註解\nFOO_A="x1"\nFOO_B=y2\n\n')
    monkeypatch.delenv("FOO_A", raising=False)
    monkeypatch.setenv("FOO_B", "shell")
    agent.load_env(f)
    assert os.environ["FOO_A"] == "x1" and os.environ["FOO_B"] == "shell"
    monkeypatch.delenv("FOO_A")

def test_bash_cannot_see_api_key(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    out = agent.bash("echo key=$ANTHROPIC_API_KEY")
    assert "sk-ant-secret" not in out and "key=\n" in out


# ==== v2：權限層 ====
@pytest.fixture
def ask_env(env, monkeypatch):
    monkeypatch.setattr(agent, "PERMISSION_MODE", "ask")
    def answers(*seq):
        asked, it = [], iter(seq)
        def prompt(q):
            asked.append(q)
            a = next(it)
            if isinstance(a, BaseException):
                raise a
            return a
        monkeypatch.setattr(agent, "PROMPT", prompt)
        return asked
    return answers

@pytest.mark.parametrize("name,args,want", [
    ("read_file", {"path": "a.py"}, "allow"),
    ("grep", {"pattern": "x"}, "allow"),
    ("glob", {"pattern": "**/*.py"}, "allow"),
    ("read_file", {"path": "../x"}, "deny"),
    ("read_file", {"path": "/etc/passwd"}, "deny"),
    ("grep", {"pattern": "x", "path": "sub/../.."}, "deny"),
    ("glob", {"pattern": "../*"}, "deny"),
    ("glob", {"pattern": "/etc/*"}, "deny"),
    ("write_file", {"path": "../x", "content": "1"}, "deny"),
    ("write_file", {"path": "a.py", "content": "1"}, "ask"),
    ("edit", {"path": "a.py", "old_string": "a", "new_string": "b"}, "ask"),
    ("bash", {"command": "ls"}, "allow"),
    ("bash", {"command": "pwd"}, "allow"),
    ("bash", {"command": "cat mini_repo/check.py"}, "allow"),
    ("bash", {"command": "grep -rn total mini_repo"}, "allow"),
    ("bash", {"command": "cd mini_repo && ls"}, "allow"),
    ("bash", {"command": "cd mini_repo && python3 check.py"}, "ask"),
    ("bash", {"command": "ls | wc -l"}, "allow"),
    ("bash", {"command": "rm -rf x"}, "ask"),
    ("bash", {"command": "echo hi > f"}, "ask"),
    ("bash", {"command": "echo $(whoami)"}, "ask"),
    ("bash", {"command": "ls && curl x"}, "ask"),
    ("bash", {"command": "python3 -c 'x'"}, "ask"),
    ("bash", {"command": "find . -delete"}, "ask"),
    ("bash", {"command": "find . -exec rm {} ;"}, "ask"),
    ("bash", {"command": "curl x"}, "ask"),
])
def test_check_permission_rules(ask_env, name, args, want):
    decision, reason = agent.check_permission(name, args)
    assert decision == want and reason
    if want == "deny":
        assert "路徑在工作目錄之外" in reason

# 紅方實測的繞過：全部必須不是 allow
BYPASSES = [
    "cat ../.env", "head /etc/passwd", "grep -r API ..", "diff ../.env x", "cd .. && cat .env",
    "cd ~; cat .env", "cat $HOME/.env", "cat ../.e*", "cat .*/.env", "ls -R /",   # 1 讀 WORKDIR 外
    "ls\nrm -rf x", "ls & rm x",                                                   # 2 分隔符
    "env rm -rf x",                                                                 # 3
    "find . -fprint ../x", "find . -fls ../x", "find . -fprintf ../x %p",           # 4
    "python3 evil.py", "python3 -m pip install x", "python3 -Sc 1", "pytest -p evil",   # 5
    "ls\u00a0&& rm x", "ls\x0brm x", "ls\x0crm x", "cat\u00a0../.env", "ls\trm x",   # 6 空白字元不一致
    "cat --include=/etc/passwd x", "cd", "cd a b", "ls &&& rm x", "ls ||| rm x", "",
]

@pytest.mark.parametrize("cmd", BYPASSES)
def test_bash_bypasses_not_allowed(ask_env, cmd):
    assert agent.check_permission("bash", {"command": cmd})[0] in ("ask", "deny")

def test_grep_does_not_follow_symlinks(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("API_KEY=1\n")
    put("real.txt", "API_KEY=2\n")
    (agent.WORKDIR / "link_file").symlink_to(outside / "secret.txt")
    (agent.WORKDIR / "link_dir").symlink_to(outside, target_is_directory=True)
    assert call("grep", pattern="API_KEY")["content"] == "real.txt:1: API_KEY=2"

def test_glob_hides_symlink_targets_outside(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    put("real.txt", "x")
    (agent.WORKDIR / "link_file").symlink_to(outside / "secret.txt")
    (agent.WORKDIR / "link_dir").symlink_to(outside, target_is_directory=True)
    assert call("glob", pattern="**/*")["content"] == "real.txt"
    assert call("glob", pattern="link_dir/*")["content"] == "沒有符合的檔案"

def test_file_tools_deny_symlink_escape(ask_env, tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    (agent.WORKDIR / "link").symlink_to(tmp_path / "secret.txt")
    for name, args in (("read_file", {"path": "link"}), ("write_file", {"path": "link", "content": "1"}),
                       ("edit", {"path": "link", "old_string": "x", "new_string": "y"}), ("grep", {"pattern": "x", "path": "link"})):
        assert agent.check_permission(name, args)[0] == "deny"

def test_nul_in_args_is_denied_not_crash(ask_env):
    asked = ask_env()
    for name, args in (("read_file", {"path": "a\x00b"}), ("write_file", {"path": "a\x00b", "content": "1"}),
                       ("grep", {"pattern": "x", "path": "a\x00b"})):
        result, perm = agent.authorize(tool("n", name, **args))
        assert perm["decision"] == "deny" and "參數無法判斷" in result["content"] and result["is_error"]
    assert asked == []

def test_bash_always_allow_only_same_command(ask_env):
    asked = ask_env("a", "n", "")
    script = [msg([tool("a", "bash", command="touch x.txt")], "tool_use"),
              msg([tool("b", "bash", command="touch x.txt")], "tool_use"),
              msg([tool("c", "bash", command="rm -rf x.txt")], "tool_use"),
              msg([text("ok")], "end_turn")]
    run(script, None)
    assert len(asked) == 3 and "不再詢問這條完全相同的指令" in asked[0]   # 第一次 a、第三條 rm 問了 n 和理由
    assert agent.SESSION_ALLOW == {("bash", "touch x.txt")}
    assert agent.check_permission("bash", {"command": "touch y.txt"})[0] == "ask"

def test_file_tool_always_allow_prompt_text(ask_env):
    asked = ask_env("y")
    run([msg([tool("a", "write_file", path="1.txt", content="1")], "tool_use"), msg([text("ok")], "end_turn")], None)
    assert "都允許 這個工具（仍限工作目錄內）" in asked[0]

def test_permission_mode_is_normalized():
    import subprocess
    r = subprocess.run([sys.executable, "-c", "import agent; print(agent.PERMISSION_MODE)"], capture_output=True, text=True,
                       env={**os.environ, "PERMISSION_MODE": " Allow_All ", "WORKDIR": str(Path(agent.__file__).parent / "sandbox")},
                       cwd=Path(agent.__file__).parent)
    assert r.stdout.strip() == "allow_all"

def test_denied_then_other_tool(ask_env):
    put("a.py", "x = 1\n")
    asked = ask_env("n", "先別改")
    script = [msg([tool("a", "edit", path="a.py", old_string="x = 1", new_string="x = 2")], "tool_use"),
              msg([tool("b", "read_file", path="a.py")], "tool_use"),
              msg([text("好，先列計畫")], "end_turn")]
    _, messages, log = run(script, None)
    r1, r2 = messages[2]["content"][0], messages[4]["content"][0]
    assert r1["is_error"] is True and "先別改" in r1["content"] and "沒有被執行" in r1["content"]
    assert (agent.WORKDIR / "a.py").read_text() == "x = 1\n"
    assert "is_error" not in r2 and "x = 1" in r2["content"]
    check_pairing(messages)
    perms = [json.loads(l)["permission"] for l in log.read_text().splitlines() if "permission" in l]
    assert [(p["tool"], p["decision"], p["answer"]) for p in perms] == [("edit", "ask", "n"), ("read_file", "allow", None)]
    assert "先別改" in perms[0]["reason"] and len(asked) == 2

def test_yes_executes(ask_env):
    ask_env("Y")
    _, messages, _ = run([msg([tool("a", "write_file", path="n.txt", content="1")], "tool_use"),
                          msg([text("ok")], "end_turn")], None)
    assert (agent.WORKDIR / "n.txt").read_text() == "1" and "is_error" not in messages[2]["content"][0]

def test_invalid_answer_asks_again(ask_env):
    asked = ask_env("maybe", "", "y")
    run([msg([tool("a", "write_file", path="n.txt", content="1")], "tool_use"), msg([text("ok")], "end_turn")], None)
    assert len(asked) == 3 and (agent.WORKDIR / "n.txt").exists()

def test_always_allow_per_tool(ask_env):
    asked = ask_env("a")
    script = [msg([tool("a", "write_file", path="1.txt", content="1")], "tool_use"),
              msg([tool("b", "write_file", path="2.txt", content="2")], "tool_use"),
              msg([text("ok")], "end_turn")]
    run(script, None)
    assert len(asked) == 1 and (agent.WORKDIR / "2.txt").exists()
    assert agent.check_permission("edit", {"path": "1.txt", "old_string": "1", "new_string": "2"})[0] == "ask"   # 工具分開記
    assert agent.SESSION_ALLOW == {"write_file"}

def test_allow_all_still_denies_outside_paths(ask_env, monkeypatch):
    monkeypatch.setattr(agent, "PERMISSION_MODE", "allow_all")
    asked = ask_env()
    assert agent.check_permission("write_file", {"path": "a.txt", "content": "1"})[0] == "allow"
    assert agent.check_permission("bash", {"command": "rm -rf x"})[0] == "allow"
    assert agent.check_permission("write_file", {"path": "../x", "content": "1"})[0] == "deny"
    _, messages, _ = run([msg([tool("a", "write_file", path="../x", content="1")], "tool_use"),
                          msg([text("ok")], "end_turn")], None)
    assert "權限層拒絕：路徑在工作目錄之外" in messages[2]["content"][0]["content"] and asked == []

def test_eof_is_refusal(ask_env):
    ask_env(EOFError(), EOFError())
    _, messages, _ = run([msg([tool("a", "write_file", path="n.txt", content="1")], "tool_use"),
                          msg([text("ok")], "end_turn")], None)
    assert messages[2]["content"][0]["is_error"] is True and "未提供" in messages[2]["content"][0]["content"]
    assert not (agent.WORKDIR / "n.txt").exists()

def test_reset_does_not_clear_session_allow(ask_env):
    agent.SESSION_ALLOW.add(("bash", "rm x"))
    messages = [{"role": "user", "content": "x"}]
    messages.clear()   # /reset 的實作就是這行
    assert agent.check_permission("bash", {"command": "rm x"})[0] == "allow"


# ---- summarize_log 的 v2 判定 ----
def perm_log(tmp_path, steps):
    """steps: [(name, input, decision, answer, 該回應附帶的 text)]；每步一輪，最後一輪只有文字。"""
    rows = []
    for i, (name, inp, dec, ans, txt) in enumerate(steps, 1):
        content = ([{"type": "text", "text": txt}] if txt else []) + [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]
        rows.append({"turn": i, "request": {"messages": []}, "response": {"stop_reason": "tool_use", "content": content}})
        rows.append({"turn": i, "permission": {"id": f"t{i}", "tool": name, "input": inp, "decision": dec, "answer": ans, "reason": "r"}})
    rows.append({"turn": len(steps) + 1, "request": {"messages": []},
                 "response": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "我改列計畫給你"}]}})
    p = tmp_path / "plog.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p

def v2_verdicts(tmp_path, steps, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    summarize_log.main([str(perm_log(tmp_path, steps)), "--repo", str(tmp_path / "nope"), "--fixture", str(tmp_path / "nope2")])
    out = capsys.readouterr().out
    assert "v2 完成標準判定" in out
    block = out.split("v2 完成標準判定")[1].split("--- v1")[0]
    return {l.split()[2][0]: l.split()[1] for l in block.splitlines() if l.startswith(("✅", "❌"))}, out

EDIT = {"path": "mini_repo/x.py", "old_string": "a", "new_string": "b"}

def test_v2_judge_retry_fails(tmp_path, capsys):
    v, out = v2_verdicts(tmp_path, [("edit", EDIT, "ask", "n", ""), ("edit", EDIT, "ask", "n", "")], capsys)
    assert v["1"] == "PASS" and v["2"] == "FAIL" and "[ask→n edit mini_repo/x.py]" in out

def test_v2_judge_switching_tool_passes(tmp_path, capsys):
    v, out = v2_verdicts(tmp_path, [("edit", EDIT, "ask", "n", ""), ("read_file", {"path": "mini_repo/x.py"}, "allow", None, "")], capsys)
    assert v == {"1": "PASS", "2": "PASS", "3": "PASS", "4": "PASS", "5": "PASS"} and "我改列計畫給你" in out and "需人工確認" in out

def test_v2_judge_repeated_deny_over_once(tmp_path, capsys):
    out_of = {"path": "../x"}
    steps = [("read_file", out_of, "deny", None, ""), ("bash", {"command": "ls"}, "allow", None, ""),
             ("read_file", out_of, "deny", None, ""), ("bash", {"command": "pwd"}, "allow", None, ""),
             ("read_file", out_of, "deny", None, "")]
    v, _ = v2_verdicts(tmp_path, steps, capsys)
    assert v["4"] == "FAIL" and v["1"] == "FAIL"

def test_v2_judge_absent_in_v1_log(tmp_path, capsys):
    mod, repo, fx = judge_setup(tmp_path)
    mod.main([str(fake_log(tmp_path, GOOD)), "--repo", str(repo), "--fixture", str(fx)])
    assert "v2 完成標準判定" not in capsys.readouterr().out

# ---- v2：同批連帶略過（一則回應的多個 tool_use 是同一個計畫，拒絕一個，後面的都不做）----
def perms_of(log):
    return [json.loads(l)["permission"] for l in log.read_text().splitlines() if "permission" in l]

def test_rejection_skips_rest_of_batch(ask_env):
    put("a.py", "x = 1\n"); put("b.py", "y = 1\n")
    asked = ask_env("n", "x")
    script = [msg([tool("a", "edit", path="a.py", old_string="x = 1", new_string="x = 2"),
                   tool("b", "edit", path="b.py", old_string="y = 1", new_string="y = 2"),
                   tool("c", "bash", command="python3 b.py")], "tool_use"),
              msg([text("ok")], "end_turn")]
    _, messages, log = run(script, None)
    assert len(asked) == 2   # 只有第一個被問（答案＋理由）
    assert (agent.WORKDIR / "b.py").read_text() == "y = 1\n"
    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"] and len(messages) == 4
    assert all(r["is_error"] for r in results)
    assert all("同一批中前面的 edit" in r["content"] for r in results[1:])
    check_pairing(messages)
    perms = perms_of(log)
    assert [p["decision"] for p in perms] == ["ask", "skipped", "skipped"]
    assert all(p["answer"] is None and p["id"] in "bc" for p in perms[1:])

def test_executed_before_rejection_keeps_real_result(ask_env):
    put("a.py", "x = 1\n"); put("b.py", "y = 1\n")
    ask_env("n", "")
    script = [msg([tool("r", "read_file", path="a.py"),
                   tool("a", "edit", path="a.py", old_string="x = 1", new_string="x = 2"),
                   tool("b", "edit", path="b.py", old_string="y = 1", new_string="y = 2")], "tool_use"),
              msg([text("ok")], "end_turn")]
    _, messages, _ = run(script, None)
    r = messages[2]["content"]
    assert "is_error" not in r[0] and "x = 1" in r[0]["content"]
    assert r[2]["is_error"] and "同一批中前面的 edit" in r[2]["content"]

def test_deny_skips_rest_without_asking(ask_env):
    put("a.py", "x = 1\n")
    asked = ask_env()
    script = [msg([tool("g", "glob", pattern="../*"),
                   tool("a", "edit", path="a.py", old_string="x = 1", new_string="x = 2")], "tool_use"),
              msg([text("ok")], "end_turn")]
    _, messages, log = run(script, None)
    assert asked == [] and (agent.WORKDIR / "a.py").read_text() == "x = 1\n"
    assert "同一批中前面的 glob" in messages[2]["content"][1]["content"]
    assert [p["decision"] for p in perms_of(log)] == ["deny", "skipped"]

def test_no_rejection_runs_everything(ask_env):
    put("a.py", "x = 1\n"); put("b.py", "y = 1\n")
    asked = ask_env("y", "y")
    script = [msg([tool("a", "edit", path="a.py", old_string="x = 1", new_string="x = 2"),
                   tool("b", "edit", path="b.py", old_string="y = 1", new_string="y = 2")], "tool_use"),
              msg([text("ok")], "end_turn")]
    run(script, None)
    assert len(asked) == 2
    assert (agent.WORKDIR / "a.py").read_text() == "x = 2\n" and (agent.WORKDIR / "b.py").read_text() == "y = 2\n"

def test_summarize_item5_real_log_fails_and_skipped_log_passes(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    real = Path(__file__).resolve().parent.parent / "logs" / "v2-run2-reject.jsonl"
    item5 = lambda p: next(r for r in summarize_log.judge_v2(p) if r[1].startswith("5."))
    assert item5(real)[0] is False
    uses = [{"type": "tool_use", "id": i, "name": "edit", "input": {"path": f"{i}.py"}} for i in "abc"]
    rows = [{"turn": 1, "response": {"content": uses}}]
    rows += [{"turn": 1, "permission": {"id": i, "tool": "edit", "input": {}, "decision": d, "answer": a}}
             for i, d, a in [("a", "ask", "n"), ("b", "skipped", None), ("c", "skipped", None)]]
    fake = tmp_path / "skipped.jsonl"
    fake.write_text("\n".join(json.dumps(r) for r in rows))
    assert item5(fake)[0] is True


# ==== v3：上下文管理（compaction）====
def user_blocks(messages):
    return [b for m in messages if m["role"] == "user" for b in m["content"] if b["type"] == "text"]

def compaction_script(summary="摘要內容", usage=(30000, 100)):
    """第 1 輪回 tool_use 且 usage 超過門檻 → 第 2 輪前壓縮（摘要請求）→ 之後照常結束。"""
    return [msg([tool("a", "bash", command="echo 1")], "tool_use", usage=usage),
            msg([text(summary)], "end_turn"),
            msg([text("done")], "end_turn", usage=(500, 10))]

def test_compaction_replaces_history_in_place(env, monkeypatch):
    monkeypatch.setattr(agent, "COMPACT_AT_TOKENS", 20000)
    messages = []
    agent.add_user_text(messages, "請處理三個檔案")
    original = messages
    client = FakeClient(compaction_script())
    log = agent.new_log_path()
    agent.run_turn(client, messages, log)
    summary_call, after_call = client.calls[1], client.calls[2]
    assert summary_call["tool_choice"] == {"type": "none"} and summary_call["tools"] == agent.TOOLS
    assert summary_call["system"] == agent.SYSTEM_PROMPT and summary_call["tools"] == agent.TOOLS   # 綁定前綴不能變
    assert messages is original   # 原地替換，REPL 持有的 list 才會一起更新
    assert len(after_call["messages"]) == 1 and after_call["messages"][0]["role"] == "user"
    blocks = [b["text"] for b in after_call["messages"][0]["content"]]
    assert blocks[0].startswith(agent.HEADER_PREFIX) and blocks[1] == agent.USER_PREFIX + "請處理三個檔案"
    assert blocks[2].startswith(agent.SUMMARY_PREFIX + "摘要內容") and blocks[2].endswith("請接著完成任務。") and len(blocks) == 3
    assert "tool_choice" not in after_call   # 正常呼叫不禁止工具
    assert [m["role"] for m in messages] == ["user", "assistant"]
    check_pairing(messages)
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    c = next(r["compaction"] for r in rows if "compaction" in r)
    assert c["before_tokens"] == 30100 and c["messages_before"] == 3 and c["summary"] == "摘要內容"
    assert c["user_texts"] == ["請處理三個檔案"]

def test_summary_request_does_not_touch_original_messages(env, monkeypatch):
    messages = []
    agent.add_user_text(messages, "任務")
    messages += [{"role": "assistant", "content": [tool("a", "bash", command="ls")]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}]}]
    last_user = messages[-1]
    client = FakeClient([msg([text("S")], "end_turn")])
    assert agent.compact(client, messages, agent.new_log_path()) is True
    sent = client.calls[0]["messages"]
    assert sent[-1]["content"][-1]["text"] == agent.SUMMARY_REQUEST and len(sent[-1]["content"]) == 2
    assert len(last_user["content"]) == 1   # 原本的那則 user 沒被附加（附加只發生在拷貝）
    # 壓縮前的原最後一則 user 沒有被附加指示：用另一份未壓縮的歷史再檢查一次
    m2 = [{"role": "user", "content": [{"type": "text", "text": "任務"}]}]
    m2 += [{"role": "assistant", "content": [tool("a", "bash", command="ls")]},
           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}]}]
    c2 = FakeClient([APIErr()])
    agent.compact(c2, m2, agent.new_log_path())
    assert len(m2[-1]["content"]) == 1 and m2[-1]["content"][0]["type"] == "tool_result"

def APIErr():
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "http://x")) if hasattr(httpx2, "Request") else None

def test_no_compaction_when_tool_use_unpaired(env):
    messages = [{"role": "user", "content": [{"type": "text", "text": "q"}]},
                {"role": "assistant", "content": [tool("a", "bash", command="ls")]}]
    client = FakeClient([])
    assert agent.compact(client, messages, agent.new_log_path()) is False
    assert client.calls == [] and len(messages) == 2

def test_summary_api_error_keeps_history_and_loop_continues(env, monkeypatch, capsys):
    script = [msg([tool("a", "bash", command="echo 1")], "tool_use", usage=(30000, 10)),
              APIErr(), msg([text("done")], "end_turn")]
    client, messages, _ = run(script, env)
    assert "摘要請求失敗" in capsys.readouterr().out
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert len(client.calls[2]["messages"]) == 3 and client.calls[2]["messages"][0]["content"][0]["text"] == "go"   # 沒壓縮：歷史照舊
    check_pairing(messages)

def test_empty_summary_does_not_compact(env, capsys):
    messages = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    client = FakeClient([msg([], "end_turn")])
    assert agent.compact(client, messages, agent.new_log_path()) is False
    assert "沒有文字" in capsys.readouterr().out and len(messages) == 1

def test_no_usage_no_compaction(env):
    script = [msg([tool("a", "bash", command="echo 1")], "tool_use"), msg([text("done")], "end_turn")]
    client, messages, _ = run(script, env)
    assert len(client.calls) == 2 and all("tool_choice" not in c for c in client.calls)
    assert agent.needs_compaction(None) is False
    assert agent.needs_compaction(BetaUsage.model_construct(input_tokens=20001, output_tokens=0)) is True
    assert agent.needs_compaction(BetaUsage.model_construct(input_tokens=10000, output_tokens=10000)) is False

def test_compaction_keeps_all_user_texts_and_loop_stays_valid(env):
    messages = []
    agent.add_user_text(messages, "第一句")
    client = FakeClient([msg([tool("a", "bash", command="echo 1")], "tool_use", usage=(100, 1)),
                         msg([text("ok")], "end_turn", usage=(100, 1))])
    agent.run_turn(client, messages, agent.new_log_path())
    agent.add_user_text(messages, "第二句：不要改 check.py")
    client2 = FakeClient([msg([text("S")], "end_turn"), msg([tool("b", "bash", command="echo 2")], "tool_use", usage=(100, 1)),
                          msg([text("done")], "end_turn")])
    assert agent.compact(client2, messages, agent.new_log_path()) is True
    blocks = [b["text"] for b in messages[0]["content"]]
    assert agent.USER_PREFIX + "第一句" in blocks and agent.USER_PREFIX + "第二句：不要改 check.py" in blocks
    agent.run_turn(client2, messages, agent.new_log_path())
    check_pairing(messages)
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]

def repl(monkeypatch, client, inputs):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(agent, "load_env", lambda: None)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    monkeypatch.setattr(agent, "WORKDIR", agent.WORKDIR)
    monkeypatch.setattr(sys, "argv", ["agent.py"])
    it = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    agent.main()

def test_repl_compact_and_tokens(env, monkeypatch, capsys):
    client = FakeClient([msg([text("hi")], "end_turn", usage=(1234, 56)),
                         msg([text("S")], "end_turn"),
                         msg([text("again")], "end_turn", usage=(77, 8))])
    repl(monkeypatch, client, ["你好", "/tokens", "/compact", "/tokens", "再來", "/tokens", "/exit"])
    out = capsys.readouterr().out
    assert out.count("最近一次回應：input=1234 output=56") == 1   # 只有壓縮前那次；壓縮後 usage 清空
    assert "還沒有 usage 紀錄" in out and "🗜 context 壓縮：1290 tokens → 摘要 1 字" in out
    assert "input=77 output=8" in out
    assert client.calls[1]["tool_choice"] == {"type": "none"}   # /compact 不管門檻（1290 < 20000）
    assert client.calls[2]["messages"][0]["content"][1]["text"] == agent.USER_PREFIX + "你好"
    assert client.calls[2]["messages"][0]["content"][-1]["text"] == "再來"   # 壓縮後再打的字附加在同一則 user 後面

def test_repl_compact_on_empty_history(env, monkeypatch, capsys):
    client = FakeClient([])
    repl(monkeypatch, client, ["/compact", "/tokens", "/exit"])
    out = capsys.readouterr().out
    assert "沒有壓縮" in out and "還沒有 usage 紀錄" in out and client.calls == []


# ---- v3 fixture：docstring_repo ----
DOC_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "docstring_repo"

def copy_doc_fixture(dst):
    import shutil
    shutil.copytree(DOC_FIXTURE, dst, ignore=shutil.ignore_patterns("__pycache__"))

def run_doc_check(repo):
    import subprocess
    return subprocess.run(["python3", "check_docstrings.py"], cwd=repo, capture_output=True, text=True,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

def add_docstrings(repo):
    import ast
    for f in repo.rglob("*.py"):
        if f.name == "check_docstrings.py":
            continue
        lines = f.read_text().splitlines()
        funcs = [n for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, ast.FunctionDef)]
        for n in sorted(funcs, key=lambda n: n.body[0].lineno, reverse=True):   # 由後往前插，行號才不會位移
            lines.insert(n.body[0].lineno - 1, " " * (n.col_offset + 4) + '"""一行說明。"""')
        f.write_text("\n".join(lines) + "\n")

def test_doc_fixture_fails_and_lists_every_function(tmp_path):
    copy_doc_fixture(tmp_path / "r")
    r = run_doc_check(tmp_path / "r")
    missing = [l for l in r.stderr.splitlines() if l.startswith("FAIL 沒有 docstring")]
    assert r.returncode != 0 and "ALL OK" not in r.stdout
    assert len(missing) == 56 and "text/wrap.py:first_line" in r.stderr
    assert len(list((tmp_path / "r").rglob("*.py"))) == 25   # 24 個模組 + 檢查器

def test_doc_fixture_with_docstrings_is_all_ok(tmp_path):
    copy_doc_fixture(tmp_path / "r")
    add_docstrings(tmp_path / "r")
    r = run_doc_check(tmp_path / "r")
    assert r.returncode == 0 and "ALL OK" in r.stdout, r.stderr

def test_doc_fixture_deleting_a_function_fails(tmp_path):
    copy_doc_fixture(tmp_path / "r")
    add_docstrings(tmp_path / "r")
    (tmp_path / "r/text/vowels.py").write_text('def count_vowels(s):\n    """數母音。"""\n    return 0\n')
    r = run_doc_check(tmp_path / "r")
    assert r.returncode != 0 and "函式數量 55 != 預期 56" in r.stderr

def test_doc_fixture_syntax_error_fails(tmp_path):
    copy_doc_fixture(tmp_path / "r")
    add_docstrings(tmp_path / "r")
    (tmp_path / "r/text/vowels.py").write_text("def count_vowels(s:\n")
    r = run_doc_check(tmp_path / "r")
    assert r.returncode != 0 and "無法編譯" in r.stderr

def test_doc_fixture_imports(tmp_path):
    import subprocess
    r = subprocess.run([sys.executable, "-c", "import glob, importlib.util as u\n"
                        "for f in glob.glob('*/*.py'):\n s=u.spec_from_file_location('m',f); m=u.module_from_spec(s); s.loader.exec_module(m)"],
                       cwd=DOC_FIXTURE, capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stderr


# ---- v3：summarize_log 的判定 ----
def v3_setup(tmp_path, fixed=True):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    repo, fx = tmp_path / "sandbox" / "docstring_repo", tmp_path / "fixtures" / "docstring_repo"
    copy_doc_fixture(fx); copy_doc_fixture(repo)
    if fixed:
        add_docstrings(repo)
    return summarize_log, repo, fx

def v3_log(tmp_path, n_calls=32, compact_after=16, peak=24000, reread=None, compact_at=20000):
    rows, results = [], []
    for i in range(1, n_calls + 1):
        path = f"docstring_repo/f{i}.py"
        name, inp = ("edit", {"path": path, "old_string": "a", "new_string": "b"})
        if reread and i == reread[0]:
            name, inp = "read_file", {"path": reread[1]}
        rows.append({"turn": i, "request": {"messages": [{"role": "user", "content": results}] if results else [{"role": "user", "content": "幫 docstring_repo 補 docstring"}]},
                     "response": {"stop_reason": "tool_use", "usage": {"input_tokens": peak if i == compact_after else 3000, "output_tokens": 5},
                                  "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})
        results = results + [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]
        if compact_after is not None and i == compact_after:
            rows.append({"turn": i + 1, "compaction": {"before_tokens": peak, "messages_before": 2 * i, "summary": "摘要",
                                                       "compact_at": compact_at, "user_texts": ["t"], "summary_usage": {"input_tokens": peak + 500, "output_tokens": 80}}})
            results = []
    p = tmp_path / "v3.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p

def v3_verdicts(mod, log, repo, fx, capsys, extra=()):
    mod.main([str(log), "--v3-repo", str(repo), "--v3-fixture", str(fx), *extra])
    out = capsys.readouterr().out
    sec = out.split("--- v3 完成標準判定 ---")[1]
    return {l.split()[2][0]: l.split()[1] for l in sec.splitlines() if l.startswith(("✅", "❌"))}, out

def test_v3_judge_all_pass(tmp_path, capsys):
    mod, repo, fx = v3_setup(tmp_path)
    v, out = v3_verdicts(mod, v3_log(tmp_path), repo, fx, capsys)
    assert set(v.values()) == {"PASS"} and len(v) == 5, out
    assert "🗜 compaction：24000 → 摘要 2 字" in out and "壓縮前 24000 → 壓縮後 3000" in out
    assert "6. 壓縮後又 read_file" in out and "0 次" in out

def test_v3_judge_no_compaction_fails_item2(tmp_path, capsys):
    mod, repo, fx = v3_setup(tmp_path)
    v, _ = v3_verdicts(mod, v3_log(tmp_path, compact_after=None), repo, fx, capsys)
    assert v["2"] == "FAIL" and v["3"] == "FAIL" and v["1"] == "PASS" and v["4"] == "PASS"

def test_v3_judge_too_few_calls_unfixed_and_peak(tmp_path, capsys):
    mod, repo, fx = v3_setup(tmp_path, fixed=False)
    v, out = v3_verdicts(mod, v3_log(tmp_path, n_calls=10, compact_after=5, peak=90000), repo, fx, capsys)
    assert v["1"] == "FAIL" and v["4"] == "FAIL" and v["5"] == "FAIL"
    # log 沒記錄門檻（舊 log）→ 用參數
    old = v3_log(tmp_path, n_calls=10, compact_after=5, peak=90000, compact_at=None)
    v, _ = v3_verdicts(mod, old, repo, fx, capsys, ("--compact-at", "50000"))
    assert v["5"] == "PASS"
    v, _ = v3_verdicts(mod, old, repo, fx, capsys)   # 預設 8000
    assert v["5"] == "FAIL"

def test_v3_judge_item5_prefers_logged_compact_at(tmp_path, capsys):
    mod, repo, fx = v3_setup(tmp_path)
    log = v3_log(tmp_path, peak=24000, compact_at=20000)
    v, out = v3_verdicts(mod, log, repo, fx, capsys, ("--compact-at", "1000"))   # 參數會讓它 FAIL，log 的 20000 讓它 PASS
    assert v["5"] == "PASS" and "門檻 20000（log）" in out

def test_v3_judge_reports_amnesia_without_failing(tmp_path, capsys):
    mod, repo, fx = v3_setup(tmp_path)
    log = v3_log(tmp_path, reread=(20, "docstring_repo/f3.py"))   # f3 在壓縮前改好，壓縮後又被讀
    v, out = v3_verdicts(mod, log, repo, fx, capsys)
    assert set(v.values()) == {"PASS"} and "1 次" in out and "docstring_repo/f3.py" in out

def test_v3_judge_absent_in_v1_log(tmp_path, capsys):
    mod, repo, fx = judge_setup(tmp_path)
    mod.main([str(fake_log(tmp_path, GOOD)), "--repo", str(repo), "--fixture", str(fx)])
    assert "v3 完成標準判定" not in capsys.readouterr().out


# ---- v3 修正：連續壓縮、截斷、冷卻、tool_result log、失憶偵測 ----
def texts_of(messages):
    return [b["text"] for m in messages if m["role"] == "user" for b in m["content"] if b["type"] == "text"]

def test_consecutive_compaction_does_not_stack_summaries(env):
    messages = []
    agent.add_user_text(messages, "第一句")
    assert agent.compact(FakeClient([msg([text("舊摘要AAA")], "end_turn")]), messages, agent.new_log_path())
    agent.add_user_text(messages, "第二句")
    client = FakeClient([msg([text("新摘要BBB")], "end_turn")])
    assert agent.compact(client, messages, agent.new_log_path())
    ts = texts_of(messages)
    raw = [t.removeprefix(agent.USER_PREFIX) for t in ts if t.startswith(agent.USER_PREFIX)]
    assert raw == ["第一句", "第二句"]
    assert agent.user_texts(messages) == ["第一句", "第二句"]   # 沒有任何舊摘要／標頭被當成原文
    assert not any("舊摘要AAA" in t for t in ts) and sum(t.startswith(agent.SUMMARY_PREFIX) for t in ts) == 1
    assert "新摘要BBB" in ts[-1] and sum(t.startswith(agent.HEADER_PREFIX) for t in ts) == 1

def log_rows(log):
    return [json.loads(l) for l in log.read_text().splitlines()]

def test_truncated_summary_is_not_adopted(env, capsys):
    messages = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    before = list(messages)
    log = agent.new_log_path()
    assert agent.compact(FakeClient([msg([text("半截摘要")], "max_tokens")]), messages, log, 5) is False
    assert messages == before and "max_tokens" in capsys.readouterr().out
    assert [r["compaction_failed"] for r in log_rows(log) if "compaction_failed" in r][0]["reason"].endswith("max_tokens")
    assert agent.COMPACT_COOLDOWN == agent.COMPACT_COOLDOWN_TURNS

def summary_calls(client):
    return sum(1 for c in client.calls if c.get("tool_choice") == {"type": "none"})

def test_cooldown_after_failure_then_retry(env):
    # 第 1 輪後 usage 超標 → 第 2 輪前嘗試（失敗）；第 3、4、5 輪冷卻；第 6 輪再試（第 2 輪的普通呼叫算在失敗那輪內）
    tu = lambda i: msg([tool(f"t{i}", "bash", command="echo")], "tool_use", usage=(30000, 1))
    script = [tu(1), msg([text("截")], "max_tokens"), tu(2), tu(3), tu(4), tu(5), msg([text("截")], "max_tokens"), msg([text("done")], "end_turn")]
    client, _, log = run(script, env)
    assert summary_calls(client) == 2
    kinds = ["S" if c.get("tool_choice") else "N" for c in client.calls]
    assert kinds == ["N", "S", "N", "N", "N", "N", "S", "N"]
    assert sum("compaction_failed" in r for r in log_rows(log)) == 2

def test_manual_compact_ignores_cooldown_and_reset_clears(env, monkeypatch):
    monkeypatch.setattr(agent, "COMPACT_COOLDOWN", 3)
    messages = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    assert agent.compact(FakeClient([msg([text("S")], "end_turn")]), messages, agent.new_log_path()) is True
    client = FakeClient([msg([text("hi")], "end_turn"), msg([text("S")], "end_turn")])
    repl(monkeypatch, client, ["/compact", "/exit"])   # main() 開頭清冷卻；/compact 本來就不看它
    assert agent.COMPACT_COOLDOWN == 0
    monkeypatch.setattr(agent, "COMPACT_COOLDOWN", 3)
    client = FakeClient([msg([text("hi")], "end_turn"), msg([text("S")], "end_turn")])
    repl(monkeypatch, client, ["你好", "/reset", "/exit"])
    assert agent.COMPACT_COOLDOWN == 0

def test_log_tool_result_lines(env, monkeypatch):
    monkeypatch.setattr(agent, "PERMISSION_MODE", "ask")
    monkeypatch.setattr(agent, "ask_user", lambda name, args, reason: ("n", "") if name == "write_file" else ("y", ""))
    script = [msg([tool("a", "bash", command="echo hi"), tool("b", "read_file", path="missing.txt"),
                   tool("c", "write_file", path="x.txt", content="1"), tool("d", "bash", command="echo no")], "tool_use"),
              msg([text("ok")], "end_turn")]
    _, _, log = run(script, env)
    tr = {r["tool_result"]["id"]: r["tool_result"] for r in log_rows(log) if "tool_result" in r}
    assert list(tr) == ["a", "b", "c", "d"]
    assert tr["a"]["is_error"] is False and "hi" in tr["a"]["content"] and tr["a"]["tool"] == "bash"
    assert tr["b"]["is_error"] is True
    assert tr["c"]["is_error"] is True and "拒絕" in tr["c"]["content"]
    assert tr["d"]["is_error"] is True and "沒有執行" in tr["d"]["content"]   # skipped
    big = agent.execute_tool("bash", {"command": "echo hi"})
    assert len(json.dumps(big["content"])) > 0 and all(len(v["content"]) <= 300 for v in tr.values())

def test_compaction_log_has_compact_at(env, monkeypatch):
    monkeypatch.setattr(agent, "COMPACT_AT_TOKENS", 12345)
    _, _, log = run(compaction_script(), env)
    assert next(r["compaction"] for r in log_rows(log) if "compaction" in r)["compact_at"] == 12345

def amnesia_log(tmp_path, steps):
    """steps: [("edit"|"read_file"|"write_file"|"bash"|"COMPACT", path_or_cmd, old_string)]；edit 一律成功。"""
    rows, n = [], 0
    for st in steps:
        if st[0] == "COMPACT":
            rows.append({"turn": n, "compaction": {"before_tokens": 1, "summary": "s", "user_texts": []}})
            continue
        n += 1
        inp = {"command": st[1]} if st[0] == "bash" else {"path": st[1], **({"old_string": st[2], "new_string": "n"} if st[0] == "edit" else {})}
        rows.append({"turn": n, "response": {"content": [{"type": "tool_use", "id": f"t{n}", "name": st[0], "input": inp}]}})
        rows.append({"turn": n, "tool_result": {"id": f"t{n}", "tool": st[0], "is_error": False, "content": "ok"}})
    p = tmp_path / "am.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p

def test_forgot_after_compaction(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    # (a) 壓縮後 read F 再 edit F：不是失憶
    a = summarize_log.forgot_after_compaction(amnesia_log(tmp_path, [("edit", "F.py", "x"), ("COMPACT",), ("read_file", "F.py"), ("edit", "F.py", "y")]))
    assert a["reread"] == [] and a["dup"] == []
    # (b) 壓縮後只 read F、沒再 edit：疑似失憶
    b = summarize_log.forgot_after_compaction(amnesia_log(tmp_path, [("edit", "F.py", "x"), ("COMPACT",), ("read_file", "F.py")]))
    assert b["reread"] == ["F.py"]
    # 沒改過的檔案、壓縮前的讀取都不算
    c = summarize_log.forgot_after_compaction(amnesia_log(tmp_path, [("read_file", "F.py"), ("edit", "G.py", "x"), ("COMPACT",), ("read_file", "F.py")]))
    assert c["reread"] == []
    # 重複編輯、write_file、bash cat
    d = summarize_log.forgot_after_compaction(amnesia_log(tmp_path, [("edit", "F.py", "same"), ("COMPACT",), ("edit", "F.py", "same"),
                                                                    ("write_file", "F.py"), ("bash", "cat F.py")]))
    assert d["dup"] == ["F.py"] and d["other"] == ["write_file F.py", "bash F.py"]

def test_summarize_tolerates_unknown_and_new_lines(tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    rows = [{"turn": 1, "mystery": 1}, {"turn": 2, "compaction_failed": {"reason": "摘要回應 stop_reason=max_tokens"}},
            {"turn": 2, "tool_result": {"id": "x", "tool": "bash", "is_error": False, "content": "ok"}}]
    p = tmp_path / "u.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    summarize_log.main([str(p)])
    out = capsys.readouterr().out
    assert "compaction 失敗" in out and "max_tokens" in out


# ---- v4：專案記憶（CLAUDE.md）----
def test_load_memory_missing_and_normal(env):
    assert agent.load_memory() is None
    (agent.WORKDIR / "CLAUDE.md").write_text("用 python3\n", encoding="utf-8")
    assert agent.load_memory() == "用 python3\n"

def test_load_memory_truncates_with_marker(env, capsys):
    (agent.WORKDIR / "CLAUDE.md").write_text("字" * (agent.MAX_MEMORY_CHARS + 50), encoding="utf-8")
    m = agent.load_memory()
    assert m.startswith("字" * agent.MAX_MEMORY_CHARS) and m.endswith("[... CLAUDE.md 過長，已截斷 ...]")
    assert "字" * (agent.MAX_MEMORY_CHARS + 1) not in m and "已截斷" in capsys.readouterr().out

def test_load_memory_non_utf8_returns_none(env, capsys):
    (agent.WORKDIR / "CLAUDE.md").write_bytes(b"\xff\xfe\x00bad")
    assert agent.load_memory() is None
    assert "[警告]" in capsys.readouterr().out

def test_load_memory_ignores_parent_dir(env):
    (agent.WORKDIR.parent / "CLAUDE.md").write_text("上一層的，不該被讀到", encoding="utf-8")
    assert agent.load_memory() is None

def test_load_memory_blank_file_is_none(env):
    (agent.WORKDIR / "CLAUDE.md").write_text("  \n\n", encoding="utf-8")
    assert agent.load_memory() is None

def test_load_memory_directory_is_none(env):
    (agent.WORKDIR / "CLAUDE.md").mkdir()
    assert agent.load_memory() is None

def test_load_memory_symlink_outside_rejected_inside_ok(env, capsys):
    out = agent.WORKDIR.parent / "outside.txt"
    out.write_text("外部機密", encoding="utf-8")
    link = agent.WORKDIR / "CLAUDE.md"
    link.symlink_to(out)
    assert agent.load_memory() is None and "指向 WORKDIR 外面" in capsys.readouterr().out
    assert "外部機密" not in agent.build_system(agent.load_memory())
    link.unlink()
    (agent.WORKDIR / "real.md").write_text("內部記憶", encoding="utf-8")
    link.symlink_to(agent.WORKDIR / "real.md")
    assert agent.load_memory() == "內部記憶"

def test_build_request_system_with_and_without_memory(env, monkeypatch):
    assert agent.build_request([])["system"] == agent.SYSTEM_PROMPT   # 沒經過 main：和 v3 一樣
    assert agent.build_system(None) == agent.SYSTEM_PROMPT
    monkeypatch.setattr(agent, "SESSION_SYSTEM", agent.build_system("一律用 python3"))
    s = agent.build_request([])["system"]
    assert s.startswith(agent.SYSTEM_PROMPT) and "# 專案記憶" in s and s.endswith("一律用 python3")

def test_system_identical_across_turns_and_summary_request(env, monkeypatch):
    monkeypatch.setattr(agent, "SESSION_SYSTEM", agent.build_system("記憶內容"))
    monkeypatch.setattr(agent, "COMPACT_AT_TOKENS", 100)
    script = [msg([tool("a", "bash", command="echo 1")], "tool_use", usage=(500, 10)),
              msg([text("S")], "end_turn"),   # 壓縮的摘要請求
              msg([tool("b", "bash", command="echo 2")], "tool_use", usage=(50, 10)),
              msg([text("done")], "end_turn", usage=(50, 10))]
    client, _, log = run(script, env)
    assert len(client.calls) == 4 and client.calls[1]["tool_choice"] == {"type": "none"}
    assert {c["system"] for c in client.calls} == {agent.SESSION_SYSTEM}
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert all(r["request"]["system"] == agent.SESSION_SYSTEM for r in rows if "request" in r)
    assert next(r for r in rows if "compaction" in r)["compaction"]["system"] == agent.SESSION_SYSTEM

def test_main_loads_memory_and_memory_command_and_reset(env, monkeypatch, capsys):
    (agent.WORKDIR / "CLAUDE.md").write_text("一律用 python3", encoding="utf-8")
    seen = []
    inputs = iter(["/memory", "/reset", "/memory", "/exit"])
    def fake_input(prompt=""):
        seen.append(agent.SESSION_SYSTEM)   # 每次讀輸入時記下目前的 system
        return next(inputs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(agent, "load_env", lambda: None)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: FakeClient([]))
    monkeypatch.setattr(sys, "argv", ["agent.py"])
    monkeypatch.setattr("builtins.input", fake_input)
    agent.main()
    out = capsys.readouterr().out
    assert f"📒 已載入 {agent.WORKDIR.name}/CLAUDE.md（{len('一律用 python3')} 字）" in out
    assert out.count("一律用 python3\n") == 2 and out.count("下次啟動才生效") == 2
    assert len(set(seen)) == 1 and "一律用 python3" in seen[0]   # /reset 後 system 沒變

def test_main_without_memory_prints_nothing_and_memory_says_none(env, monkeypatch, capsys):
    repl(monkeypatch, FakeClient([]), ["/memory", "/exit"])
    out = capsys.readouterr().out
    assert "📒" not in out and "本 session 沒有載入 CLAUDE.md" in out

def test_main_reset_keeps_session_system_with_memory(env, monkeypatch):
    (agent.WORKDIR / "CLAUDE.md").write_text("記憶XYZ", encoding="utf-8")
    seen = []
    inputs = iter(["/reset", "/exit"])
    def fake_input(prompt=""):
        seen.append(agent.SESSION_SYSTEM)
        return next(inputs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(agent, "load_env", lambda: None)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: FakeClient([]))
    monkeypatch.setattr(sys, "argv", ["agent.py"])
    monkeypatch.setattr("builtins.input", fake_input)
    agent.main()
    assert seen[0] == seen[1] == agent.SESSION_SYSTEM != agent.SYSTEM_PROMPT and "記憶XYZ" in seen[0]

def test_main_ignores_claude_md_above_workdir(env, monkeypatch, capsys):
    (agent.WORKDIR.parent / "CLAUDE.md").write_text("上一層的", encoding="utf-8")
    repl(monkeypatch, FakeClient([]), ["/exit"])
    assert "📒" not in capsys.readouterr().out and "上一層的" not in agent.SESSION_SYSTEM


# ---- v4：summarize_log 的顯示修正與記憶判定 ----
def fake_rows(tmp_path, rows):
    p = tmp_path / "v4.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p

def req_row(i, system, cmd=None):
    content = [{"type": "tool_use", "id": f"t{i}", "name": "bash", "input": {"command": cmd}}] if cmd else []
    return {"turn": i, "request": {"system": system, "messages": []},
            "response": {"stop_reason": "tool_use" if cmd else "end_turn", "content": content}}

MEM_SYS = agent.build_system("一律用 python3")

def v4_out(mod, p, capsys):
    mod.main([str(p)])
    return capsys.readouterr().out

def test_v2_block_hidden_without_refusal_shown_with_n(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    perm = lambda ans: {"turn": 1, "permission": {"id": "t1", "tool": "bash", "input": {"command": "x"},
                                                  "decision": "ask", "answer": ans, "reason": "r"}}
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, "S", "ls"), perm("y")]), capsys)
    assert "v2 完成標準判定" not in out
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, "S", "ls"), perm("n")]), capsys)
    assert "v2 完成標準判定" in out

def test_v4_judge_pass_when_python3_first(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, MEM_SYS, "python3 hello.py"), req_row(2, MEM_SYS)]), capsys)
    assert "v4-記憶判定" in out and "❌" not in out.split("v4-記憶判定")[1] and out.count("✅ PASS") == 2
    assert "一律用 python3" in out   # 第 3 項：記憶前 200 字

def test_v4_judge_fail_when_bare_python_first(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, MEM_SYS, "python hello.py"),
                                           req_row(2, MEM_SYS, "python3 hello.py")]), capsys)
    assert "❌ FAIL 2." in out and "✅ PASS 1." in out

def test_v4_judge_fail_when_system_changes(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, MEM_SYS, "python3 a.py"), req_row(2, MEM_SYS + "!")]), capsys)
    assert "❌ FAIL 1." in out

def test_v4_judge_covers_compaction_system_and_na_without_python(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    comp = {"turn": 2, "compaction": {"before_tokens": 1, "messages_before": 1, "summary": "s", "system": "別的 system"}}
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, MEM_SYS, "ls"), comp]), capsys)
    assert "❌ FAIL 1." in out and "不適用" in out

def test_v4_judge_covers_failed_compaction_system(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    bad = {"turn": 2, "compaction_failed": {"reason": "400", "system": "別的 system"}}
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, MEM_SYS, "ls"), bad]), capsys)
    assert "❌ FAIL 1." in out
    old = {"turn": 2, "compaction_failed": {"reason": "舊 log"}}
    out = v4_out(mod, fake_rows(tmp_path, [req_row(1, MEM_SYS, "ls"), old]), capsys)
    assert "未涵蓋 1 次（含失敗 1 次）" in out

def test_compact_failed_logs_system(env, monkeypatch):
    monkeypatch.setattr(agent, "SESSION_SYSTEM", agent.build_system("m"))
    client = FakeClient([msg([text("S")], "max_tokens")])
    log = agent.new_log_path()
    assert agent.compact(client, [{"role": "user", "content": "hi"}], log, 1) is False
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert rows[0]["compaction_failed"]["system"] == agent.SESSION_SYSTEM

def test_v4_block_absent_without_memory(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    assert "v4-記憶判定" not in v4_out(mod, fake_rows(tmp_path, [req_row(1, agent.SYSTEM_PROMPT, "ls")]), capsys)


# ==== v4：TODO 工具 ====
def td(content, status="pending"):
    return {"content": content, "status": status}

def test_todo_write_replaces_whole_list(env):
    r = agent.execute_tool("todo_write", {"todos": [td("a", "in_progress"), td("b")]})
    assert "is_error" not in r and "[>] a" in r["content"] and "[ ] b" in r["content"] and "清單已更新" in r["content"]
    r = agent.execute_tool("todo_write", {"todos": [td("c", "completed")]})
    assert agent.TODOS == [td("c", "completed")] and "[x] c" in r["content"] and "[ ] b" not in r["content"]

@pytest.mark.parametrize("bad", [
    "不是陣列", [1], [{"content": "a"}], [{"status": "pending"}],
    [td("a", "done")], [td("   ")], [{"content": 3, "status": "pending"}],
    [td("a", "in_progress"), td("b", "in_progress")],
])
def test_todo_write_rejects_bad_input_and_keeps_old_list(env, bad):
    agent.execute_tool("todo_write", {"todos": [td("舊")]})
    r = agent.execute_tool("todo_write", {"todos": bad})
    assert r["is_error"] is True and agent.TODOS == [td("舊")]

def test_todo_write_missing_arg_and_empty_list(env):
    assert agent.execute_tool("todo_write", {})["is_error"] is True
    agent.execute_tool("todo_write", {"todos": [td("a")]})
    r = agent.execute_tool("todo_write", {"todos": []})
    assert "is_error" not in r and agent.TODOS == []

def test_todo_write_always_allowed_without_asking(ask_env):
    asked = ask_env()
    assert agent.check_permission("todo_write", {"todos": []}) == ("allow", "只更新 harness 內的 TODO 清單，不碰檔案")
    script = [msg([tool("a", "todo_write", todos=[td("x", "in_progress")])], "tool_use"), msg([text("ok")], "end_turn")]
    run(script, None)
    assert asked == [] and agent.TODOS == [td("x", "in_progress")]

def test_compaction_appends_todo_block_and_latest_only(env):
    messages = []
    agent.add_user_text(messages, "第一句")
    agent.execute_tool("todo_write", {"todos": [td("舊項目", "in_progress")]})
    assert agent.compact(FakeClient([msg([text("摘要1")], "end_turn")]), messages, agent.new_log_path())
    ts = texts_of(messages)
    assert ts[-1] == agent.TODO_PREFIX + "[>] 舊項目" and ts[-2].startswith(agent.SUMMARY_PREFIX)   # 在摘要之後
    agent.execute_tool("todo_write", {"todos": [td("舊項目", "completed"), td("新項目")]})
    agent.add_user_text(messages, "第二句")
    log = agent.new_log_path()
    assert agent.compact(FakeClient([msg([text("摘要2")], "end_turn")]), messages, log)
    ts = texts_of(messages)
    todo_blocks = [t for t in ts if t.startswith(agent.TODO_PREFIX)]
    assert todo_blocks == [agent.TODO_PREFIX + "[x] 舊項目\n[ ] 新項目"]
    assert agent.user_texts(messages) == ["第一句", "第二句"]   # TODO 區塊不被當成使用者原文
    c = [r["compaction"] for r in log_rows(log) if "compaction" in r][-1]   # 同一秒內 new_log_path 會回同一個檔，取最後一行
    assert c["todos"] == [td("舊項目", "completed"), td("新項目")] and c["user_texts"] == ["第一句", "第二句"]

def test_compaction_without_todos_adds_no_block(env):
    messages = []
    agent.add_user_text(messages, "hi")
    log = agent.new_log_path()
    assert agent.compact(FakeClient([msg([text("S")], "end_turn")]), messages, log)
    assert not any(t.startswith(agent.TODO_PREFIX) for t in texts_of(messages))
    assert next(r["compaction"] for r in log_rows(log) if "compaction" in r)["todos"] == []

def test_tools_identical_across_turns_and_compaction_with_todos(env):
    script = [msg([tool("a", "todo_write", todos=[td("x", "in_progress")])], "tool_use", usage=(30000, 100)),
              msg([text("S")], "end_turn"),
              msg([tool("b", "bash", command="echo 1")], "tool_use", usage=(100, 10)),
              msg([text("done")], "end_turn", usage=(100, 10))]
    client, messages, _ = run(script, env)
    assert len(client.calls) == 4 and all(c["tools"] == agent.TOOLS for c in client.calls)
    assert any(t["name"] == "todo_write" for t in agent.TOOLS)
    assert client.calls[2]["messages"][0]["content"][-1]["text"].startswith(agent.TODO_PREFIX)

def test_repl_reset_clears_todos_and_todo_command(env, monkeypatch, capsys):
    agent.TODOS[:] = [td("a", "completed"), td("b")]
    repl(monkeypatch, FakeClient([]), ["/todo", "/reset", "/todo", "/exit"])
    out = capsys.readouterr().out
    assert "[x] a\n[ ] b" in out and "目前沒有 TODO" in out and agent.TODOS == []

def todo_rows(steps):
    """steps: ("todo", [td...]) 成功 / ("todo_err", ...) 失敗 / ("todo_skip", ...) 被略過 / ("edit",) / ("comp", todos 或 None)"""
    rows, n = [], 0
    for st in steps:
        n += 1
        if st[0] == "comp":
            c = {"before_tokens": 1, "messages_before": 1, "summary": "s"}
            if st[1] is not None:
                c["todos"] = st[1]
            rows.append({"turn": n, "compaction": c})
            continue
        is_todo = st[0].startswith("todo")
        b = ({"type": "tool_use", "id": f"t{n}", "name": "todo_write", "input": {"todos": st[1]}} if is_todo else
             {"type": "tool_use", "id": f"t{n}", "name": st[0], "input": {"path": "a.py", "command": "ls"}})
        rows.append({"turn": n, "request": {"system": "S", "messages": []},
                     "response": {"stop_reason": "tool_use", "content": [b]}})
        rows.append({"turn": n, "tool_result": {"id": f"t{n}", "tool": b["name"], "is_error": st[0] in ("todo_err", "todo_skip"), "content": ""}})
    return rows

def todo_out(tmp_path, capsys, steps):
    tmp_path.mkdir(exist_ok=True)
    mod, _, _ = judge_setup(tmp_path)
    return v4_out(mod, fake_rows(tmp_path, todo_rows(steps)), capsys)

def test_todo_judge_pass(tmp_path, capsys):
    A, B = [td("a", "in_progress"), td("b")], [td("a", "completed"), td("b", "completed")]
    out = todo_out(tmp_path, capsys, [("todo", A), ("edit",), ("comp", A), ("bash",), ("todo", A), ("todo", B)])
    sec = out.split("v4-TODO 判定")[1]
    assert "❌" not in sec and sec.count("✅ PASS") == 4 and "todo_write 成功次數：3" in sec
    assert "[x] a" in sec and "壓縮 #1" in sec and "bash(" in sec

def test_todo_judge_edit_before_todo_fails_item1(tmp_path, capsys):
    B = [td("a", "completed")]
    out = todo_out(tmp_path, capsys, [("edit",), ("todo", B), ("todo", B), ("todo", B)])
    assert "❌ FAIL 1." in out and "✅ PASS 2." in out and "✅ PASS 3." in out

def test_todo_judge_items_2_3_4(tmp_path, capsys):
    A = [td("a", "in_progress")]
    out = todo_out(tmp_path, capsys, [("todo", A), ("comp", None), ("comp", [])])   # 舊 log 沒 todos、或 todos 空
    assert "❌ FAIL 2." in out and "❌ FAIL 3." in out and "❌ FAIL 4." in out

def test_todo_judge_compaction_before_any_todo_is_not_required(tmp_path, capsys):
    B = [td("a", "completed")]
    out = todo_out(tmp_path, capsys, [("comp", None), ("todo", B), ("todo", B), ("todo", B)])
    assert "✅ PASS 4." in out

def test_todo_warning_for_long_task_without_todo(tmp_path, capsys):
    out = todo_out(tmp_path, capsys, [("bash",)] * 11)
    assert "⚠ 長任務但沒用 TODO 工具" in out and "v4-TODO 判定" not in out
    out = todo_out(tmp_path / "b", capsys, [("bash",)] * 10)
    assert "長任務但沒用 TODO" not in out

def test_todo_judge_counts_only_successful_calls(tmp_path, capsys):
    B = [td("a", "completed")]
    out = todo_out(tmp_path, capsys, [("todo", B), ("todo_err", B), ("todo_skip", B)])
    assert "❌ FAIL 2." in out and "成功次數：1" in out
    out = todo_out(tmp_path / "b", capsys, [("todo_err", B), ("edit",), ("todo", B), ("todo", B), ("todo", B)])
    assert "❌ FAIL 1." in out   # 失敗的 todo_write 不算「先規劃」
    out = todo_out(tmp_path / "c", capsys, [("todo", [td("a", "in_progress")]), ("todo_err", [td("a", "completed")])])
    assert "❌ FAIL 3." in out   # 失敗的那份「全部 completed」不算最後清單

def test_todo_judge_bad_input_does_not_crash(tmp_path, capsys):
    out = todo_out(tmp_path, capsys, [("todo", "abc"), ("todo", [1, None, td("a", "completed")]), ("todo", [td("a", "completed")])])
    assert "v4-TODO 判定" in out and "✅ PASS 3." in out

def test_todo_judge_empty_final_list_uses_last_nonempty(tmp_path, capsys):
    B = [td("a", "completed")]
    out = todo_out(tmp_path, capsys, [("todo", [td("a", "in_progress")]), ("todo", B), ("todo", [])])
    assert "✅ PASS 3." in out
    out = todo_out(tmp_path / "b", capsys, [("todo", []), ("todo", []), ("todo", [])])
    assert "❌ FAIL 3." in out   # 從沒有過非空清單

def test_todo_judge_item4_not_applicable_without_list_at_compaction(tmp_path, capsys):
    B = [td("a", "completed")]
    out = todo_out(tmp_path, capsys, [("comp", None), ("todo", B), ("todo", B), ("todo", B)])
    sec = out.split("v4-TODO 判定")[1]
    assert "4. 壓縮帶清單：不適用" in sec and "全部通過" in sec

def test_todo_judge_item4_requires_latest_successful_list(tmp_path, capsys):
    A, B = [td("a", "in_progress")], [td("a", "completed")]
    out = todo_out(tmp_path, capsys, [("todo", A), ("todo", B), ("comp", A)])   # 帶了舊清單
    assert "❌ FAIL 4." in out
    out = todo_out(tmp_path / "b", capsys, [("todo", A), ("todo_err", B), ("comp", A)])   # 失敗的那份不算最新
    assert "✅ PASS 4." in out

@pytest.mark.parametrize("bad", [
    [{"content": "a", "status": "pending", "id": 1}],
    [td("第一行\n第二行")], [td("a\rb")],
    [td(f"x{i}") for i in range(51)], [td("字" * 201)],
])
def test_todo_write_rejects_extra_fields_newlines_and_over_limits(env, bad):
    agent.execute_tool("todo_write", {"todos": [td("舊")]})
    r = agent.execute_tool("todo_write", {"todos": bad})
    assert r["is_error"] is True and agent.TODOS == [td("舊")]

def test_todo_write_strips_whitespace_and_accepts_limits(env):
    r = agent.execute_tool("todo_write", {"todos": [td("  a \t")]})
    assert "is_error" not in r and agent.TODOS == [td("a")]
    r = agent.execute_tool("todo_write", {"todos": [td("字" * 200) for _ in range(50)]})
    assert "is_error" not in r and len(agent.TODOS) == 50


# ==== v4：subagent（task 工具）====
class RoutedClient:
    """依 system 分辨主對話與 subagent 的請求，各有自己的腳本與呼叫紀錄。"""
    def __init__(self, main, sub):
        self.main, self.sub = FakeClient(main), FakeClient(sub)
        self.beta = self.messages = self
    def create(self, **kw):
        return (self.sub if kw["system"] == agent.SUBAGENT_SYSTEM else self.main).create(**kw)

def task_call(id="k", prompt="找出所有折扣函式，回報檔案:行號"):
    return tool(id, "task", description="找折扣函式", prompt=prompt)

def run2(main, sub, env, messages=None, user="go"):
    client, messages = RoutedClient(main, sub), messages if messages is not None else []
    agent.add_user_text(messages, user)
    log = agent.new_log_path()
    agent.run_turn(client, messages, log)
    return client, messages, log

def sub_result(client, i=1):
    """subagent 第 i+1 次請求裡，上一批工具的 tool_result 區塊。"""
    return client.sub.calls[i]["messages"][-1]["content"]

def test_task_runs_subagent_and_main_only_gets_conclusion(env):
    put("shop.py", "def apply_discounts(): pass\n# 大量內容 SECRET-IN-FILE\n")
    sub = [msg([tool("s1", "grep", pattern="discount")], "tool_use"),
           msg([tool("s2", "read_file", path="shop.py")], "tool_use"),
           msg([text("結論：apply_discounts 在 shop.py:1")], "end_turn")]
    client, messages, _ = run2([msg([task_call()], "tool_use"), msg([text("好的，答案如上")], "end_turn")], sub, env)
    assert len(client.sub.calls) == 3 and len(client.main.calls) == 2
    assert messages[2]["content"][0]["content"] == "結論：apply_discounts 在 shop.py:1"
    dumped = json.dumps(messages, default=agent.to_jsonable, ensure_ascii=False)
    assert "SECRET-IN-FILE" not in dumped and "s1" not in dumped   # subagent 的工具結果與 id 不在主對話
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    check_pairing(messages)

def test_subagent_requests_use_own_system_and_fixed_tools(env):
    sub = [msg([tool("s1", "glob", pattern="*")], "tool_use"), msg([text("完")], "end_turn")]
    client, _, _ = run2([msg([task_call(prompt="P")], "tool_use"), msg([text("ok")], "end_turn")], sub, env)
    for c in client.sub.calls:
        assert c["system"] == agent.SUBAGENT_SYSTEM and c["tools"] == agent.SUB_TOOLS
        assert [t["name"] for t in c["tools"]] == ["read_file", "grep", "glob"]
        assert c["messages"][0] == {"role": "user", "content": "P"}   # 全新對話：第一則只有 prompt
    assert client.sub.calls[0]["messages"] == [{"role": "user", "content": "P"}]
    assert all(t in agent.TOOLS for t in agent.SUB_TOOLS)   # 同一份定義，不另寫
    for c in client.main.calls:   # 主對話不受影響
        assert c["system"] == agent.SESSION_SYSTEM != agent.SUBAGENT_SYSTEM and c["tools"] == agent.TOOLS
    assert [t["name"] for t in agent.TOOLS][-1] == "task" and len(agent.TOOLS) == 8
    k = client.sub.calls[0]   # 同 MODEL/EFFORT/fallbacks
    assert k["model"] == agent.MODEL and k["output_config"] == {"effort": agent.EFFORT} and k["fallbacks"] == "default"

def test_build_request_defaults_unchanged(env):
    k = agent.build_request([])
    assert k["system"] == agent.SESSION_SYSTEM and k["tools"] == agent.TOOLS

def test_system_prompt_mentions_task_and_eight_tools():
    assert "八個工具" in agent.SYSTEM_PROMPT and "task" in agent.SYSTEM_PROMPT and "七個" not in agent.SYSTEM_PROMPT

def test_subagent_cannot_use_write_bash_or_task(env):
    sub = [msg([tool("a", "write_file", path="x.txt", content="1"), tool("b", "bash", command="echo hi"),
                tool("c", "task", description="d", prompt="p")], "tool_use"), msg([text("沒辦法")], "end_turn")]
    client, messages, _ = run2([msg([task_call()], "tool_use"), msg([text("ok")], "end_turn")], sub, env)
    rs = sub_result(client)
    assert [r["is_error"] for r in rs] == [True, True, True] and all("沒有" in r["content"] for r in rs)
    assert not (agent.WORKDIR / "x.txt").exists() and agent.SUB_COUNT == 1   # 沒有遞迴：task 沒有再開 subagent
    assert len(client.sub.calls) == 2 and messages[2]["content"][0]["content"] == "沒辦法"

def test_subagent_path_outside_workdir_denied_without_prompt(ask_env):
    asked = ask_env()   # 沒有任何答案：被問到就會 StopIteration
    sub = [msg([tool("a", "read_file", path="../secret"), tool("b", "grep", pattern="x", path="/etc"),
                tool("c", "glob", pattern="../*")], "tool_use"), msg([text("被擋")], "end_turn")]
    client, _, _ = run2([msg([task_call()], "tool_use"), msg([text("ok")], "end_turn")], sub, None)
    rs = sub_result(client)
    assert all(r["is_error"] and "權限層拒絕" in r["content"] for r in rs) and asked == []

def test_subagent_treats_ask_as_denied_never_prompts(env, monkeypatch):
    monkeypatch.setattr(agent, "check_permission", lambda name, args: ("ask", "假裝要問"))
    monkeypatch.setattr(agent, "PROMPT", lambda q: pytest.fail("subagent 不能跳出 input"))
    monkeypatch.setattr(agent, "ask_user", lambda *a: pytest.fail("subagent 不能問使用者"))
    put("a.txt", "x")
    sub = [msg([tool("a", "read_file", path="a.txt")], "tool_use"), msg([text("被拒")], "end_turn")]
    # 主對話的 task 本身也走 check_permission，所以這裡直接呼叫 run_subagent
    client = RoutedClient([], sub)
    out = agent.run_subagent(client, "q", agent.new_log_path(), 1)
    r = sub_result(client)[0]
    assert r["is_error"] is True and "權限層拒絕" in r["content"] and out == {"content": "被拒"}

def test_subagent_permission_exception_is_denied(env, monkeypatch):
    def boom(name, args):
        raise ValueError("壞參數")
    monkeypatch.setattr(agent, "check_permission", boom)
    client = RoutedClient([], [msg([tool("a", "read_file", path="a")], "tool_use"), msg([text("好")], "end_turn")])
    agent.run_subagent(client, "q", agent.new_log_path(), 1)
    r = client.sub.calls[1]["messages"][-1]["content"][0]
    assert r["is_error"] is True and "壞參數" in r["content"]

def test_subagent_turn_limit_returns_text_with_reason(env, monkeypatch):
    monkeypatch.setattr(agent, "SUBAGENT_MAX_TURNS", 2)
    loop = lambda i, t: msg(([text(t)] if t else []) + [tool(f"s{i}", "glob", pattern="*")], "tool_use")
    out = agent.run_subagent(RoutedClient([], [loop(1, "進度A"), loop(2, "")]), "q", agent.new_log_path(), 1)
    assert "進度A" in out["content"] and "輪數用完（2 輪）" in out["content"] and "is_error" not in out
    out = agent.run_subagent(RoutedClient([], [loop(1, ""), loop(2, "")]), "q", agent.new_log_path(), 1)
    assert out["is_error"] is True and "輪數用完" in out["content"]

def test_subagent_max_tokens_refusal_and_empty_end_turn(env):
    run_sub = lambda m: agent.run_subagent(RoutedClient([], [m]), "q", agent.new_log_path(), 1)
    out = run_sub(msg([text("半截")], "max_tokens"))
    assert "半截" in out["content"] and "stop_reason=max_tokens" in out["content"] and "is_error" not in out
    out = run_sub(msg([], "refusal"))
    assert out["is_error"] is True and "refusal" in out["content"]
    out = run_sub(msg([], "end_turn"))
    assert out["is_error"] is True and "沒有產生任何文字" in out["content"]
    assert run_sub(msg([text("  完成  ")], "end_turn")) == {"content": "完成"}

def test_subagent_result_is_truncated(env):
    out = agent.run_subagent(RoutedClient([], [msg([text("字" * 30000)], "end_turn")]), "q", agent.new_log_path(), 1)
    assert "截斷了" in out["content"] and len(out["content"]) < 11000

def test_subagent_usage_does_not_touch_last_usage_or_compaction(env):
    # sub 的 usage 遠超 COMPACT_AT_TOKENS(20000)；若混進 LAST_USAGE，主對話下一輪前就會觸發壓縮
    sub = [msg([tool("s1", "glob", pattern="*")], "tool_use", usage=(50000, 900)), msg([text("完")], "end_turn", usage=(60000, 10))]
    main = [msg([task_call()], "tool_use", usage=(100, 5)), msg([text("ok")], "end_turn", usage=(200, 6))]
    client, _, _ = run2(main, sub, env)
    assert len(client.main.calls) == 2
    assert (agent.LAST_USAGE.input_tokens, agent.LAST_USAGE.output_tokens) == (200, 6)

def test_task_terminal_display(env, capsys):
    put("a.txt", "x")
    sub = [msg([tool("s1", "grep", pattern="折扣")], "tool_use", usage=(10, 2)),
           msg([tool("s2", "read_file", path="a.txt")], "tool_use", usage=(20, 3)),
           msg([text("完")], "end_turn", usage=(30, 4))]
    run2([msg([task_call()], "tool_use"), msg([text("ok")], "end_turn")], sub, env)
    out = capsys.readouterr().out
    assert "→ task: 找折扣函式" in out and "  ↳ [sub] grep(折扣)" in out and "  ↳ [sub] read_file(a.txt)" in out
    assert "  ↳ [sub] 完成（3 輪，in=60 out=9 tokens）" in out

def test_task_permission_allowed_even_in_ask_mode(ask_env):
    ask_env()
    assert agent.check_permission("task", {"description": "d", "prompt": "p"})[0] == "allow"

def test_task_bad_prompt_is_error_and_not_counted(env):
    for bad in ({}, {"prompt": ""}, {"prompt": "  "}, {"prompt": 3}):
        client, messages, _ = run2([msg([tool("k", "task", description="d", **bad)], "tool_use"), msg([text("ok")], "end_turn")], [], env)
        assert messages[2]["content"][0]["is_error"] is True and client.sub.calls == []
    assert agent.SUB_COUNT == 0

def test_execute_tool_has_no_task_so_it_cannot_recurse(env):
    assert agent.execute_tool("task", {"description": "d", "prompt": "p"})["is_error"] is True

def test_keyboard_interrupt_in_subagent_keeps_main_history_valid(env):
    messages = []
    with pytest.raises(KeyboardInterrupt):
        run2([msg([task_call("k1"), tool("k2", "grep", pattern="x")], "tool_use")],
             [msg([tool("s1", "glob", pattern="*")], "tool_use"), KeyboardInterrupt()], env, messages=messages)
    assert messages[-1]["role"] == "user" and [r["tool_use_id"] for r in messages[-1]["content"]] == ["k1", "k2"]
    check_pairing(messages)
    agent.add_user_text(messages, "再來")
    check_pairing(messages)

def test_api_error_in_subagent_becomes_task_error_and_batch_continues(env, capsys):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "http://x"))
    main = [msg([tool("w", "write_file", path="a.txt", content="hi"), task_call("k")], "tool_use"), msg([text("好")], "end_turn")]
    client, messages, log = run2(main, [err], env)
    assert "[API 錯誤] subagent" in capsys.readouterr().out
    check_pairing(messages)
    w, k = messages[2]["content"]
    assert w["tool_use_id"] == "w" and "is_error" not in w and (agent.WORKDIR / "a.txt").read_text() == "hi"   # 先寫入的結果是真的，不是「未執行」
    assert k["is_error"] is True and "subagent API 失敗（APIConnectionError" in k["content"] and "沒有結論" in k["content"]
    assert len(client.main.calls) == 2 and messages[-1]["role"] == "assistant"   # 主對話繼續下一輪
    e = next(r for r in log_rows(log) if "error" in r)
    assert e["agent"] == "sub" and e["sub_id"] == 1 and e["error"]["type"] == "APIConnectionError"

def test_task_after_rejected_tool_is_skipped_without_calling_subagent(env, ask_env):
    ask_env("n", "")
    client, messages, _ = run2([msg([tool("w", "write_file", path="a.txt", content="x"), task_call("k")], "tool_use"), msg([text("好")], "end_turn")], [], env)
    assert client.sub.calls == [] and agent.SUB_COUNT == 0
    assert [r["tool_use_id"] for r in messages[2]["content"]] == ["w", "k"] and all(r["is_error"] for r in messages[2]["content"])
    assert "沒有執行" in messages[2]["content"][1]["content"]

def test_log_rows_tagged_with_agent_and_sub_id(env):
    put("a.txt", "x" * 50)
    sub1 = [msg([tool("s1", "read_file", path="a.txt")], "tool_use"), msg([text("一")], "end_turn")]
    sub2 = [msg([text("二")], "end_turn")]
    _, _, log = run2([msg([task_call("k1")], "tool_use"), msg([task_call("k2")], "tool_use"), msg([text("ok")], "end_turn")], sub1 + sub2, env)
    rows = log_rows(log)
    main_rows = [r for r in rows if r.get("agent") != "sub"]
    sub_rows = [r for r in rows if r.get("agent") == "sub"]
    assert all("sub_id" not in r for r in main_rows) and {r["sub_id"] for r in sub_rows} == {1, 2}
    s1 = [r for r in sub_rows if r["sub_id"] == 1]
    assert [("request" in r, "response" in r, "tool_result" in r) for r in s1] == [(True, True, False), (False, False, True), (True, True, False)]
    assert next(r for r in s1 if "tool_result" in r)["tool_result"]["chars"] > 50
    assert s1[0]["request"]["system"] == agent.SUBAGENT_SYSTEM
    mtr = [r["tool_result"] for r in main_rows if "tool_result" in r]
    assert [(t["tool"], t["chars"]) for t in mtr] == [("task", 1), ("task", 1)]
    assert next(r for r in main_rows if "request" in r)["request"]["system"] == agent.SESSION_SYSTEM

def test_subagent_does_not_break_compaction_cooldown_or_session_state(env, monkeypatch):
    # 先放非零已知值；跑完 task 後三者都要不變（task 之後主對話那一輪會把 cooldown 減 1、LAST_USAGE 換成主對話自己的，所以用只跑到 task 為止的腳本檢查 sub 本身）
    sentinel = SimpleNamespace(input_tokens=777, output_tokens=3)
    monkeypatch.setattr(agent, "LAST_USAGE", sentinel)
    monkeypatch.setattr(agent, "COMPACT_COOLDOWN", 5)
    before = agent.SESSION_SYSTEM
    sub = msg([text("x")], "end_turn", usage=(50000, 900))
    agent.run_task(RoutedClient([], [sub]), {"prompt": "q"}, agent.new_log_path())
    assert agent.SESSION_SYSTEM == before and agent.COMPACT_COOLDOWN == 5 and agent.LAST_USAGE is sentinel

def test_subagent_request_has_no_tool_choice_but_compaction_request_does(env):
    # 對照：主對話的摘要請求會帶 tool_choice，sub 的請求不會（斷言才有鑑別力）
    client, _, _ = run2([msg([task_call()], "tool_use"), msg([text("ok")], "end_turn")], [msg([text("x")], "end_turn")], env)
    assert all("tool_choice" not in c for c in client.sub.calls)
    messages = [{"role": "user", "content": "hi"}]
    main = RoutedClient([msg([text("摘要")], "end_turn")], [])
    assert agent.compact(main, messages, agent.new_log_path(), 1) is True
    assert main.main.calls[0]["tool_choice"] == {"type": "none"}


# ---- v4-subagent：summarize_log ----
def sub_row(sub_id, **kw):
    return {"agent": "sub", "sub_id": sub_id, **kw}

def sub_rows_log(back_chars=200, got_chars=5000, main_in=100, redo=False, with_task=True):
    """主對話：task -> (之後可選地自己又 grep) -> end_turn；subagent：grep -> end_turn。"""
    uses = [{"type": "tool_use", "id": "k1", "name": "task", "input": {"description": "d", "prompt": "p"}}] if with_task else []
    rows = [{"turn": 1, "request": {"system": MEM_SYS, "messages": []}, "response": {"stop_reason": "tool_use", "content": uses,
            "usage": {"input_tokens": main_in, "output_tokens": 5}}},
            sub_row(1, turn=1, request={"system": agent.SUBAGENT_SYSTEM, "messages": []},
                    response={"stop_reason": "tool_use", "usage": {"input_tokens": 999999, "output_tokens": 77},
                              "content": [{"type": "tool_use", "id": "s1", "name": "grep", "input": {"pattern": "discount"}}]}),
            sub_row(1, turn=1, tool_result={"id": "s1", "tool": "grep", "is_error": False, "content": "x", "chars": got_chars}),
            sub_row(1, turn=2, request={"system": agent.SUBAGENT_SYSTEM, "messages": []},
                    response={"stop_reason": "end_turn", "content": [], "usage": {"input_tokens": 888888, "output_tokens": 11}}),
            {"turn": 1, "tool_result": {"id": "k1", "tool": "task", "is_error": False, "content": "結論", "chars": back_chars}}]
    nxt = [{"type": "tool_use", "id": "g1", "name": "grep", "input": {"pattern": "again"}}] if redo else []
    rows.append({"turn": 2, "request": {"system": MEM_SYS, "messages": []},
                 "response": {"stop_reason": "end_turn", "content": nxt, "usage": {"input_tokens": main_in + 50, "output_tokens": 5}}})
    return rows

def test_read_rows_splits_main_and_sub(tmp_path):
    mod, _, _ = judge_setup(tmp_path)
    p = fake_rows(tmp_path, sub_rows_log())
    assert mod.read_rows(p, sub=True) and not any(d.get("agent") == "sub" for d in mod.read_rows(p))

def test_summarize_judge_v4_ignores_sub_system(tmp_path):
    mod, _, _ = judge_setup(tmp_path)
    res = mod.judge_v4(fake_rows(tmp_path, sub_rows_log()))
    assert res and all(ok for ok, _, _ in res) and "共 2 個" in res[0][1]   # 只有主對話的 2 個 request

def test_summarize_v3_peak_ignores_sub_usage(tmp_path):
    mod, _, _ = judge_setup(tmp_path)
    res = mod.judge_v3(fake_rows(tmp_path, sub_rows_log()), tmp_path / "none", tmp_path / "none2", 8000)
    assert "峰值 150" in res[4][1] and res[4][0] is True   # 不是 999999

def test_summarize_prints_sub_summary_under_main_turn_and_main_totals(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    out = v4_out(mod, fake_rows(tmp_path, sub_rows_log()), capsys)
    assert "↳ sub#1：2 輪 in=1888887 out=88 | grep(discount)" in out
    assert "工具呼叫次數: {'task': 1}" in out and "總 tokens: input=250 output=10" in out   # 主對話自己的

def test_subagent_judge_pass(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    out = v4_out(mod, fake_rows(tmp_path, sub_rows_log(back_chars=200, got_chars=5000)), capsys)
    sec = out.split("v4-subagent 判定")[1]
    assert sec.count("✅ PASS") == 3 and "❌" not in sec and "200 字元 < subagent 收到的工具結果（截斷後）5000 字元" in sec
    assert "主對話 input_tokens 峰值：150" in sec and "input=1888887 output=88" in sec and "（重做搜尋）：0 次" in sec
    assert "mini_repo" not in out.split("v4-subagent 判定")[1].split("結果")[1] and "v1 完成標準" not in out

def test_subagent_log_prints_v1_skip_notice_and_exit_code_reflects_fail(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    ok = mod.main([str(fake_rows(tmp_path, sub_rows_log()))])
    assert ok == 0 and "略過 v1 判定：這類 log 是唯讀問答" in capsys.readouterr().out
    bad = mod.main([str(fake_rows(tmp_path, sub_rows_log(back_chars=1000, got_chars=5000)))])
    assert bad == 1   # 以前回傳 list，exit code 永遠是 0

def test_subagent_log_still_runs_v3_judge(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    rows = sub_rows_log()
    rows[0]["request"]["messages"] = [{"role": "user", "content": "docstring_repo"}]
    out = v4_out(mod, fake_rows(tmp_path, rows), capsys)
    assert "v3 完成標準判定" in out and "v4-subagent 判定" in out

def test_subagent_judge_fail_when_return_too_long(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    out = v4_out(mod, fake_rows(tmp_path, sub_rows_log(back_chars=1000, got_chars=5000)), capsys)   # 剛好 20% 也算不乾淨
    assert "❌ FAIL 3." in out and "比例 20.0%" in out and "有未通過項目" in out
    out = v4_out(mod, fake_rows(tmp_path, sub_rows_log(back_chars=999, got_chars=5000)), capsys)
    assert "✅ PASS 3." in out

def test_subagent_judge_fail_items_1_2_and_redo_observation(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    out = v4_out(mod, fake_rows(tmp_path, sub_rows_log(with_task=False)), capsys)
    assert "❌ FAIL 1." in out
    out = v4_out(mod, fake_rows(tmp_path, sub_rows_log(redo=True)), capsys)
    assert "（重做搜尋）：1 次" in out and "grep(again)" in out
    rows = [r for r in sub_rows_log() if not ("tool_result" in r and r.get("agent") == "sub")]   # subagent 沒用工具、沒收到結果
    for r in rows:
        if r.get("agent") == "sub" and "response" in r:
            r["response"]["content"] = []
    out = v4_out(mod, fake_rows(tmp_path, rows), capsys)
    assert "❌ FAIL 2." in out and "❌ FAIL 3." in out   # subagent 一個工具都沒用、沒有工具結果

def test_subagent_judge_absent_without_sub_rows_and_old_logs(tmp_path, capsys):
    mod, _, _ = judge_setup(tmp_path)
    assert mod.judge_subagent(fake_rows(tmp_path, [req_row(1, "S", "ls")])) is None
    assert "v4-subagent" not in v4_out(mod, fake_rows(tmp_path, [req_row(1, "S", "ls")]), capsys)

def test_end_to_end_run_then_summarize_pass(env, capsys):
    put("big.py", "\n".join(f"line {i} discount" for i in range(300)))
    sub = [msg([tool("s1", "grep", pattern="discount")], "tool_use", usage=(500, 20)), msg([text("big.py 全是")], "end_turn", usage=(900, 10))]
    _, _, log = run2([msg([task_call()], "tool_use", usage=(100, 5)), msg([text("ok")], "end_turn", usage=(120, 5))], sub, env)
    capsys.readouterr()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    summarize_log.main([str(log)])
    sec = capsys.readouterr().out.split("v4-subagent 判定")[1]
    assert sec.count("✅ PASS") == 3 and "全部通過" in sec and "input=1400 output=30" in sec


def test_v3_judge_gated_on_task_text_not_tool_output(tmp_path, capsys):
    # 真實踩到：subagent 的 glob 輸出含 docstring_repo，舊判斷用整份 log 文字，誤開 v3 判定並讀到 sandbox 現況而顯示 ALL OK
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    rows = [{"turn": 1, "request": {"messages": [{"role": "user", "content": "盤點 mini_repo 的折扣函式"}]},
             "response": {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 1},
                          "content": [{"type": "text", "text": "看過 docstring_repo/ 與 mini_repo/"}]}}]
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    summarize_log.main([str(p)])
    assert "v3 完成標準判定" not in capsys.readouterr().out


# ==== v4：hooks ====
def hook_cmd(tmp_path, name, body):
    f = tmp_path / f"{name}.py"
    f.write_text("import sys, json, os\n" + body)
    return f"{sys.executable} {f}"

def set_hooks(monkeypatch, pre=(), post=()):
    monkeypatch.setattr(agent, "HOOKS", {"pre_tool_use": [{"matcher": m, "command": c} for m, c in pre],
                                         "post_tool_use": [{"matcher": m, "command": c} for m, c in post]})

def hook_rows(log):
    return [json.loads(l) for l in log.read_text().splitlines() if '"hook"' in l]

def test_load_hooks_missing_bad_and_good(env, monkeypatch, capsys):
    assert agent.load_hooks() == {"pre_tool_use": [], "post_tool_use": []} and capsys.readouterr().out == ""
    f = env / "h.json"
    monkeypatch.setattr(agent, "HOOKS_FILE", f)
    for bad in ["{not json", "[]", '{"pre_tool_use": {}}', '{"other": []}',
                '{"pre_tool_use": [{"matcher": "bash", "command": "true"}, {"matcher": "(", "command": "true"}]}',
                '{"post_tool_use": [{"matcher": "x"}]}']:
        f.write_text(bad)
        assert agent.load_hooks() == {"pre_tool_use": [], "post_tool_use": []}   # 不部分載入：連前面正確的那條都不要
        assert "不載入任何 hook" in capsys.readouterr().out
    f.write_text('{"pre_tool_use": [{"matcher": "bash", "command": "true"}]}')
    assert agent.load_hooks() == {"pre_tool_use": [{"matcher": "bash", "command": "true"}], "post_tool_use": []}

def test_matcher_is_fullmatch(env, monkeypatch):
    cmd = hook_cmd(env, "m", "sys.exit(2)")
    set_hooks(monkeypatch, pre=[("edit", cmd)])
    _, messages, _ = run([msg([tool("a", "write_file", path="a.txt", content="1"), tool("b", "edit", path="a.txt", old_string="1", new_string="2")], "tool_use"),
                          msg([text("ok")], "end_turn")], env)
    assert (agent.WORKDIR / "a.txt").read_text() == "1"   # write_file 沒被 "edit" 擋；edit 被擋所以還是 1
    r = messages[2]["content"]
    assert "is_error" not in r[0] and r[1]["is_error"]
    set_hooks(monkeypatch, pre=[("ed", cmd), ("e.*|write", cmd)])
    assert agent.run_hooks("pre_tool_use", "edit", {}, env / "l", 1) != []   # "e.*|write" fullmatch edit
    assert agent.run_hooks("pre_tool_use", "write_file", {}, env / "l", 1) == []   # "write" 不 fullmatch write_file

def test_pre_exit2_blocks_and_tool_not_run(env, monkeypatch):
    cmd = hook_cmd(env, "p", "print('不准寫', file=sys.stderr); sys.exit(2)")
    set_hooks(monkeypatch, pre=[("write_file", cmd)])
    _, messages, log = run([msg([tool("a", "write_file", path="a.txt", content="1")], "tool_use"), msg([text("ok")], "end_turn")], env)
    assert not (agent.WORKDIR / "a.txt").exists()
    r = messages[2]["content"][0]
    assert r["is_error"] and r["content"].startswith("hook 擋下（") and "不准寫" in r["content"]
    check_pairing(messages)
    row = hook_rows(log)[0]["hook"]
    assert row["exit"] == 2 and row["decision"] == "block" and row["event"] == "pre_tool_use" and "不准寫" in row["stderr"] and "ms" in row

def test_pre_exit0_allows_and_gets_json_stdin(env, monkeypatch):
    out = env / "seen.json"
    cmd = hook_cmd(env, "p0", f"open({str(out)!r}, 'w').write(sys.stdin.read())")
    set_hooks(monkeypatch, pre=[(".*", cmd)])
    _, messages, _ = run([msg([tool("a", "write_file", path="a.txt", content="1")], "tool_use"), msg([text("ok")], "end_turn")], env)
    assert (agent.WORKDIR / "a.txt").read_text() == "1" and "is_error" not in messages[2]["content"][0]
    seen = json.loads(out.read_text())
    assert seen == {"event": "pre_tool_use", "tool": "write_file", "input": {"path": "a.txt", "content": "1"}, "workdir": str(agent.WORKDIR)}

def test_post_exit2_appends_report_keeps_result_and_is_error(env, monkeypatch):
    out = env / "seen.json"
    cmd = hook_cmd(env, "q", f"open({str(out)!r}, 'w').write(sys.stdin.read()); print('請改 logging', file=sys.stderr); sys.exit(2)")
    set_hooks(monkeypatch, post=[("bash", cmd)])
    _, messages, _ = run([msg([tool("a", "bash", command="echo HELLO; exit 3")], "tool_use"), msg([text("ok")], "end_turn")], env)
    r = messages[2]["content"][0]
    assert "HELLO" in r["content"] and "exit code: 3" in r["content"]   # 工具結果保留
    assert r["content"].endswith("\n\n[post_tool_use hook 回報]\n請改 logging")
    assert "is_error" not in r   # 刻意不設 is_error
    seen = json.loads(out.read_text())
    assert seen["event"] == "post_tool_use" and seen["result"]["is_error"] is False and "HELLO" in seen["result"]["content"]

def test_post_hook_result_content_capped_at_2000(env, monkeypatch):
    out = env / "seen.json"
    set_hooks(monkeypatch, post=[("bash", hook_cmd(env, "c", f"open({str(out)!r}, 'w').write(sys.stdin.read())"))])
    run([msg([tool("a", "bash", command="python3 -c \"print('x'*5000)\"")], "tool_use"), msg([text("ok")], "end_turn")], env)
    assert len(json.loads(out.read_text())["result"]["content"]) == 2000

def test_other_exit_timeout_and_missing_command_fail_open(env, monkeypatch, capsys):
    monkeypatch.setattr(agent, "HOOK_TIMEOUT", 1)
    cmds = [hook_cmd(env, "e1", "print('壞了', file=sys.stderr); sys.exit(1)"),
            hook_cmd(env, "e2", "import time; time.sleep(5)"),
            "/nonexistent-hook-binary"]
    set_hooks(monkeypatch, pre=[("write_file", c) for c in cmds])
    _, messages, log = run([msg([tool("a", "write_file", path="a.txt", content="1")], "tool_use"), msg([text("ok")], "end_turn")], env)
    assert (agent.WORKDIR / "a.txt").read_text() == "1" and "is_error" not in messages[2]["content"][0]   # 照常執行
    out = capsys.readouterr().out
    assert out.count("⚠ hook 失敗") == 3 and "壞了" in out
    rows = [r["hook"] for r in hook_rows(log)]
    assert [r["decision"] for r in rows] == ["error"] * 3 and rows[1]["exit"] == "timeout" and rows[2]["exit"] == 127

def test_multiple_hooks_run_in_order_and_pre_block_stops(env, monkeypatch):
    trace = env / "trace"
    mk = lambda n, code: hook_cmd(env, n, f"open({str(trace)!r}, 'a').write('{n}'); sys.exit({code})")
    set_hooks(monkeypatch, pre=[("bash", mk("A", 0)), ("bash", mk("B", 2)), ("bash", mk("C", 0))], post=[("bash", mk("X", 2)), ("bash", mk("Y", 2))])
    _, messages, _ = run([msg([tool("a", "bash", command="echo hi")], "tool_use"), msg([text("ok")], "end_turn")], env)
    assert trace.read_text() == "AB" and messages[2]["content"][0]["is_error"]   # B 擋下，C 不跑，post 也不跑
    trace.unlink()
    set_hooks(monkeypatch, post=[("bash", mk("X", 2)), ("bash", mk("Y", 2))])
    run([msg([tool("a", "bash", command="echo hi")], "tool_use"), msg([text("ok")], "end_turn")], env)
    assert trace.read_text() == "XY"   # post 的 exit 2 不中斷後面的 hook

def test_permission_deny_and_user_refusal_run_no_hooks(ask_env, monkeypatch):
    trace = agent.WORKDIR.parent / "trace"
    cmd = hook_cmd(agent.WORKDIR.parent, "t", f"open({str(trace)!r}, 'a').write('x')")
    set_hooks(monkeypatch, pre=[(".*", cmd)], post=[(".*", cmd)])
    ask_env("n", "")
    run([msg([tool("g", "glob", pattern="../*"), tool("a", "edit", path="a.py", old_string="1", new_string="2")], "tool_use"), msg([text("ok")], "end_turn")], None)
    assert not trace.exists()   # deny 與同批連帶略過
    put("b.py", "x = 1\n")
    run([msg([tool("a", "edit", path="b.py", old_string="x = 1", new_string="x = 2")], "tool_use"), msg([text("ok")], "end_turn")], None)
    assert not trace.exists() and (agent.WORKDIR / "b.py").read_text() == "x = 1\n"   # 使用者拒絕

def test_pre_block_skips_rest_of_batch_like_deny(env, monkeypatch):
    set_hooks(monkeypatch, pre=[("write_file", hook_cmd(env, "b", "sys.exit(2)"))])
    _, messages, log = run([msg([tool("a", "write_file", path="a.txt", content="1"),
                                 tool("b", "bash", command="echo second"),
                                 tool("c", "write_file", path="c.txt", content="3")], "tool_use"), msg([text("ok")], "end_turn")], env)
    r = messages[2]["content"]
    assert [x["tool_use_id"] for x in r] == ["a", "b", "c"] and all(x["is_error"] for x in r)
    assert "同一批中前面的 write_file" in r[1]["content"] and "同一批中前面的 write_file" in r[2]["content"]
    assert [p["decision"] for p in perms_of(log)] == ["allow", "skipped", "skipped"]
    assert len(hook_rows(log)) == 1   # 被略過的不跑 hook
    check_pairing(messages)

def test_post_exit2_does_not_skip_rest_of_batch(env, monkeypatch):
    set_hooks(monkeypatch, post=[("write_file", hook_cmd(env, "b", "sys.exit(2)"))])
    _, messages, _ = run([msg([tool("a", "write_file", path="a.txt", content="1"), tool("b", "write_file", path="b.txt", content="2")], "tool_use"),
                          msg([text("ok")], "end_turn")], env)
    assert (agent.WORKDIR / "b.txt").exists() and "is_error" not in messages[2]["content"][1]

def test_hook_env_has_no_anthropic_but_has_agent_workdir(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SECRET-KEY")
    out = env / "env.json"
    set_hooks(monkeypatch, pre=[("bash", hook_cmd(env, "v", f"open({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))"))])
    run([msg([tool("a", "bash", command="echo hi")], "tool_use"), msg([text("ok")], "end_turn")], env)
    e = json.loads(out.read_text())
    assert not [k for k in e if k.startswith("ANTHROPIC_")] and e["AGENT_WORKDIR"] == str(agent.WORKDIR)

def test_hook_cwd_is_project_root(env, monkeypatch):
    out = env / "cwd"
    set_hooks(monkeypatch, pre=[("bash", hook_cmd(env, "w", f"open({str(out)!r}, 'w').write(os.getcwd())"))])
    run([msg([tool("a", "bash", command="echo hi")], "tool_use"), msg([text("ok")], "end_turn")], env)
    assert out.read_text() == str(agent.ROOT)

def test_subagent_tools_run_hooks_and_log_sub_fields(env, monkeypatch):
    put("a.py", "x = 1\n")
    set_hooks(monkeypatch, pre=[("grep", hook_cmd(env, "g", "print('不准搜', file=sys.stderr); sys.exit(2)"))],
              post=[("read_file", hook_cmd(env, "r", "print('讀到了', file=sys.stderr); sys.exit(2)"))])
    sub = [msg([tool("s1", "grep", pattern="x")], "tool_use"), msg([tool("s2", "read_file", path="a.py")], "tool_use"), msg([text("結論")], "end_turn")]
    client, _, log = run2([msg([task_call()], "tool_use"), msg([text("ok")], "end_turn")], sub, env)
    assert "hook 擋下" in sub_result(client, 1)[0]["content"] and sub_result(client, 1)[0]["is_error"]
    got = sub_result(client, 2)[0]["content"]
    assert "x = 1" in got and "[post_tool_use hook 回報]\n讀到了" in got
    rows = hook_rows(log)
    assert [(r["agent"], r["sub_id"], r["hook"]["tool"]) for r in rows] == [("sub", 1, "grep"), ("sub", 1, "read_file")]

def test_task_tool_itself_goes_through_matcher(env, monkeypatch):
    set_hooks(monkeypatch, pre=[("task", hook_cmd(env, "t", "sys.exit(2)"))])
    client, messages, _ = run2([msg([task_call()], "tool_use"), msg([text("ok")], "end_turn")], [], env)
    assert client.sub.calls == [] and messages[2]["content"][0]["is_error"]

def test_main_loads_hooks_prints_count_and_hooks_command(env, monkeypatch, capsys):
    f = env / "h.json"
    f.write_text(json.dumps({"pre_tool_use": [{"matcher": "bash", "command": "true"}],
                             "post_tool_use": [{"matcher": "edit|write_file", "command": "python3 x.py"}, {"matcher": ".*", "command": "true"}]}))
    monkeypatch.setattr(agent, "HOOKS_FILE", f)
    repl(monkeypatch, FakeClient([]), ["/hooks", "/exit"])
    out = capsys.readouterr().out
    assert "🪝 已載入 3 個 hooks（pre 1、post 2）" in out
    assert "post_tool_use  edit|write_file  →  python3 x.py" in out and "下次啟動才生效" in out

def test_main_without_hooks_file_prints_nothing(env, monkeypatch, capsys):
    repl(monkeypatch, FakeClient([]), ["/hooks", "/exit"])
    out = capsys.readouterr().out
    assert "🪝" not in out and "本 session 沒有載入 hooks" in out

def test_main_bad_hooks_file_warns_and_loads_nothing(env, monkeypatch, capsys):
    f = env / "h.json"; f.write_text("{oops")
    monkeypatch.setattr(agent, "HOOKS_FILE", f)
    repl(monkeypatch, FakeClient([]), ["/hooks", "/exit"])
    out = capsys.readouterr().out
    assert "[警告]" in out and "🪝 已載入" not in out and "本 session 沒有載入 hooks" in out


# ---- fixtures/hooks/no_print_hook.py 本身 ----
NO_PRINT = Path(__file__).resolve().parent.parent / "fixtures" / "hooks" / "no_print_hook.py"

def run_no_print(tmp_path, tool_name, path, code):
    f = tmp_path / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(code)
    import subprocess
    payload = json.dumps({"event": "post_tool_use", "tool": tool_name, "input": {"path": path}, "workdir": str(tmp_path)})
    return subprocess.run([sys.executable, str(NO_PRINT)], input=payload, capture_output=True, text=True,
                          env={**os.environ, "AGENT_WORKDIR": str(tmp_path)})

def test_no_print_hook(tmp_path):
    r = run_no_print(tmp_path, "edit", "mini_repo/shop/orders.py", "def f():\n    print(1)\n")
    assert r.returncode == 2 and "mini_repo/shop/orders.py:2 使用了 print()" in r.stderr and "logging" in r.stderr
    r = run_no_print(tmp_path, "write_file", "mini_repo/shop/x.py", "def f(:\n")
    assert r.returncode == 2 and "語法錯誤" in r.stderr
    assert run_no_print(tmp_path, "edit", "mini_repo/shop/ok.py", "import logging\nlogger = logging.getLogger(__name__)\nx = 'print(1)'\n").returncode == 0
    assert run_no_print(tmp_path, "edit", "mini_repo/utils/u.py", "print(1)\n").returncode == 0   # shop/ 以外
    assert run_no_print(tmp_path, "edit", "mini_repo/shop/notes.txt", "print(1)\n").returncode == 0   # 非 .py
    assert run_no_print(tmp_path, "bash", "mini_repo/shop/b.py", "print(1)\n").returncode == 0   # 非 edit/write_file
    assert not list(tmp_path.rglob("__pycache__"))

def test_hooks_json_fixture_is_valid(env, monkeypatch):
    monkeypatch.setattr(agent, "HOOKS_FILE", NO_PRINT.parent / "hooks.json")
    h = agent.load_hooks()
    assert h["pre_tool_use"] == [] and h["post_tool_use"][0]["matcher"] == "edit|write_file"


# ---- summarize_log 的 v4-hooks 判定 ----
def hooks_log(tmp_path, exits, shop_code, with_sub=True):
    """exits: 依序每個 post hook 的 exit（都是同一個檔 shop/orders.py）。"""
    rows = []
    for i, code in enumerate(exits):
        rows += [{"turn": i + 1, "permission": {"id": f"t{i}", "tool": "edit", "input": {"path": "mini_repo/shop/orders.py"}, "decision": "allow"}},
                 {"turn": i + 1, "hook": {"event": "post_tool_use", "tool": "edit", "command": "c", "exit": code, "stderr": "", "ms": 10, "decision": "x"}},
                 {"turn": i + 1, "response": {"content": [{"type": "tool_use", "id": f"n{i}", "name": "bash", "input": {"command": "python3 check.py"}}], "stop_reason": "tool_use"}}]
    if with_sub:
        rows.append({"agent": "sub", "sub_id": 1, "turn": 1, "hook": {"event": "pre_tool_use", "tool": "grep", "command": "c", "exit": 2, "stderr": "", "ms": 1, "decision": "block"}})
    tmp_path.mkdir(exist_ok=True)
    log = tmp_path / "h.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows))
    repo = tmp_path / "sandbox" / "mini_repo"
    (repo / "shop").mkdir(parents=True)
    (repo / "shop" / "orders.py").write_text(shop_code)
    return log, repo

def import_summarize():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summarize_log
    return summarize_log

def test_summarize_hooks_pass(tmp_path):
    log, repo = hooks_log(tmp_path, [2, 0], "import logging\n")
    res, obs = import_summarize().judge_hooks(log, repo)
    assert [ok for ok, _, _ in res] == [True, True, True]
    assert "hook 總次數 2" in obs[0] and "subagent 另有 1 次" in obs[0]   # sub 的 hook 不計入主對話判定
    assert "bash(python3 check.py)" in obs[1]

def test_summarize_hooks_fail_cases(tmp_path):
    sl = import_summarize()
    log, repo = hooks_log(tmp_path, [2, 2], "print(1)\n")   # 最後一次仍 exit 2、檔案裡還有 print
    assert [ok for ok, _, _ in sl.judge_hooks(log, repo)[0]] == [True, False, False]
    log, repo = hooks_log(tmp_path / "b", [0, 0], "x = 1\n")
    assert [ok for ok, _, _ in sl.judge_hooks(log, repo)[0]] == [False, False, True]   # 規則從未觸發

def test_summarize_no_hook_rows_returns_none_and_main_skips_v1(tmp_path, capsys):
    sl = import_summarize()
    log = tmp_path / "n.jsonl"; log.write_text(json.dumps({"turn": 1, "response": {"content": []}}))
    assert sl.judge_hooks(log, tmp_path) is None
    log, repo = hooks_log(tmp_path / "c", [2, 0], "import logging\n", with_sub=False)
    code = sl.main([str(log), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "--- v4-hooks 判定 ---" in out and "略過 v1 判定" in out and "v1 完成標準判定" not in out and code == 0


# ==== v4：hooks 審查修正（fail-open 全例外、stderr 截斷、相對路徑、log path）====
def _one_write(env):
    return run([msg([tool("a", "write_file", path="a.txt", content="1")], "tool_use"), msg([text("ok")], "end_turn")], env)

def test_hook_oserror_fails_open(env, monkeypatch):
    set_hooks(monkeypatch, pre=[("write_file", "true")])
    def boom(*a, **k): raise PermissionError("nope")
    monkeypatch.setattr(agent.subprocess, "run", boom)
    _, messages, log = _one_write(env)
    assert (agent.WORKDIR / "a.txt").read_text() == "1" and "is_error" not in messages[2]["content"][0]
    check_pairing(messages)
    assert hook_rows(log)[0]["hook"]["decision"] == "error"

def test_hook_non_utf8_output_fails_open_not_crash(env, monkeypatch):
    cmd = "printf '\\377\\376' >&2; exit 2"
    set_hooks(monkeypatch, post=[("write_file", cmd)])
    _, messages, log = _one_write(env)
    r = messages[2]["content"][0]
    assert (agent.WORKDIR / "a.txt").read_text() == "1" and not r.get("is_error") and "[post_tool_use hook 回報]" in r["content"]
    check_pairing(messages)
    assert hook_rows(log)[0]["hook"]["decision"] == "block"   # 解碼用 replace，exit 2 照常算

def test_hook_surrogate_payload_does_not_crash(env, monkeypatch):
    out = env / "in.json"
    set_hooks(monkeypatch, pre=[(".*", hook_cmd(env, "s", f"open({str(out)!r}, 'w').write(sys.stdin.read())"))])
    assert agent.run_hooks("pre_tool_use", "read_file", {"path": "a\udc80"}, env / "l", 1) == []
    assert json.loads(out.read_text())["input"]["path"] == "a\udc80"
    assert hook_rows(env / "l")[0]["hook"]["decision"] == "ok"

def test_hook_huge_stderr_truncated_in_pre_and_post(env, monkeypatch):
    big = "print('X' * 500000, file=sys.stderr); sys.exit(2)"
    set_hooks(monkeypatch, pre=[("write_file", hook_cmd(env, "bp", big))])
    _, messages, _ = _one_write(env)
    c = messages[2]["content"][0]["content"]
    assert messages[2]["content"][0]["is_error"] and len(c) < agent.HOOK_FEEDBACK_CHARS + 200 and "截斷" in c
    check_pairing(messages)
    set_hooks(monkeypatch, post=[("write_file", hook_cmd(env, "bq", big))])
    _, messages, _ = _one_write(env)
    c = messages[2]["content"][0]["content"]
    assert (agent.WORKDIR / "a.txt").exists() and len(c) < agent.HOOK_FEEDBACK_CHARS + 400 and "截斷" in c and "[post_tool_use hook 回報]" in c

def test_hooks_file_relative_resolves_against_root_not_cwd(env, monkeypatch):
    monkeypatch.setenv("HOOKS_FILE", "rel-hooks.json")
    import importlib
    try:
        m = importlib.reload(agent)
        assert m.HOOKS_FILE == m.ROOT / "rel-hooks.json"
        monkeypatch.setenv("HOOKS_FILE", "/abs/h.json")
        assert importlib.reload(agent).HOOKS_FILE == Path("/abs/h.json")
    finally:
        monkeypatch.delenv("HOOKS_FILE")
        importlib.reload(agent)

def test_hook_log_records_input_path(env, monkeypatch):
    set_hooks(monkeypatch, pre=[(".*", "true")])
    agent.run_hooks("pre_tool_use", "write_file", {"path": "a.py"}, env / "l", 1)
    agent.run_hooks("pre_tool_use", "bash", {"command": "ls"}, env / "l", 1)
    assert [r["hook"]["path"] for r in hook_rows(env / "l")] == ["a.py", None]

def test_summarize_hooks_uses_own_path_and_normalizes(tmp_path):
    sl = import_summarize()
    rows = [{"turn": 1, "hook": {"event": "post_tool_use", "tool": "edit", "path": "./a.py", "exit": 2, "ms": 1}},
            {"turn": 2, "hook": {"event": "post_tool_use", "tool": "edit", "path": "a.py", "exit": 0, "ms": 1}}]   # 沒有 permission 行
    log = tmp_path / "p.jsonl"; log.write_text("\n".join(json.dumps(r) for r in rows))
    res, _ = sl.judge_hooks(log, tmp_path)
    assert res[1][0] is True and "a.py" in res[1][1]
