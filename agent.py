"""v1 迷你 coding agent：手寫 agent loop + 6 個工具（read_file/write_file/edit/grep/glob/bash）。不用框架、不用 SDK tool runner。"""
import fnmatch, json, os, re, subprocess, sys
from datetime import datetime
from pathlib import Path

import anthropic   # import 本身不需要 key；client 在 main 才建立

# 放在最上面，方便實驗：拿掉一段，看行為怎麼變。
SYSTEM_PROMPT = """你是一個在 WORKDIR 資料夾裡工作的 coding agent。
你有六個工具：read_file、write_file、edit、grep、glob、bash。所有相對路徑與 bash 的工作目錄都是 WORKDIR。
先想清楚要做什麼，再動手。工作順序：不熟的程式碼先用 glob/grep 找位置，再 read_file 讀相關段落，再用 edit 做最小修改，最後用 bash 驗證。
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
MAX_RESULTS = 200   # grep/glob 的筆數上限，避免一次搜尋灌爆 context
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}   # 這些目錄的內容對模型只是雜訊

# ==== 【harness 角色：工具定義（給模型看的說明書）】模型只看得到這些文字，描述好壞直接決定它用得對不對 ====
TOOLS = [
    {"name": "read_file",
     "description": "讀取 WORKDIR 內一個文字檔，每行前面會加上行號（格式「行號<tab>內容」）。"
                    "行號只是顯示用，不是檔案內容的一部分，之後用 edit 時絕對不要把它抄進 old_string。"
                    "一次最多約 2000 行或 1 萬字元，以先到者為準；沒讀完時結尾會提示讀到第幾行，照提示用 offset 繼續讀。"
                    "路徑為相對於 WORKDIR 的相對路徑。檔案不存在、不是 UTF-8 文字檔、offset 超出檔尾會回傳錯誤。",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "相對於 WORKDIR 的檔案路徑"},
                                     "offset": {"type": "integer", "description": "從第幾行開始讀（從 1 起算），預設 1"},
                                     "limit": {"type": "integer", "description": "最多讀幾行，預設 2000（字元數先到也會停）"}},
                      "required": ["path"]}},
    {"name": "write_file",
     "description": "建立新檔，或確實需要整檔重寫時使用（整檔取代，不是局部修改）。"
                    "修改既有檔案請用 edit，不要用它整檔覆寫。必要時會自動建立上層資料夾。"
                    "寫完後回傳寫入的 bytes 數。寫程式後請用 bash 實際執行來驗證。",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "相對於 WORKDIR 的檔案路徑"},
                                     "content": {"type": "string", "description": "要寫入的完整檔案內容"}},
                      "required": ["path", "content"]}},
    {"name": "edit",
     "description": "精確字串替換，用來修改既有檔案（優先於 write_file）。"
                    "old_string 必須與檔案內容逐字相符（含縮排與換行），且不要包含 read_file 顯示的行號前綴。"
                    "old_string 必須在檔案中唯一；出現多次時請加入前後幾行上下文，或設 replace_all=true 全部替換。"
                    "成功後回傳修改處附近的片段，請確認改對了；改完用 bash 驗證。",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "相對於 WORKDIR 的檔案路徑"},
                                     "old_string": {"type": "string", "description": "要被取代的原文（逐字、不含行號）"},
                                     "new_string": {"type": "string", "description": "取代後的新文字，必須與 old_string 不同"},
                                     "replace_all": {"type": "boolean", "description": "是否替換所有出現處，預設 false"}},
                      "required": ["path", "old_string", "new_string"]}},
    {"name": "grep",
     "description": "用正規表達式（Python re）在檔案內容中搜尋，不熟悉的程式碼先用它找位置。"
                    "遞迴搜尋 path 下所有文字檔（path 也可以是單一檔案），自動跳過 .git、.venv、__pycache__、node_modules。"
                    "輸出每行「相對路徑:行號: 內容」，最多 200 筆。沒找到會回傳「沒有符合的結果」，path 不存在會回傳錯誤。",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string", "description": "正規表達式"},
                                     "path": {"type": "string", "description": "搜尋的資料夾或檔案，預設 \".\"（WORKDIR）"},
                                     "glob": {"type": "string", "description": "只搜尋符合的檔案；同時比對檔名與相對路徑，所以 *.py 與 **/*.py 都能用，也可寫 shop/*.py"}},
                      "required": ["pattern"]}},
    {"name": "glob",
     "description": "依檔名樣式列出檔案，回傳相對於 WORKDIR 的排序路徑，只列檔案、最多 200 筆。"
                    "注意：*.py 只找 WORKDIR 根目錄；要遞迴所有子資料夾請用 **/*.py。"
                    "想知道專案有哪些檔案、結構長怎樣時使用；要找檔案「內容」請用 grep。",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string", "description": "glob 樣式，例如 **/*.py 或 src/*.js"}},
                      "required": ["pattern"]}},
    {"name": "bash",
     "description": "在 WORKDIR 執行一行 shell 指令（例如 python hello.py、ls、pytest）。"
                    "回傳 exit code、stdout、stderr。exit code 非 0 只代表指令失敗，不是工具壞掉，請讀 stderr 判斷原因。"
                    "每次呼叫彼此獨立，不會保留 cd 或環境變數。逾時（預設 30 秒）會被終止並回傳錯誤。"
                    # v1 對照實驗 B：只加這一句，看模型是否改用專用工具（A 組 log：logs/v1-A-baseline.jsonl）
                    "bash 只用來執行程式、跑測試、安裝套件。不要用 bash 讀檔或搜尋："
                    "讀檔用 read_file（不要 cat/head/tail），搜尋內容用 grep 工具（不要 grep/rg），"
                    "找檔案用 glob（不要 find/ls -R），改檔用 edit（不要 sed -i）。",
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

def _rel(p):
    return p.relative_to(WORKDIR).as_posix()

def _numbered(lines, start):
    # 與 cat -n 同格式；行號只給人和模型定位用，不是檔案內容
    return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(lines, start))

def _files(base):
    # 遞迴列出檔案，並在走進去之前就剔除 SKIP_DIRS（省時間，也省 context）
    if base.is_file():
        yield base
        return
    for root, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            yield Path(root) / n

def _read_text(p):
    # newline="" 保留原本的 CRLF；用 read_text() 會把 \r\n 悄悄換成 \n，edit 寫回就改掉整個檔的換行
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()

def _read_for(path, action):
    try:
        return _read_text(_path(path))
    except UnicodeDecodeError:
        raise ValueError(f"{path} 不是 UTF-8 文字檔，無法{action}") from None

def read_file(path, offset=1, limit=2000):
    lines = _read_for(path, "讀取").splitlines()
    total = len(lines)
    if total == 0:
        return "(空檔案)"
    if limit < 1:
        raise ValueError("limit 必須 >= 1")
    start = max(offset, 1)
    if start > total:
        raise ValueError(f"offset 超出檔尾，檔案共 {total} 行")
    # 自己控制大小、在行邊界停下：否則 execute_tool 的通用截斷會從中間砍，
    # 模型看到斷在半行的內容，也不知道該從哪一行接著讀。留 500 字元給結尾提示。
    budget, rows, used = MAX_TOOL_OUTPUT_CHARS - 500, [], 0
    for n, line in enumerate(lines[start - 1:start - 1 + limit], start):
        row = f"{n:>6}\t{line}"
        if rows and used + len(row) + 1 > budget:
            break
        rows.append(row[:budget])   # 單行本身就超長時才會被切（至少要讀出一行）
        used += len(row) + 1
    last = start + len(rows) - 1
    out = "\n".join(rows)
    if last < total:
        out += f"\n[... 讀到第 {last} 行，檔案共 {total} 行，用 offset={last + 1} 繼續讀 ...]"
    return out

def write_file(path, content):
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content.encode())} bytes to {path}"

def edit(path, old_string, new_string, replace_all=False):
    # 要求 old_string 唯一：逼模型先讀過檔案、帶足上下文，才不會改到錯的地方
    p = _path(path)
    text = _read_for(path, "編輯")
    if old_string == new_string:
        raise ValueError("old_string 與 new_string 相同，沒有東西可改")
    if "\r\n" in text:   # 模型看到的行是沒有 \r 的，多行字串要換成檔案原本的換行才對得上
        old_string, new_string = (x.replace("\r\n", "\n").replace("\n", "\r\n") for x in (old_string, new_string))
    n = text.count(old_string) if old_string else 0
    if n == 0:
        raise ValueError("找不到 old_string。請先 read_file 確認原文（注意縮排、不要包含行號前綴）")
    if n > 1 and not replace_all:
        raise ValueError(f"old_string 出現 {n} 次，請加入更多上下文讓它唯一，或設 replace_all=true")
    first = text.index(old_string)
    new_text = text.replace(old_string, new_string)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    start = new_text[:first].count("\n") + 1                  # 第一處修改的起始行
    end = start + new_string.count("\n")
    lo, lines = max(start - 3, 1), new_text.splitlines()
    return f"已替換 {n if replace_all else 1} 處，{path} 修改處片段：\n{_numbered(lines[lo - 1:end + 3], lo)}"

def grep(pattern, path=".", glob=None):
    rx = re.compile(pattern)   # 不合法的 regex 會丟 re.error，由 execute_tool 轉成 is_error
    base = _path(path)
    if not base.exists():
        raise ValueError(f"路徑不存在: {path}")
    hits = []
    for f in _files(base):
        # 同時比對檔名與相對路徑：模型常寫 *.py，也常寫 **/*.py，兩種都該能用
        if glob and not any(fnmatch.fnmatch(x, g) for x in (f.name, _rel(f)) for g in (glob, glob.removeprefix("**/"))):
            continue
        try:
            lines = _read_text(f).splitlines()
        except UnicodeDecodeError:
            continue   # 二進位檔
        hits += [f"{_rel(f)}:{n}: {l}" for n, l in enumerate(lines, 1) if rx.search(l)]
    if not hits:
        return "沒有符合的結果"
    out = "\n".join(hits[:MAX_RESULTS])
    return out + (f"\n[... 共 {len(hits)} 筆，只顯示前 {MAX_RESULTS} 筆 ...]" if len(hits) > MAX_RESULTS else "")

def glob(pattern):
    found = sorted(_rel(f) for f in WORKDIR.glob(pattern)
                   if f.is_file() and not SKIP_DIRS & set(f.relative_to(WORKDIR).parts))
    if not found:
        return "沒有符合的檔案"
    out = "\n".join(found[:MAX_RESULTS])
    return out + (f"\n[... 共 {len(found)} 筆，只顯示前 {MAX_RESULTS} 筆 ...]" if len(found) > MAX_RESULTS else "")

def bash(command, timeout=30):
    # 子程序拿不到 ANTHROPIC_*：否則模型一句 `echo $ANTHROPIC_API_KEY` 就會把 key 帶進對話和 log
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    r = subprocess.run(command, shell=True, cwd=WORKDIR, env=env, capture_output=True, text=True, timeout=timeout)
    return f"exit code: {r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"

TOOL_FUNCS = {"read_file": read_file, "write_file": write_file, "edit": edit,
              "grep": grep, "glob": glob, "bash": bash}

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
        request = {k: kwargs[k] for k in ("model", "system", "tools", "messages")}
        try:
            response = client.beta.messages.create(**kwargs)
        except (anthropic.APIError, TypeError) as e:
            # SDK 沒憑證時丟的是 TypeError 而非 APIError；只接認證那種，其他 TypeError 是真 bug，照拋
            if isinstance(e, TypeError) and "authentication" not in str(e):
                raise
            # 失敗也留一行 log，之後回頭看才知道這次為什麼沒有紀錄
            with open(log, "a") as f:
                line = {"turn": turn, "request": request, "error": {"type": type(e).__name__, "message": str(e)}}
                f.write(json.dumps(line, ensure_ascii=False, default=to_jsonable) + "\n")
            if isinstance(e, TypeError):
                print("[認證錯誤] 找不到可用的 API 憑證，請確認這個終端機已 export ANTHROPIC_API_KEY")
            else:   # 429／529／網路錯誤：印出後回 REPL，歷史尾端是 user，下一句可併進去
                print(f"[API 錯誤] {type(e).__name__}: {e}")
            return
        with open(log, "a") as f:
            line = {"turn": turn, "request": request,
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
                summary = block.input.get("command") or block.input.get("pattern") or block.input.get("path") or ""
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

def load_env(path=Path(__file__).parent / ".env"):
    # 專案層級的設定檔：KEY=VALUE 一行一個。終端機已 export 的值優先（setdefault），方便臨時覆寫
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

def main():
    load_env()
    # 環境變數不會在終端機視窗之間共用，新開的視窗常忘了 export；在 SDK 丟出一長串 traceback 前先擋下
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("找不到 API key：請在專案的 .env 寫入 ANTHROPIC_API_KEY=...（見 .env.example），或在這個終端機 export")
        sys.exit(1)
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
