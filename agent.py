"""v0 迷你 coding agent：手寫 agent loop + 3 個工具。不用框架、不用 SDK tool runner。"""
import json, os, subprocess, sys
from datetime import datetime
from pathlib import Path

import anthropic   # import 本身不需要 key；client 在 main 才建立

# 放在最上面，方便實驗：拿掉一段，看行為怎麼變。
SYSTEM_PROMPT = """你是一個在 WORKDIR 資料夾裡工作的 coding agent。
你有三個工具：read_file、write_file、bash。所有相對路徑與 bash 的工作目錄都是 WORKDIR。
先想清楚要做什麼，再動手；需要時先讀檔看現況。
改完或寫完程式後，一定要實際執行（用 bash）驗證結果，不要只憑推測說「應該可以」。
工具回傳錯誤時，讀懂錯誤訊息並修正，不要重複同一個失敗的動作。
完成後用簡短的話回報你做了什麼、驗證結果如何。"""

# ==== 【harness 角色：設定】用環境變數決定模型、輪數上限、工作目錄，方便不改程式做實驗 ====
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("EFFORT", "medium")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "25"))   # 防模型卡在同一個錯誤無限重試
USE_FALLBACKS = os.environ.get("USE_FALLBACKS", "1") == "1"
# v2 權限層之前，bash 只准在這個丟棄式資料夾跑（注意：絕對路徑仍能跑出去，這裡只是預設 cwd）
WORKDIR = Path(os.environ.get("WORKDIR", "./sandbox")).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = Path("logs")
MAX_TOOL_OUTPUT_CHARS = 10000

# ==== 【harness 角色：工具定義（給模型看的說明書）】模型只看得到這些文字，描述好壞直接決定它用得對不對 ====
TOOLS = [
    {"name": "read_file",
     "description": "讀取 WORKDIR 內一個文字檔的完整內容。要看現有程式碼、設定或某個指令產生的檔案時使用。"
                    "路徑可為相對於 WORKDIR 的相對路徑。檔案不存在或不是文字檔會回傳錯誤。過長的內容會被截斷。",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "相對於 WORKDIR 的檔案路徑"}},
                      "required": ["path"]}},
    {"name": "write_file",
     "description": "建立或完全覆寫一個文字檔（整檔取代，不是局部修改）。必要時會自動建立上層資料夾。"
                    "寫完後回傳寫入的 bytes 數。寫程式後請用 bash 實際執行來驗證。",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "相對於 WORKDIR 的檔案路徑"},
                                     "content": {"type": "string", "description": "要寫入的完整檔案內容"}},
                      "required": ["path", "content"]}},
    {"name": "bash",
     "description": "在 WORKDIR 執行一行 shell 指令（例如 python hello.py、ls、pytest）。"
                    "回傳 exit code、stdout、stderr。exit code 非 0 只代表指令失敗，不是工具壞掉，請讀 stderr 判斷原因。"
                    "每次呼叫彼此獨立，不會保留 cd 或環境變數。逾時（預設 30 秒）會被終止並回傳錯誤。",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string", "description": "要執行的 shell 指令"},
                                     "timeout": {"type": "integer", "description": "逾時秒數，預設 30"}},
                      "required": ["command"]}},
]

# ==== 【harness 角色：輸出截斷】工具輸出會進 context，太長會吃光窗口，所以保留頭尾 ====
def truncate(text, limit=None):
    limit = limit or MAX_TOOL_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... 截斷了 {len(text) - 2 * half} 個字元 ...]\n{text[-half:]}"

# ==== 【harness 角色：工具執行器】模型說「我要呼叫 X」，真正動手的是這裡 ====
def _path(p):
    return WORKDIR / p   # 絕對路徑會直接覆蓋 WORKDIR（Path 的行為）；v2 權限層才會擋

def read_file(path):
    return _path(path).read_text()

def write_file(path, content):
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content.encode())} bytes to {path}"

def bash(command, timeout=30):
    r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=timeout)
    return f"exit code: {r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"

TOOL_FUNCS = {"read_file": read_file, "write_file": write_file, "bash": bash}

def execute_tool(name, args):
    # 錯誤一律回傳成 tool_result（is_error=True），不丟例外：
    # 例外會讓整個 loop 死掉；回傳給模型，它才看得到錯誤並自己修正。
    try:
        if name not in TOOL_FUNCS:
            return {"content": f"Error: unknown tool '{name}'", "is_error": True}
        return {"content": truncate(str(TOOL_FUNCS[name](**args)))}
    except subprocess.TimeoutExpired:
        return {"content": f"Error: command timed out after {args.get('timeout', 30)}s", "is_error": True}
    except Exception as e:  # 參數錯（TypeError）、檔案不存在等都走這裡
        return {"content": truncate(f"Error: {type(e).__name__}: {e}"), "is_error": True}

# ==== 【harness 角色：紀錄】每輪 request/response 存成 jsonl，事後回放是最好的學習材料 ====
def to_jsonable(o):
    # messages 裡混有 SDK 物件（pydantic），json 不認得，統一轉成 dict
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"cannot serialize {type(o)}")

def new_log_path():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

# ==== 【harness 角色：request 組裝】把 system、tools、歷史打包成一次 API 呼叫 ====
def build_request(messages):
    kwargs = dict(model=MODEL, max_tokens=16000, system=SYSTEM_PROMPT, tools=TOOLS,
                  messages=messages, output_config={"effort": EFFORT})
    if USE_FALLBACKS:
        # SDK 1.14.0 的 BetaFallbacksParam 接受 Literal["default"]，所以直接傳字串，不需要 extra_body
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    # 刻意不送：thinking disabled / budget_tokens / temperature / 強制 tool_choice / assistant prefill
    # （claude-opus-5-5 都不接受）
    return kwargs

# ==== 【harness 角色：agent loop】呼叫模型 → 看 stop_reason → 執行工具 → 寫回結果 → 再來一輪 ====
def _field(b, key):
    return b.get(key) if isinstance(b, dict) else getattr(b, key, None)   # 區塊可能是 SDK 物件或 dict

def close_dangling(messages):
    # 保證歷史合法：每個 tool_use 都有 tool_result、沒有空訊息。
    # API 對這兩點直接回 400，而且歷史一壞，之後每次輸入都會失敗；所以任何結束路徑都先收尾。
    if not messages or messages[-1]["role"] != "assistant":
        return
    content = messages[-1]["content"]
    if not content:
        messages.pop()
        return
    ids = [_field(b, "id") for b in content if _field(b, "type") == "tool_use"]
    if ids:
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": "Error: 工具未執行（回應被截斷或中斷）",
             "is_error": True} for i in ids]})

def run_turn(client, messages, log):
    try:
        _loop(client, messages, log)
    finally:
        close_dangling(messages)   # return、例外、Ctrl-C 都會經過這裡

def _loop(client, messages, log):
    for turn in range(1, MAX_TURNS + 1):
        kwargs = build_request(messages)
        try:
            response = client.beta.messages.create(**kwargs)
        except anthropic.APIError as e:   # 429／529／網路錯誤：印出後回 REPL，歷史尾端是 user，下一句可併進去
            print(f"[API 錯誤] {type(e).__name__}: {e}")
            return
        with open(log, "a") as f:
            line = {"turn": turn,
                    "request": {k: kwargs[k] for k in ("model", "system", "tools", "messages")},
                    "response": response.model_dump(mode="json")}
            f.write(json.dumps(line, ensure_ascii=False, default=to_jsonable) + "\n")
        # 整包 append（含 thinking 等區塊）：歷史只能往後加、不可改寫，
        # 下一輪模型才看得到自己上一輪完整的推理與 tool_use。
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "text":
                print(block.text)

        reason = response.stop_reason
        if reason == "tool_use":
            results = []   # 本則回應的「所有」tool_use 都要執行，結果放進「同一則」user 訊息
            for block in response.content:
                if block.type != "tool_use":
                    continue
                summary = block.input.get("command") or block.input.get("path") or ""
                print(f"→ {block.name}: {summary}")
                result = execute_tool(block.name, block.input)
                print(f"  {result['content'][:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, **result})
            messages.append({"role": "user", "content": results})
        elif reason == "end_turn":
            return
        elif reason == "max_tokens":
            print("[警告] 回應被 max_tokens 截斷")
            return
        elif reason == "refusal":
            print(f"[拒絕] stop_details: {getattr(response, 'stop_details', None)}")
            return
        elif reason == "pause_turn":
            # 目前沒有 server tool，這個分支 v0 走不到；
            # 留著是因為加入 server tool 後要原樣再送一輪
            continue
        else:
            print(f"[未處理的 stop_reason] {reason}")
            return
    print(f"[達到最大輪數 {MAX_TURNS}]")

# ==== 【harness 角色：REPL】接使用者輸入並維持歷史；不能破壞 tool_use/tool_result 配對 ====
def add_user_text(messages, text):
    block = {"type": "text", "text": text}
    if messages and messages[-1]["role"] == "user":
        # 上次因 MAX_TURNS 停在 tool_result：附加進同一則，避免連續兩則 user，且 tool_result 仍緊跟 tool_use
        last = messages[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append(block)
    else:
        messages.append({"role": "user", "content": [block]})

def main():
    client = anthropic.Anthropic()   # 在這裡才建立，沒有 API key 時 import agent 不會爆
    log, messages = new_log_path(), []
    if len(sys.argv) > 1:
        add_user_text(messages, " ".join(sys.argv[1:]))
        try:
            return run_turn(client, messages, log)
        except KeyboardInterrupt:
            return print("[中斷]")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print()
            continue
        if text == "/exit":
            break
        if text == "/reset":
            messages.clear()
            continue
        if text:
            add_user_text(messages, text)
            try:
                run_turn(client, messages, log)
            except KeyboardInterrupt:
                print("[中斷]")

if __name__ == "__main__":
    main()
