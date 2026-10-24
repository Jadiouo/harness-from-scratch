"""v4 迷你 coding agent：手寫 agent loop + 6 個工具（read_file/write_file/edit/grep/glob/bash）+ 權限層 + 上下文壓縮 + 專案記憶（CLAUDE.md）。不用框架、不用 SDK tool runner。"""
import fnmatch, json, os, re, subprocess, sys
from datetime import datetime
from pathlib import Path

import anthropic   # import 本身不需要 key；client 在 main 才建立

# 刻意不在這裡寫「工具被拒絕時要換方法」：v2 要觀察模型光靠 tool_result 的「被拒絕」回饋能不能自己調整。
# v3 同理：刻意不提示「context 可能被壓縮」，要觀察光靠摘要本身夠不夠讓它接續。
# 放在最上面，方便實驗：拿掉一段，看行為怎麼變。
SYSTEM_PROMPT = """你是一個在 WORKDIR 資料夾裡工作的 coding agent。
你有六個工具：read_file、write_file、edit、grep、glob、bash。所有相對路徑與 bash 的工作目錄都是 WORKDIR。
先想清楚要做什麼，再動手。工作順序：不熟的程式碼先用 glob/grep 找位置，再 read_file 讀相關段落，再用 edit 做最小修改，最後用 bash 驗證。
改完或寫完程式後，一定要實際執行（用 bash）驗證結果，不要只憑推測說「應該可以」。
工具回傳錯誤時，讀懂錯誤訊息並修正，不要重複同一個失敗的動作。
完成後用簡短的話回報你做了什麼、驗證結果如何。
WORKDIR/CLAUDE.md 是這個專案的記憶，若有內容會附在下面；學到之後的 session 也該知道的事（例如環境限制），可以提議寫進去。"""

# ==== 【harness 角色：設定】用環境變數決定模型、輪數上限、工作目錄，方便不改程式做實驗 ====
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("EFFORT", "medium")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "60"))   # 防模型卡在同一個錯誤無限重試；30 次以上工具呼叫的任務需要更多輪（v2 是 25）
# 故意設低讓測驗觸發：fixture 的 context 只有約 6.8k 字元，要能觸發 2 次以上壓縮；真實 harness 會等到接近模型 context 上限才壓縮
COMPACT_AT_TOKENS = int(os.environ.get("COMPACT_AT_TOKENS", "8000"))
COMPACT_COOLDOWN_TURNS = 3   # 摘要失敗後這麼多輪不再自動重試；否則 usage 一直超標，每輪都多付一次失敗的摘要請求
USE_FALLBACKS = os.environ.get("USE_FALLBACKS", "1") == "1"
# bash 的 cwd 是這個丟棄式資料夾（bash 本身仍能用絕對路徑跑出去，見權限層的說明）；檔案類工具的路徑由權限層限制在這裡面
WORKDIR = Path(os.environ.get("WORKDIR", "./sandbox")).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = Path("logs")
MAX_TOOL_OUTPUT_CHARS = 10000
PERMISSION_MODE = os.environ.get("PERMISSION_MODE", "ask").strip().lower()   # ask（預設）或 allow_all（等同 --dangerously-skip-permissions）
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
    return WORKDIR / p   # 絕對路徑會直接覆蓋 WORKDIR（Path 的行為）；逃出 WORKDIR 由 check_permission 在執行前擋下

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
    for root, dirs, names in os.walk(base):   # followlinks 預設 False：不會走進 symlink 目錄
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            if not (Path(root) / n).is_symlink():   # symlink 檔可能指到 WORKDIR 外，直接略過
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
                   if f.is_file() and _inside(f) and not SKIP_DIRS & set(f.relative_to(WORKDIR).parts))
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

# ==== 【harness 角色：權限層】工具執行「之前」的守門員：allow 直接做、ask 問使用者、deny 直接拒絕 ====
# 注意：bash 沒有被真正隔離，這裡只是字串判斷，不是安全邊界（例如使用者核准過的腳本可以做任何事）；
# 真正的隔離要靠 OS 層 sandbox（Claude Code 有，我們沒做）。
# 設計原則：不列舉危險指令（列不完），只放行「一眼看得出安全」的，其餘一律 ask。
# 能跑腳本就能做任何事，字串規則擋不住，所以 python／pytest／find／env 這類「執行程式」的指令一律 ask。
SAFE_COMMANDS = {"pwd", "ls", "cat", "head", "tail", "wc", "echo", "grep", "diff", "cd", "true", "which"}
SAFE_SEGMENT = re.compile(r"[A-Za-z0-9 _./=:,+-]*")   # 一段指令只准有這些字元：引號、$、*、~、重導向、換行、非 ASCII 全部不在內
SESSION_ALLOW = set()   # 使用者回 a 的記錄：檔案工具記工具名，bash 記 ("bash", 完全相同的指令)。模組層級、/reset 不清
PROMPT = input          # 抽成變數，測試才能替換掉真正的鍵盤輸入

def _inside(p):
    return (WORKDIR / p).resolve().is_relative_to(WORKDIR.resolve())   # 含絕對路徑與 ..；resolve 也會跟著 symlink

def _bash_rule(cmd):
    # 只用 && || ; | 切段；切完每段必須只含安全字元（單一 & 因此也會 ask）。引號內的分隔符會被誤切，結果只是 ask
    for seg in re.split(r"&&|\|\||;|\|", cmd):
        if not SAFE_SEGMENT.fullmatch(seg):
            return "ask", "含引號、變數、萬用字元、重導向、換行或非 ASCII 字元，無法一眼判斷安全"
        w = seg.split()
        if not w or w[0] not in SAFE_COMMANDS:
            return "ask", f"不在白名單內的指令：{w[0] if w else '(空)'}"
        for t in w[1:]:
            # 把 --opt=/x、a:b 也拆開看；絕對路徑或 .. 片段都可能跑出 WORKDIR
            if any(x.startswith("/") or ".." in x.split("/") for x in re.split(r"[=:,]", t)):
                return "ask", f"路徑可能在工作目錄之外：{t}"
        if w[0] == "cd" and len(w) != 2:
            return "ask", "cd 只允許一個相對路徑參數"
    return "allow", "白名單內的指令"

def _session_key(name, args):
    return ("bash", str(args.get("command", ""))) if name == "bash" else name   # bash 只記完全相同的指令

def check_permission(name, args):
    """回傳 (decision, reason)，decision 是 "allow" / "ask" / "deny"。"""
    # 1. 路徑限制永遠先查，allow_all 也繞不過
    if name == "glob":
        pat = str(args.get("pattern", ""))
        if os.path.isabs(pat) or ".." in Path(pat).parts:
            return "deny", f"路徑在工作目錄之外：{pat}"
    elif name in ("read_file", "write_file", "edit", "grep"):
        p = str(args.get("path", "."))
        if not _inside(p):
            return "deny", f"路徑在工作目錄之外：{p}"
    # 2. 規則
    if name in ("read_file", "grep", "glob"):
        return "allow", "唯讀工具"
    if PERMISSION_MODE == "allow_all":
        return "allow", "PERMISSION_MODE=allow_all"
    decision, reason = ("ask", "會修改檔案") if name in ("write_file", "edit") else \
                       _bash_rule(str(args.get("command", ""))) if name == "bash" else ("allow", "未知工具，交給執行器回報錯誤")
    if decision == "ask" and _session_key(name, args) in SESSION_ALLOW:
        return "allow", "本 session 已選過「允許」"
    return decision, reason

def ask_user(name, args, reason):
    """問使用者，回傳 (答案 y/n/a, 拒絕理由)。"""
    print(f"\n🔐 {name} 需要你的許可（{reason}）")
    if name == "bash":
        print(f"  command: {args.get('command')}")
    elif name == "edit":
        print(f"  path: {args.get('path')}\n  old: {str(args.get('old_string'))[:200]}\n  new: {str(args.get('new_string'))[:200]}")
    elif name == "write_file":
        print(f"  path: {args.get('path')}\n  內容前 10 行：\n" + "\n".join(str(args.get("content", "")).splitlines()[:10]))
    ans = None
    a_text = "[a]本次 session 不再詢問這條完全相同的指令" if name == "bash" else "[a]本次 session 都允許 這個工具（仍限工作目錄內）"
    while ans not in ("y", "n", "a"):
        try:
            ans = PROMPT(f"允許嗎？[y]是 / [n]否 / {a_text} > ").strip().lower()
        except EOFError:
            ans = "n"   # 沒有人可以問，保守當作拒絕
    note = ""
    if ans == "n":
        try:
            note = PROMPT("拒絕理由（可留空）> ").strip()
        except EOFError:
            pass
    if ans == "a":
        SESSION_ALLOW.add(_session_key(name, args))
    return ans, note

def authorize(block):
    """回傳 (被拒絕時要送回的 tool_result 或 None, 給 log 的 permission 記錄)。"""
    name, args = block.name, block.input
    try:
        decision, reason = check_permission(name, args)
    except Exception as e:   # 例如路徑含 NUL 會讓 resolve 丟 ValueError：判斷不了就拒絕，不要讓 REPL 崩潰
        decision, reason = "deny", f"參數無法判斷（{e}）"
    answer, result = None, None
    if decision == "deny":
        print(f"⛔ 權限層拒絕：{reason}")
        result = {"content": f"權限層拒絕：{reason}", "is_error": True}
    elif decision == "ask":
        answer, note = ask_user(name, args, reason)
        if answer == "n":
            print("🚫 已拒絕")
            reason += f"；使用者理由：{note}" if note else ""
            # 拒絕也要回成 tool_result：tool_use 一定要有配對的結果，而且模型要看到回饋才能改走別的路
            result = {"content": f"使用者拒絕了這個操作（{name}）。理由：{note or '未提供'}。這個操作沒有被執行。",
                      "is_error": True}
        else:
            print("✅ 已允許")
    return result, {"id": block.id, "tool": name, "input": args, "decision": decision, "answer": answer, "reason": reason}

# ==== 【harness 角色：紀錄】每輪 request/response 存成 jsonl，事後回放是最好的學習材料 ====
def to_jsonable(o):
    # messages 裡混有 SDK 物件（pydantic），json 不認得，統一轉成 dict
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"cannot serialize {type(o)}")

def new_log_path():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

# ==== 【harness 角色：專案記憶（CLAUDE.md）】啟動時讀 WORKDIR/CLAUDE.md，附在 system prompt 後面，讓模型跨 session 記得環境限制 ====
# 只讀 WORKDIR 這一個檔、不往上層找：專案根目錄的 CLAUDE.md 是給開發者的 Claude Code 用的，不是給這個 agent。
# symlink 解析後若跑出 WORKDIR 也拒載：否則 CLAUDE.md -> 外部檔會把外部內容送上 API（權限層管不到這條路）。
MAX_MEMORY_CHARS = 4000   # 記憶每輪都會重送，不設上限會悄悄吃掉 context

def load_memory():
    """回傳 CLAUDE.md 內容；不存在或讀不了回 None（讀不了會印警告）。"""
    p = WORKDIR / "CLAUDE.md"
    if not p.is_file():
        return None
    if not _inside(p):
        print(f"[警告] {WORKDIR.name}/CLAUDE.md 指向 WORKDIR 外面，不載入")
        return None
    try:
        text = _read_text(p)
    except (UnicodeDecodeError, OSError) as e:
        print(f"[警告] 讀不了 {WORKDIR.name}/CLAUDE.md（{type(e).__name__}），這個 session 不載入記憶")
        return None
    if not text.strip():   # 空白檔等於沒有，免得 system 多一個空的記憶標題
        return None
    if len(text) > MAX_MEMORY_CHARS:
        print(f"[警告] {WORKDIR.name}/CLAUDE.md 超過 {MAX_MEMORY_CHARS} 字，已截斷")
        text = text[:MAX_MEMORY_CHARS] + "\n[... CLAUDE.md 過長，已截斷 ...]"
    return text

def build_system(memory):
    if not memory:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + "\n\n# 專案記憶（WORKDIR/CLAUDE.md，session 開始時載入）\n" + memory

# session 開始時組一次、之後凍結（main 設定；沒經過 main 的測試用預設值，行為和 v3 一樣）。
# 為什麼凍結：thinking 區塊的簽章綁定「system＋tools＋之前所有訊息」，session 中途重組 system 會 400（v3 摘要請求真實踩到）。
# 為什麼放 system 而不是第一則 user 訊息：compaction 會把歷史換掉，放在 system 才會自動保留。
# 代價：session 中途改 CLAUDE.md 要下個 session 才生效。/reset 也不重讀（system 屬於 session，不屬於對話）。
SESSION_SYSTEM = SYSTEM_PROMPT
SESSION_MEMORY = None   # /memory 顯示用

# ==== 【harness 角色：request 組裝】把 system、tools、歷史打包成一次 API 呼叫 ====
def build_request(messages):
    kwargs = dict(model=MODEL, max_tokens=16000, system=SESSION_SYSTEM, tools=TOOLS,
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

# ==== 【harness 角色：上下文管理（compaction）】歷史每輪都整段重送，會越來越長；超過門檻就把整段歷史換成「摘要＋使用者原文」 ====
# 不採「清掉舊 tool_result」：歷史只能往後加（thinking 區塊綁定整段對話，改寫舊訊息可能讓它們失效而 400）。
# 所以做法是整段換掉：新對話不含任何舊的 assistant/thinking 區塊。
LAST_USAGE = None   # 最近一次回應的 usage；跨 run_turn 保留，/tokens 與壓縮判斷都看它
COMPACT_COOLDOWN = 0   # 還要略過幾輪自動壓縮；/compact 手動不看它，/reset 清掉

# 壓縮後那則 user 訊息由多個 text 區塊組成，靠固定前綴辨認。
# 使用者原文一段一塊、逐字保留：下次壓縮時 user_texts 才能分得出「原文」與「上次的摘要」，摘要不會疊層、原文不會被改寫。
HEADER_PREFIX = "【context 已壓縮】"
USER_PREFIX = "【使用者原文】\n"
SUMMARY_PREFIX = "【先前工作摘要】\n"

SUMMARY_REQUEST = """【系統指示】請停止工作，不要呼叫任何工具，只輸出一份摘要，讓一個看不到上面歷史的 agent 能接著完成任務。請依序整理：
1. 原始任務與使用者的所有要求
2. 已完成的工作（列出已處理完的檔案）
3. 目前檔案狀態
4. 尚未完成的工作（列出剩下的檔案）
5. 重要發現與踩過的錯誤（例如 python 指令不存在要用 python3）
6. 下一步"""

def needs_compaction(last_usage):
    if last_usage is None:   # 假 client 或第一輪：沒有 usage 就沒有依據，不壓縮
        return False
    used = (getattr(last_usage, "input_tokens", 0) or 0) + (getattr(last_usage, "output_tokens", 0) or 0)
    return used > COMPACT_AT_TOKENS

def user_texts(messages):
    # 只收使用者「打的字」（不含 tool_result），原樣保留：摘要由模型改寫，使用者的話不能被改寫。
    # 上次壓縮留下的標頭與摘要要丟掉（摘要會被新的取代）；原文區塊去掉前綴還原
    out = []
    for m in messages:
        if m["role"] != "user":
            continue
        if isinstance(m["content"], str):
            out.append(m["content"])
            continue
        for b in m["content"]:
            t = _field(b, "text")
            if _field(b, "type") != "text" or t.startswith((HEADER_PREFIX, SUMMARY_PREFIX)):
                continue
            out.append(t.removeprefix(USER_PREFIX))
    return out

def _compact_failed(log, turn, reason, system=None):
    # 失敗要退避，也要留 log：之後回頭看才知道為什麼那幾輪沒壓縮
    global COMPACT_COOLDOWN
    COMPACT_COOLDOWN = COMPACT_COOLDOWN_TURNS
    print(f"[警告] {reason}，這次不壓縮")
    with open(log, "a") as f:
        # 記下實際送出的 system：v3 的 400 就是摘要請求失敗，凍結檢查不能漏掉這種
        f.write(json.dumps({"turn": turn, "compaction_failed": {"reason": reason, "system": system}}, ensure_ascii=False) + "\n")
    return False

def compact(client, messages, log, turn=0):
    """回傳是否真的壓縮了。只在「邊界」做：沒有未配對的 tool_use（尾端是 user，或是 end_turn 後的純文字 assistant），
    否則摘要請求本身會是壞歷史。"""
    if not messages:
        return False
    last = messages[-1]
    instruction = {"type": "text", "text": SUMMARY_REQUEST}
    # 淺拷貝＋把指示附加在拷貝上：不動到原 messages，失敗時歷史原封不動
    if last["role"] == "user":
        content = [{"type": "text", "text": last["content"]}] if isinstance(last["content"], str) else list(last["content"])
        asked = messages[:-1] + [{"role": "user", "content": content + [instruction]}]
    elif not any(_field(b, "type") == "tool_use" for b in last["content"]):   # /compact 在一輪結束後下，尾端是 assistant
        asked = messages + [{"role": "user", "content": [instruction]}]
    else:
        return False
    kwargs = build_request(asked)
    # 歷史裡有 tool_use/tool_result 時 API 要求仍要定義 tools；用 none 禁止這次呼叫工具（opus-5-5 只禁止 any/tool，none 可用）
    # system 與 tools 必須和主對話一模一樣：thinking 區塊的簽章綁定「system＋tools＋之前所有訊息」，
    # 換掉 system 會被 API 以 400「bound to a different conversation」拒絕（v3 第一次真實測驗踩到）。
    # 摘要指示只能「附加」在最後；tool_choice 不在綁定範圍內，可以改成 none。
    kwargs.update(tool_choice={"type": "none"})
    before = (getattr(LAST_USAGE, "input_tokens", 0) or 0) + (getattr(LAST_USAGE, "output_tokens", 0) or 0)
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.APIError as e:
        return _compact_failed(log, turn, f"摘要請求失敗：{type(e).__name__}: {e}", kwargs["system"])
    # 被 max_tokens 截斷的摘要會缺尾巴（通常正是「下一步」），拒絕的回應更不是摘要；寧可不壓縮
    if response.stop_reason != "end_turn":
        return _compact_failed(log, turn, f"摘要回應 stop_reason={response.stop_reason}", kwargs["system"])
    summary = "".join(b.text for b in response.content if b.type == "text").strip()
    if not summary:
        return _compact_failed(log, turn, "摘要回應沒有文字", kwargs["system"])
    texts = user_texts(messages)
    n_before = len(messages)
    # 原地替換：REPL 持有的是同一個 list，換內容才會一起更新
    blocks = [HEADER_PREFIX + "以下是使用者的原始要求（逐字原文）與先前工作的摘要。"]
    blocks += [USER_PREFIX + t for t in texts]
    blocks.append(f"{SUMMARY_PREFIX}{summary}\n\n請接著完成任務。")
    messages[:] = [{"role": "user", "content": [{"type": "text", "text": t} for t in blocks]}]
    # 摘要請求併進同一行 log（不另寫 request/response）；summary_usage 留著，統計 token 峰值時才不會漏掉它
    usage = getattr(response, "usage", None)
    with open(log, "a") as f:
        line = {"turn": turn, "compaction": {"before_tokens": before, "messages_before": n_before, "summary": summary,
                "compact_at": COMPACT_AT_TOKENS, "system": kwargs["system"], "user_texts": texts, "summary_usage": usage.model_dump(mode="json") if usage else None}}
        f.write(json.dumps(line, ensure_ascii=False, default=to_jsonable) + "\n")
    print(f"🗜 context 壓縮：{before} tokens → 摘要 {len(summary)} 字")
    return True

def run_turn(client, messages, log):
    try:
        _loop(client, messages, log)
    finally:
        close_dangling(messages)   # return、例外、Ctrl-C 都會經過這裡

def _loop(client, messages, log):
    global LAST_USAGE, COMPACT_COOLDOWN
    for turn in range(1, MAX_TURNS + 1):
        if COMPACT_COOLDOWN > 0:
            COMPACT_COOLDOWN -= 1
        elif needs_compaction(LAST_USAGE) and compact(client, messages, log, turn):
            LAST_USAGE = None   # 歷史換過了，舊的 usage 不代表現在的長度
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
        LAST_USAGE = getattr(response, "usage", None)   # 假 client 的回應可能沒有 usage
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
            # 同一則回應的多個 tool_use 是同一個計畫：只核准一半會留下不一致的狀態（例如改了 check.py 沒改 reports.py）。
            # 所以第一個被拒絕之後，同批後面的都不問、不執行。
            # 限制：拒絕「之前」已執行的步驟不會回滾，它們的結果照舊如實保留。
            rejected = None   # 本批第一個被拒絕的工具名
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if rejected:
                    print(f"⏭ 略過 {block.name}（同批前一個操作被拒絕）")
                    perm = {"id": block.id, "tool": block.name, "input": block.input, "decision": "skipped",
                            "answer": None, "reason": f"同批前面的 {rejected} 被拒絕"}
                    refusal = {"content": f"因為同一批中前面的 {rejected} 操作被拒絕，這個操作沒有執行。", "is_error": True}
                else:
                    summary = block.input.get("command") or block.input.get("pattern") or block.input.get("path") or ""
                    print(f"→ {block.name}: {summary}")
                    refusal, perm = authorize(block)
                    rejected = block.name if refusal else None
                with open(log, "a") as f:
                    f.write(json.dumps({"turn": turn, "permission": perm}, ensure_ascii=False, default=to_jsonable) + "\n")
                result = refusal or execute_tool(block.name, block.input)
                if not refusal:
                    print(f"  {result['content'][:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, **result})
                # 「記錄一切」：壓縮會換掉歷史，之後只有這行能查到哪些操作成功過（request 裡的 tool_result 已被摘要取代）
                with open(log, "a") as f:
                    f.write(json.dumps({"turn": turn, "tool_result": {"id": block.id, "tool": block.name,
                            "is_error": bool(result.get("is_error")), "content": str(result["content"])[:300]}},
                            ensure_ascii=False) + "\n")
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
    global LAST_USAGE, COMPACT_COOLDOWN, SESSION_SYSTEM, SESSION_MEMORY
    LAST_USAGE = None
    COMPACT_COOLDOWN = 0
    SESSION_MEMORY = load_memory()
    SESSION_SYSTEM = build_system(SESSION_MEMORY)
    if SESSION_MEMORY:
        print(f"📒 已載入 {WORKDIR.name}/CLAUDE.md（{len(SESSION_MEMORY)} 字）")
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
            LAST_USAGE = None
            COMPACT_COOLDOWN = 0
            continue
        if text == "/compact":   # 手動觸發，不管門檻
            if compact(client, messages, log):
                LAST_USAGE = None
            else:
                print("沒有壓縮（歷史是空的、尾端有未配對的 tool_use，或摘要失敗）")
            continue
        if text == "/memory":
            print(SESSION_MEMORY or "本 session 沒有載入 CLAUDE.md")
            print("（修改 CLAUDE.md 要下次啟動才生效）")
            continue
        if text == "/tokens":
            u = LAST_USAGE
            print(f"最近一次回應：input={u.input_tokens} output={u.output_tokens}" if u else "還沒有 usage 紀錄")
            continue
        if text:
            add_user_text(messages, text)
            try:
                run_turn(client, messages, log)
            except KeyboardInterrupt:
                print("[中斷]")

if __name__ == "__main__":
    main()
