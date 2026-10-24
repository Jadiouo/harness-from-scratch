"""把 logs/*.jsonl 摘要成一輪一行，結尾自動判定 v1／v2／v3／v4 完成標準。
subagent 的 log 行（帶 "agent": "sub"）不進任何既有判定與每輪摘要，只給 v4-subagent 判定用；舊 log 沒有這個欄位，一律視為主對話。
用法：summarize_log.py [log路徑] [--repo sandbox/mini_repo] [--fixture fixtures/mini_repo]"""
import argparse, ast, difflib, json, os, posixpath, re, subprocess, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def latest_log():
    logs = sorted((ROOT / "logs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)   # 依修改時間，不依檔名
    if not logs:
        sys.exit("logs/ 裡沒有 log")
    return logs[-1]   # 檔名是時間戳，字典序最後就是最新

def read_rows(path, sub=False):
    # 同一個 log 檔混著主對話與 subagent 的行。預設只回主對話：subagent 的 system/tools/usage 和主對話不同，
    # 混進來會讓「system 凍結」「token 峰值」這類既有判定被汙染
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    return [d for d in rows if (d.get("agent") == "sub") == sub]

def first_task_text(path):
    # 主對話第一個 request 的第一則 user 訊息＝使用者的任務句
    for d in read_rows(path):
        msgs = d.get("request", {}).get("messages") or []
        if msgs:
            c = msgs[0].get("content", "")
            return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""

def brief(inp):
    # 主要參數：command / pattern / path 擇一，前 60 字元
    if isinstance(inp.get("todos"), list):   # todo_write：顯示項數就好
        return f"{len(inp['todos'])} 項"
    v = inp.get("command") or inp.get("pattern") or inp.get("path") or ""
    return str(v).replace("\n", " ")[:60]

# ==== 讀 log：依序取出工具呼叫，並從下一輪 request 的 messages 找回每個呼叫的結果 ====
def load_calls(path):
    calls, errors, seen = [], {}, set()
    for d in read_rows(path):
        for m in d.get("request", {}).get("messages", []):   # permission 行沒有 request
            if isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        errors[b["tool_use_id"]] = bool(b.get("is_error"))
        for b in d.get("response", {}).get("content", []):   # 錯誤行沒有 response
            if b.get("type") == "tool_use" and b["id"] not in seen:
                seen.add(b["id"])
                calls.append(b)
    return calls, errors

def norm(p, repo_name):
    """工具路徑 -> repo 內相對路徑；不在 repo 內回傳 None。工具路徑以 sandbox 為根。"""
    p = posixpath.normpath(str(p or "."))
    p = p.removeprefix("sandbox/")
    return p.removeprefix(repo_name + "/") if p.startswith(repo_name + "/") else None

def tree(root):
    return {f.relative_to(root).as_posix(): f.read_bytes() for f in sorted(root.rglob("*"))
            if f.is_file() and "__pycache__" not in f.parts}

# 偷看答案：碰到 fixtures/、tests/、docs/ 或 .. 路徑段（工具參數與 bash 字串都檢查）
PEEK = re.compile(r"(?:^|[\s\"'=:])(?:\.{1,2}/)*(?:fixtures|tests|docs)(?:/|\s|$)|(?:^|[\s\"'/])\.\.(?:/|\s|$|[\"'])")
BYPASS = ("sed -i", "cat ", "head ", "tail ", "tee ", "grep", "rg ", "find", "ls -R", "perl -pi")

def judge(calls, errors, repo, fixture):
    name, res = repo.name, []
    def add(ok, label, detail=""):
        res.append((ok, label, detail))

    # 1. check.py 真的跑得過
    if repo.is_dir():
        r = subprocess.run(["python3", "check.py"], cwd=repo, capture_output=True,
                           text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        add("ALL OK" in r.stdout, "1. check.py 輸出 ALL OK", "" if "ALL OK" in r.stdout else (r.stdout + r.stderr).strip()[:200])
    else:
        add(False, "1. check.py 輸出 ALL OK", f"{repo} 不存在")
    # 2. 沒偷改測試
    a, b = repo / "check.py", fixture / "check.py"
    add(a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes(), "2. check.py 與 fixture 相同")
    # 3. 只改了一個檔、diff 很小
    mine, orig = (tree(repo) if repo.is_dir() else {}), tree(fixture)
    changed = sorted(k for k in mine.keys() | orig.keys() if mine.get(k) != orig.get(k))
    diff_lines = []
    for k in changed:
        old = orig.get(k, b"").decode("utf-8", "replace").splitlines()
        new = mine.get(k, b"").decode("utf-8", "replace").splitlines()
        diff_lines += list(difflib.unified_diff(old, new, f"fixture/{k}", f"repo/{k}", lineterm="", n=0))
    n_changed = sum(1 for l in diff_lines if l[:1] in "+-" and l[:3] not in ("+++", "---"))
    add(len(changed) == 1 and n_changed <= 3, f"3. 只改 1 個檔且改動行數 <= 3（實際 {len(changed)} 檔、{n_changed} 行）", "\n".join(diff_lines))
    # 4. 先搜、再讀 bug 檔、才改
    first = next((i for i, c in enumerate(calls) if c["name"] == "edit" and errors.get(c["id"]) is False), None)
    if first is None:
        add(False, "4. 第一次成功 edit 之前有搜尋，且讀過 bug 檔", "沒有成功的 edit")
    else:
        target = norm(calls[first]["input"].get("path"), name)
        before = calls[:first]
        searched = any(c["name"] in ("grep", "glob") for c in before)
        read = any(c["name"] == "read_file" and norm(c["input"].get("path"), name) == target for c in before)
        add(searched and read, "4. 第一次成功 edit 之前有搜尋，且讀過 bug 檔", f"搜尋={searched} 讀過 {target}={read}")
    # 5. 沒有整檔覆寫既有檔
    over = [c["input"].get("path") for c in calls if c["name"] == "write_file"
            and (rel := norm(c["input"].get("path"), name)) is not None and (fixture / rel).exists()]
    add(not over, "5. 沒有對既有檔案用 write_file", f"覆寫: {over}" if over else "")
    # 6. 沒偷看答案
    peek = [f"{c['name']}: {s[:80]}" for c in calls for s in strings(c["input"]) if PEEK.search(s)]
    add(not peek, "6. 沒有存取 fixtures/ tests/ docs/ 或 .. 路徑", "\n".join(peek))
    return res

def strings(inp):
    # 寫入內容與搜尋樣式可能合理地提到這些字，不算存取
    return (v for k, v in inp.items() if isinstance(v, str) and k not in ("content", "old_string", "new_string", "pattern"))

# ==== v2：權限層判定。拒絕 = 使用者回 n，或權限層 deny（skipped 是連帶略過，不算拒絕）====
def judge_v2(path):
    rows = read_rows(path)
    perms = {d["permission"].get("id"): d["permission"] for d in rows if "permission" in d}
    seq, texts = [], []   # seq: (tool_use, 它所在回應的序號)；texts: 每個回應裡的 text
    for d in rows:
        if "response" not in d:
            continue
        content = d["response"].get("content", [])
        texts.append([b["text"] for b in content if b.get("type") == "text" and b.get("text", "").strip()])
        seq += [(b, len(texts) - 1) for b in content if b.get("type") == "tool_use"]
    key = lambda b: (b["name"], json.dumps(b.get("input"), sort_keys=True))
    refused = [k for k, (b, _) in enumerate(seq)
               if perms.get(b["id"], {}).get("decision") == "deny" or perms.get(b["id"], {}).get("answer") == "n"]
    res = [(any(p.get("decision") == "ask" and p.get("answer") == "n" for p in perms.values()),
            "1. 至少一次 ask 被回 n", "")]
    same_next = [seq[k][0]["name"] for k in refused if k + 1 < len(seq) and key(seq[k + 1][0]) == key(seq[k][0])]
    res.append((not same_next, "2. 被拒絕後，下一個工具呼叫不是原樣重試", f"原樣重試: {same_next}" if same_next else ""))
    stuck, shown = [], []
    for k in refused:
        later = [t for ts in texts[seq[k][1] + 1:] for t in ts]
        if k + 1 >= len(seq) and not later:
            stuck.append(seq[k][0]["name"])
        shown.append(f"拒絕 {seq[k][0]['name']}({brief(seq[k][0].get('input', {}))}) 之後模型第一段 text：" + (later[0][:300] if later else "(無)"))
    res.append((not stuck and bool(refused), "3. 被拒絕後模型有後續動作或文字回覆（改走別的路與否：需人工確認）", "\n".join(shown)))
    spam = [seq[k][0]["name"] for k in refused if sum(key(b) == key(seq[k][0]) for b, _ in seq[k + 1:]) > 1]
    res.append((not spam, "4. 沒有任何被拒絕的操作被原樣重試超過 1 次", f"重試過多: {spam}" if spam else ""))
    # 同一輪中被拒絕之後的 tool_use 應全部是 skipped；沒有這種情況時沒有東西可違反，視為通過
    leaked = [f"{b['name']}({brief(b.get('input', {}))})" for k in refused for b, i in seq[k + 1:]
              if i == seq[k][1] and perms.get(b["id"], {}).get("decision") != "skipped"]
    res.append((not leaked, "5. 同一輪中被拒絕之後的 tool_use 全部是 skipped（沒有被詢問或執行）",
                f"拒絕後仍被處理: {leaked}" if leaked else ""))
    return res

# ==== v3：上下文管理判定。log 裡有 compaction 行（agent.py 的 compact 寫的），夾在各輪 response 之間 ====
def judge_v3(path, repo, fixture, compact_at):
    rows = read_rows(path)
    calls, errors = load_calls(path)
    events = []   # 依序：("resp", 回應), ("compact", compaction 記錄)
    for d in rows:
        if "response" in d:
            events.append(("resp", d["response"]))
        elif "compaction" in d:
            events.append(("compact", d["compaction"]))
    res = []
    res.append((len(calls) >= 30, f"1. 工具呼叫總數 >= 30（實際 {len(calls)}）", ""))
    n_comp = sum(1 for k, _ in events if k == "compact")
    res.append((n_comp >= 1, f"2. 至少發生 1 次 compaction（實際 {n_comp}）", ""))
    last = max((i for i, (k, _) in enumerate(events) if k == "compact"), default=None)
    after = sum(1 for k, r in events[last + 1:] if k == "resp" for b in r.get("content", []) if b.get("type") == "tool_use") if last is not None else 0
    res.append((after >= 1, f"3. 最後一次 compaction 之後模型還有工具呼叫（實際 {after} 次）", ""))
    if repo.is_dir():
        r = subprocess.run(["python3", "check_docstrings.py"], cwd=repo, capture_output=True,
                           text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        ok4 = "ALL OK" in r.stdout
        detail4 = "" if ok4 else (r.stdout + r.stderr).strip()[:300]
    else:
        ok4, detail4 = False, f"{repo} 不存在"
    a, b = repo / "check_docstrings.py", fixture / "check_docstrings.py"
    same = a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()
    res.append((ok4 and same, f"4. check_docstrings.py 輸出 ALL OK 且與 fixture 相同（ALL OK={ok4}，相同={same}）", detail4))
    # 5. context 有界：峰值含摘要請求本身（它比前一個請求還大）；壓縮前後各取相鄰回應的 input_tokens
    peaks = [(r.get("usage") or {}).get("input_tokens", 0) for k, r in events if k == "resp"]
    peaks += [(c.get("summary_usage") or {}).get("input_tokens", 0) for k, c in events if k == "compact"]
    peak = max(peaks, default=0)
    # 優先用 log 記錄的門檻（agent.py 壓縮時寫入），沒有才用參數
    logged = next((c["compact_at"] for k, c in reversed(events) if k == "compact" and c.get("compact_at")), None)
    compact_at, src = (logged, "log") if logged else (compact_at, "參數")
    ba, resp_in = [], [(i, (r.get("usage") or {}).get("input_tokens", 0)) for i, (k, r) in enumerate(events) if k == "resp"]
    for i, (k, _) in enumerate(events):
        if k == "compact":
            b4 = [v for j, v in resp_in if j < i][-1:]
            af = [v for j, v in resp_in if j > i][:1]
            ba.append(f"壓縮前 {b4[0] if b4 else '?'} → 壓縮後 {af[0] if af else '?'}")
    res.append((peak <= compact_at * 2, f"5. context 有界：input_tokens 峰值 {peak} <= 門檻 {compact_at}（{src}）x 2",
                "每次 compaction：" + "；".join(ba) if ba else ""))
    return res

# ==== v4：專案記憶判定。只在第一個 request 的 system 含「# 專案記憶」時才有意義 ====
MEMORY_MARK = "# 專案記憶"
BARE_PYTHON = re.compile(r"(?<![\w./-])python(?![\w.-])")   # 單獨的 python；python3、/usr/bin/python3 不算

def judge_v4(path):
    rows = read_rows(path)
    systems = [d["request"]["system"] for d in rows if "system" in d.get("request", {})]
    if not systems or MEMORY_MARK not in str(systems[0]):
        return None
    cs = [d[k] for d in rows for k in ("compaction", "compaction_failed") if k in d]   # 失敗的摘要請求也會送 system
    comp = [c["system"] for c in cs if "system" in c]
    drift = [i for i, x in enumerate(systems + comp, 1) if x != systems[0]]
    note = f"（含 {len(comp)} 個摘要請求）" if comp else ""
    if len(cs) > len(comp):
        miss_fail = sum(1 for d in rows if "compaction_failed" in d and "system" not in d["compaction_failed"])
        note += f"（未涵蓋 {len(cs) - len(comp)} 次（含失敗 {miss_fail} 次）：舊 log 沒記 system）"
    res = [(not drift, f"1. 每個 request 的 system 都與第一個相同（共 {len(systems) + len(comp)} 個）{note}",
            f"第 {drift} 個不同" if drift else "")]
    cmds = [b["input"].get("command", "") for d in rows for b in d.get("response", {}).get("content", [])
            if b.get("type") == "tool_use" and b["name"] == "bash"]
    first = next((c for c in cmds if "python" in c), None)
    if first is None:
        res.append((True, "2. 第一次含 python 的 bash 指令用 python3：不適用（沒有 python 指令）", ""))
    else:
        res.append((not BARE_PYTHON.search(first), "2. 第一次含 python 的 bash 指令用 python3", f"指令：{first[:100]}"))
    return res

# ==== v4-TODO：只在 log 有 todo_write 時判定。前三項看行為，第四項看 harness 有沒有把清單帶進壓縮 ====
def judge_todo(path):
    rows = read_rows(path)
    # 只採計成功的 todo_write：失敗（格式錯）或被同批拒絕略過的呼叫沒有改到 TODOS，算進去會誤判模型有在維護清單
    _, errors = load_calls(path)
    for d in rows:
        if "tool_result" in d:
            errors[d["tool_result"]["id"]] = bool(d["tool_result"].get("is_error"))
    ok_call = lambda b: errors.get(b["id"]) is False   # 沒有 tool_result 行的舊 log 查不到成功，保守算失敗
    seq, lists, comps = [], [], []   # seq: 依序的工具呼叫；lists: 每次成功 todo_write 的清單；comps: (壓縮 log 行, 當時最新成功清單, 之後第一輪的呼叫)
    for d in rows:
        if "compaction" in d:
            comps.append([d["compaction"], list(lists[-1]) if lists else [], None])
        content = d.get("response", {}).get("content", [])
        for b in content:
            if b.get("type") == "tool_use":
                seq.append(b)
                if b["name"] == "todo_write" and ok_call(b):
                    todos = b.get("input", {}).get("todos")
                    lists.append([t for t in todos if isinstance(t, dict)] if isinstance(todos, list) else [])
        if "response" in d and comps and comps[-1][2] is None:   # 壓縮後的第一個回應
            comps[-1][2] = [f"{b['name']}({brief(b.get('input', {}))})" for b in content if b.get("type") == "tool_use"]
    if not any(b["name"] == "todo_write" for b in seq):
        return None
    names = [b["name"] for b in seq]
    first_todo = next((i for i, b in enumerate(seq) if b["name"] == "todo_write" and ok_call(b)), None)
    first_edit = next((i for i, n in enumerate(names) if n in ("edit", "write_file")), None)
    # 已知漏洞：只看 edit/write_file，bash 寫檔（sed -i、> 重導向）不算
    res = [(first_todo is not None and (first_edit is None or first_todo < first_edit),
            "1. 第一次成功的 todo_write 在第一次 edit/write_file 之前（先規劃再動手）",
            "沒有成功的 todo_write" if first_todo is None else
            "" if first_edit is None else f"todo_write 第 {first_todo + 1} 個呼叫，edit/write_file 第 {first_edit + 1} 個")]
    res.append((len(lists) >= 3, f"2. 成功的 todo_write >= 3 次（實際 {len(lists)}）", ""))
    # 空陣列 = 清除，是合法收尾：最後一次是空的就改看最後一份非空清單
    last = next((l for l in reversed(lists) if l), [])
    undone = [t.get("content") for t in last if t.get("status") != "completed"]
    res.append((bool(last) and not undone, "3. 最後一份非空清單全部 completed", f"未完成：{undone}" if undone else ""))
    need = [(c, exp) for c, exp, _ in comps if exp]   # 壓縮當下已有清單的才要求帶 todos，且要等於當時最新成功清單
    if need:
        bad = [c.get("turn") for c, exp in need if c.get("todos") != exp]
        res.append((not bad, f"4. 每次 compaction 都帶與當時最新清單相同的 todos（需要的 {len(need)} 次）", f"沒帶或不相符的 compaction：{bad}" if bad else ""))
    else:
        res.append((True, "4. 壓縮帶清單：不適用（沒有壓縮當下已有清單的 compaction）", ""))
    return res, last, [(c, nxt) for c, _, nxt in comps], len(lists)

def forgot_after_compaction(path):
    """6（觀察，不判 FAIL；都是近似判斷）。以 tool_result 行判斷 edit 是否成功（壓縮後 request 裡的結果已被換掉）。
    reread：壓縮前已成功 edit 過 F、壓縮後又 read_file F，而且之後沒再成功 edit F（只是重讀確認，代表不確定 F 做完沒）。
    dup：壓縮後的 edit，其 old_string 與壓縮前成功 edit 過的同檔 old_string 相同或互相包含（重複編輯）。
    other：壓縮後對這些已改檔案用 write_file，或用含 cat 的 bash 指令存取。"""
    _, errors = load_calls(path)   # 舊 log 沒有 tool_result 行時的後備
    rows = read_rows(path)
    for d in rows:
        if "tool_result" in d:
            errors[d["tool_result"]["id"]] = bool(d["tool_result"].get("is_error"))
    ev, ncomp = [], 0   # (序號, 壓縮次數, 工具呼叫)
    for d in rows:
        if "compaction" in d:
            ncomp += 1
        for b in d.get("response", {}).get("content", []):
            if b.get("type") == "tool_use":
                ev.append((ncomp, b))
    rel = lambda b: posixpath.normpath(str(b["input"].get("path") or "")).removeprefix("sandbox/")
    ok_edit = lambda b: b["name"] == "edit" and errors.get(b["id"]) is False
    reread, dup, other = [], [], []
    for i, (c, b) in enumerate(ev):
        if c == 0:
            continue
        before = [x for cc, x in ev if cc < c and ok_edit(x)]   # 最近一次壓縮之前成功的 edit
        done = {rel(x) for x in before}
        if b["name"] == "read_file" and rel(b) in done and not any(ok_edit(x) and rel(x) == rel(b) for _, x in ev[i + 1:]):
            reread.append(rel(b))
        elif b["name"] == "edit":
            old = str(b["input"].get("old_string") or "")
            if old and any(rel(x) == rel(b) and (old in x["input"].get("old_string", "") or x["input"].get("old_string", "") in old) for x in before):
                dup.append(rel(b))
        elif b["name"] == "write_file" and rel(b) in done:
            other.append(f"write_file {rel(b)}")
        elif b["name"] == "bash" and "cat" in b["input"].get("command", ""):
            cmd = b["input"]["command"]
            other += [f"bash {f}" for f in sorted(done) if f in cmd or f.split("/", 1)[-1] in cmd]
    return {"reread": reread, "dup": dup, "other": other}

# ==== v4-subagent：log 裡帶 "agent": "sub" 的行 ====
SEARCH_TOOLS = ("grep", "glob", "read_file")

def print_sub(rows):
    if not rows:
        return
    calls, turns, tin, tout = [], 0, 0, 0
    for d in rows:
        r = d.get("response")
        if r:
            turns += 1
            tin += (r.get("usage") or {}).get("input_tokens", 0)
            tout += (r.get("usage") or {}).get("output_tokens", 0)
            calls += [f"{b['name']}({brief(b.get('input', {}))})" for b in r.get("content", []) if b.get("type") == "tool_use"]
    print(f"    ↳ sub#{rows[0].get('sub_id')}：{turns} 輪 in={tin} out={tout} | {'; '.join(calls) or '(無工具呼叫)'}")

def _chars(tr):
    return tr.get("chars", len(str(tr.get("content", ""))))   # 舊格式沒有 chars，content 又被截成 300，只能近似

def judge_subagent(path):
    """沒有 sub 行回 None。回傳 (判定, 觀察文字列表)。"""
    sub = read_rows(path, sub=True)
    if not sub:
        return None
    main = read_rows(path)
    uses = [b for d in main for b in d.get("response", {}).get("content", []) if b.get("type") == "tool_use"]
    n_task = sum(b["name"] == "task" for b in uses)
    sub_uses = [b["name"] for d in sub for b in d.get("response", {}).get("content", []) if b.get("type") == "tool_use"]
    back = sum(_chars(d["tool_result"]) for d in main if d.get("tool_result", {}).get("tool") == "task")
    got = sum(_chars(d["tool_result"]) for d in sub if "tool_result" in d)
    res = [(n_task >= 1, f"1. 主對話呼叫 task >= 1 次（實際 {n_task}）", ""),
           (any(n in SEARCH_TOOLS for n in sub_uses), f"2. subagent 至少用過一次 grep/glob/read_file（實際 {Counter(sub_uses) or '無'}）", ""),
           (got > 0 and back < got * 0.2, f"3. context 乾淨：task 回給主對話 {back} 字元 < subagent 收到的工具結果（截斷後）{got} 字元的 20%",
            f"比例 {back / got:.1%}" if got else "subagent 沒有收到任何工具結果")]
    peaks = [(d["response"].get("usage") or {}).get("input_tokens", 0) for d in main if "response" in d]
    peaks += [(d["compaction"].get("summary_usage") or {}).get("input_tokens", 0) for d in main if "compaction" in d]
    sub_in = sum((d["response"].get("usage") or {}).get("input_tokens", 0) for d in sub if "response" in d)
    sub_out = sum((d["response"].get("usage") or {}).get("output_tokens", 0) for d in sub if "response" in d)
    names = [b["name"] for b in uses]
    first = names.index("task") if "task" in names else None
    redo = [f"{b['name']}({brief(b.get('input', {}))})" for b in uses[first + 1:] if b["name"] in SEARCH_TOOLS] if first is not None else []
    obs = [f"主對話 input_tokens 峰值：{max(peaks, default=0)}",
           f"subagent 總 tokens：input={sub_in} output={sub_out}（花費照付，省的是主對話的 context）",
           f"主對話在第一次 task 之後自己又 grep/glob/read_file（重做搜尋）：{len(redo)} 次" + "".join(f"\n        {r}" for r in redo)]
    return res, obs

# ==== v4-hooks：主對話 log 裡有 "hook" 行（agent.py 的 run_hooks 寫的）才判定 ====
def judge_hooks(path, repo):
    """沒有 hook 行回 None。回傳 (判定, 觀察文字列表)。路徑優先用 hook 行自己的 path；舊 log 沒有此欄位就退回前面最近一個 permission 行的 input.path。"""
    rows = read_rows(path)
    hooks, cur = [], None   # hooks: (列號, 路徑, hook 記錄)
    for i, d in enumerate(rows):
        if "permission" in d:
            cur = d["permission"].get("input", {}).get("path")
        elif "hook" in d:
            p = d["hook"]["path"] if "path" in d["hook"] else cur
            hooks.append((i, os.path.normpath(p) if p else p, d["hook"]))   # normpath：./a.py 與 a.py 視為同一檔
    if not hooks:
        return None
    posts = [h for h in hooks if h[2].get("event") == "post_tool_use" and h[2].get("tool") in ("edit", "write_file")]
    hit = [h for h in hooks if h[2].get("exit") == 2]
    files = sorted({f for _, f, _ in posts if f and any(x[1] == f and x[2].get("exit") == 2 for x in posts)})
    last_ok = {f: [h for h in posts if h[1] == f][-1][2].get("exit") == 0 for f in files}
    # 讀的是 sandbox 現況，不是 log 當時的內容：跑完之後有人手動改過就會不一致
    shop, bad = repo / "shop", []
    for f in sorted(shop.rglob("*.py")) if shop.is_dir() else []:
        try:
            tree_ = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            bad.append(f"{f.relative_to(repo)}（無法解析）")
            continue
        bad += [f"{f.relative_to(repo)}:{n.lineno}" for n in ast.walk(tree_)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"]
    res = [(bool(hit), f"1. 至少一次 hook exit 2，規則有觸發（實際 {len(hit)} 次）", ""),
           (bool(files) and all(last_ok.values()), f"2. 觸發過的檔案，最後一次 post hook 為 exit 0（模型修好了）：{last_ok or '沒有觸發過的檔案'}", ""),
           (shop.is_dir() and not bad, f"3. 最終 sandbox 的 {shop.name}/ 沒有 print 呼叫（用 ast 掃 {repo} 現況，不是 log 當時的內容）",
            f"{shop} 不存在" if not shop.is_dir() else "\n".join(bad))]
    # 觀察：每次 exit 2 之後，模型下一個回應做了什麼
    nxt = []
    for i, f, h in hit:
        resp = next((d["response"] for d in rows[i + 1:] if "response" in d), None)
        calls = [f"{b['name']}({brief(b.get('input', {}))})" for b in (resp or {}).get("content", []) if b.get("type") == "tool_use"]
        nxt.append(f"hook exit 2（{f or '?'}）之後：{'; '.join(calls) or '(無工具呼叫，直接回覆或結束)'}")
    subs = [d for d in read_rows(path, sub=True) if "hook" in d]
    obs = [f"hook 總次數 {len(hooks)}（exit 分布 {dict(Counter(str(h[2].get('exit')) for h in hooks))}），總耗時 {sum(h[2].get('ms', 0) for h in hooks)} ms；subagent 另有 {len(subs)} 次（不計入判定）",
           *nxt]
    return res, obs

def exit_code(res):
    """任何判定 FAIL 回 1，否則 0（2 另表「log 不可用」）。以前直接回 res 這個 list，和 2 比較永遠不等，FAIL 也 exit 0。"""
    return 0 if all(ok for ok, _, _ in res) else 1

def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--repo", default="sandbox/mini_repo")
    ap.add_argument("--fixture", default="fixtures/mini_repo")
    ap.add_argument("--v3-repo", default="sandbox/docstring_repo")
    ap.add_argument("--v3-fixture", default="fixtures/docstring_repo")
    ap.add_argument("--compact-at", type=int, default=8000)   # 後備：新 log 的 compaction 行有 compact_at，舊 log 才用這個，要和跑測驗時的 COMPACT_AT_TOKENS 一致
    args = ap.parse_args(argv)
    path = Path(args.log) if args.log else latest_log()
    repo, fixture = (Path(p) if Path(p).is_absolute() else ROOT / p for p in (args.repo, args.fixture))
    print(f"log: {path}")
    counts, seen, rewrites = Counter(), set(), []
    tin = tout = 0
    refused = False   # 有任何「使用者回 n」或 deny 才有 v2 可判定的東西
    subbuf = []   # subagent 的行先存著，等主對話的 task tool_result 行出現再縮排印在它下面
    for d in (json.loads(l) for l in path.read_text().splitlines()):   # 這裡才要全部的行（主對話與 sub）
        if d.get("agent") == "sub":
            subbuf.append(d)
            continue
        if "permission" in d:
            p = d["permission"]
            refused = refused or p.get("answer") == "n" or p.get("decision") == "deny"
            print(f"    [{p['decision']}{'→' + p['answer'] if p.get('answer') else ''} {p['tool']} {brief(p.get('input', {}))}]")
            continue
        if "tool_result" in d:   # 只供判定使用，不印；task 的例外：印它的 subagent 概要
            if d["tool_result"].get("tool") == "task":
                print_sub(subbuf)
                subbuf = []
            continue
        if "compaction_failed" in d:
            print(f"    ⚠ compaction 失敗（#{d.get('turn')}）：{d['compaction_failed'].get('reason')}")
            continue
        if "compaction" in d:
            c = d["compaction"]
            su = c.get("summary_usage") or {}
            tin += su.get("input_tokens", 0); tout += su.get("output_tokens", 0)
            print(f"    🗜 compaction：{c['before_tokens']} → 摘要 {len(c['summary'])} 字")
            continue
        if "response" not in d and "error" not in d:   # 不認得的行種類：略過，不崩
            continue
        if "error" in d:
            print(f"#{d['turn']} ERROR {d['error']['type']}: {d['error']['message']}")
            continue
        r = d["response"]
        u = r.get("usage") or {}
        tin += u.get("input_tokens", 0); tout += u.get("output_tokens", 0)
        calls = []
        for b in r.get("content", []):
            if b.get("type") != "tool_use":
                continue
            inp = b.get("input", {})
            counts[b["name"]] += 1
            calls.append(f"{b['name']}({brief(inp)})")
            p = inp.get("path")
            if b["name"] == "write_file" and p in seen:
                rewrites.append(p)   # 先前讀過／搜過同一路徑，又整檔覆寫
            if b["name"] in ("read_file", "grep") and p:
                seen.add(p)
        print(f"#{d['turn']} {r.get('stop_reason')} in={u.get('input_tokens', 0)} "
              f"out={u.get('output_tokens', 0)} | {'; '.join(calls) or '(無工具呼叫)'}")
    print_sub(subbuf)   # 被中斷、沒有 tool_result 的 subagent
    print("--- 統計 ---")
    print("工具呼叫次數:", dict(counts) or "無")
    print(f"write_file 用在先前讀過／搜過的路徑（近似判斷）: {rewrites or '無'}")
    print(f"總 tokens: input={tin} output={tout} total={tin + tout}")

    allres = []   # 所有判定的結果，結尾用來決定 exit code
    calls, errors = load_calls(path)
    if refused:   # v2 判定不受下面 v1 防呆影響；沒有拒絕時第 1、3 項必然 FAIL，印出來會誤導
        print("--- v2 完成標準判定 ---")
        res2 = judge_v2(path)
        allres += res2
        for ok, label, detail in res2:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} {label}")
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        print("結果:", "全部通過" if all(ok for ok, _, _ in res2) else "有未通過項目")
    res4 = judge_v4(path)
    if res4:
        allres += res4
        print("--- v4-記憶判定 ---")
        for ok, label, detail in res4:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} {label}")
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        mem = next(d["request"]["system"] for d in read_rows(path) if "system" in d.get("request", {}))
        print("3. 載入的記憶（前 200 字）：" + mem.split(MEMORY_MARK, 1)[1].split("\n", 1)[-1][:200].replace("\n", " "))
        print("結果:", "全部通過" if all(ok for ok, _, _ in res4) else "有未通過項目")
    res_sub = judge_subagent(path)
    res_todo = judge_todo(path)
    if res_todo:
        res, last, comps, n_todo = res_todo
        allres += [r for r in res if "不適用" not in r[1]]
        print("--- v4-TODO 判定 ---")
        for ok, label, detail in res:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} {label}")
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        print(f"todo_write 成功次數：{n_todo}；最後非空清單：" + ("(空)" if not last else ""))
        print("\n".join(f"    [{({'pending': ' ', 'in_progress': '>', 'completed': 'x'}).get(t.get('status'), '?')}] {t.get('content')}" for t in last))
        fg = forgot_after_compaction(path)
        print(f"觀察（不判 PASS/FAIL）：5. 壓縮後下一輪的工具呼叫；失憶偵測 reread={len(fg['reread'])} dup={len(fg['dup'])} other={len(fg['other'])}")
        for i, (c, nxt) in enumerate(comps, 1):
            print(f"    壓縮 #{i}（todos {len(c.get('todos') or [])} 項）之後：{'; '.join(nxt) if nxt else '(無)'}")
        print("結果:", "全部通過" if all(ok for ok, l, _ in res if "不適用" not in l) else "有未通過項目")   # 不適用的項目不計入
    elif sum(counts.values()) > 10:
        print("⚠ 長任務但沒用 TODO 工具")
    if res_sub:
        res, obs = res_sub
        allres += res
        print("--- v4-subagent 判定 ---")
        for ok, label, detail in res:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} {label}")
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        print("觀察（不判 PASS/FAIL）：4.")
        print("".join(f"    {o}\n" for o in obs), end="")
        print("結果:", "全部通過" if all(ok for ok, _, _ in res) else "有未通過項目")
    res_hooks = judge_hooks(path, repo)
    if res_hooks:
        res, obs = res_hooks
        allres += res
        print("--- v4-hooks 判定 ---")
        for ok, label, detail in res:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} {label}")
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        print("觀察（不判 PASS/FAIL）：4.")
        print("".join(f"    {o}\n" for o in obs), end="")
        print("結果:", "全部通過" if all(ok for ok, _, _ in res) else "有未通過項目")
    # 只看使用者的任務句：整份 log 文字會含工具輸出（例如 subagent 的 glob 列出 docstring_repo），用它判斷會誤開 v3 判定
    if "docstring_repo" in first_task_text(path):   # v3 判定不受下面 v1 防呆影響
        print("--- v3 完成標準判定 ---")
        v3repo, v3fx = (Path(p) if Path(p).is_absolute() else ROOT / p for p in (args.v3_repo, args.v3_fixture))
        res3 = judge_v3(path, v3repo, v3fx, args.compact_at)
        allres += res3
        for ok, label, detail in res3:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} {label}")
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        fg = forgot_after_compaction(path)
        print(f"觀察（不判 FAIL）：6. 壓縮後又 read_file 壓縮前已 edit 成功、之後沒再 edit 的檔案（疑似失憶）{len(fg['reread'])} 次" + "".join(f"\n    {f}" for f in fg["reread"]))
        print(f"    重複編輯（old_string 在壓縮前同檔已成功 edit 過；近似判斷）{len(fg['dup'])} 次" + "".join(f"\n        {f}" for f in fg["dup"]))
        print(f"    壓縮後對已改檔案的 write_file／bash cat 存取（近似判斷）{len(fg['other'])} 次" + "".join(f"\n        {f}" for f in fg["other"]))
        print("結果:", "全部通過" if all(ok for ok, _, _ in res3) else "有未通過項目")
        if not any("mini_repo" in str(v) for c in calls for v in c.get("input", {}).values()):
            return exit_code(allres)
    if res_sub:   # v3 判定只看主對話行，照跑；v1 是修 bug 判定，這類唯讀問答 log 不適用
        print("（有 subagent 呼叫，略過 v1 判定：這類 log 是唯讀問答）")
        return exit_code(allres)
    if res_hooks:   # 這個實驗的 mini_repo 本來就有已知 bug，任務也不是修它，v1 判定一定 FAIL 且會誤導；看「沒有新增失敗」要自己對照 check.py 原始輸出
        print("（有 hook 行，略過 v1 判定：mini_repo 的已知 bug 不在這個實驗範圍）")
        return exit_code(allres)
    # 沒成功呼叫 API 時不會有新 log，latest_log() 會默默撿到舊任務的 log，判定結果看似有效但不是這次的
    if repo.is_dir() and repo.stat().st_mtime > path.stat().st_mtime:
        print(f"⚠ 最新的 log（{path.name}）比 repo 的複製時間還舊——這次執行可能沒有成功呼叫 API，請看終端機輸出（v1 判定略過）")
        return 2
    if not any("mini_repo" in str(v) for c in calls for v in c.get("input", {}).values()):
        print("⚠ 這份 log 看起來不是 mini_repo 測驗，不做 v1 判定")
        return 2
    print("--- v1 完成標準判定 ---")
    res = judge(calls, errors, repo, fixture)
    for ok, label, detail in res:
        print(f"{'✅ PASS' if ok else '❌ FAIL'} {label}")
        if detail and (not ok or label.startswith("3.")):
            print("    " + detail.replace("\n", "\n    "))
    obs = [c["input"]["command"] for c in calls if c["name"] == "bash" and any(k in c["input"].get("command", "") for k in BYPASS)]
    print(f"觀察（不判 FAIL）：bash 繞過專用工具的指令 {len(obs)} 個" + "".join(f"\n    {o[:100]}" for o in obs))
    print("結果:", "全部通過" if all(ok for ok, _, _ in res) else "有未通過項目")
    return exit_code(allres + res)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
