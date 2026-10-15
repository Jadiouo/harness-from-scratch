"""把 logs/*.jsonl 摘要成一輪一行，結尾自動判定 v1 完成標準。
用法：summarize_log.py [log路徑] [--repo sandbox/mini_repo] [--fixture fixtures/mini_repo]"""
import argparse, difflib, json, os, posixpath, re, subprocess, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def latest_log():
    logs = sorted((ROOT / "logs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)   # 依修改時間，不依檔名
    if not logs:
        sys.exit("logs/ 裡沒有 log")
    return logs[-1]   # 檔名是時間戳，字典序最後就是最新

def brief(inp):
    # 主要參數：command / pattern / path 擇一，前 60 字元
    v = inp.get("command") or inp.get("pattern") or inp.get("path") or ""
    return str(v).replace("\n", " ")[:60]

# ==== 讀 log：依序取出工具呼叫，並從下一輪 request 的 messages 找回每個呼叫的結果 ====
def load_calls(path):
    calls, errors, seen = [], {}, set()
    for line in path.read_text().splitlines():
        d = json.loads(line)
        for m in d["request"].get("messages", []):
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

def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--repo", default="sandbox/mini_repo")
    ap.add_argument("--fixture", default="fixtures/mini_repo")
    args = ap.parse_args(argv)
    path = Path(args.log) if args.log else latest_log()
    repo, fixture = (Path(p) if Path(p).is_absolute() else ROOT / p for p in (args.repo, args.fixture))
    print(f"log: {path}")
    counts, seen, rewrites = Counter(), set(), []
    tin = tout = 0
    for line in path.read_text().splitlines():
        d = json.loads(line)
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
    print("--- 統計 ---")
    print("工具呼叫次數:", dict(counts) or "無")
    print(f"write_file 用在先前讀過／搜過的路徑（近似判斷）: {rewrites or '無'}")
    print(f"總 tokens: input={tin} output={tout} total={tin + tout}")

    calls, errors = load_calls(path)
    # 沒成功呼叫 API 時不會有新 log，latest_log() 會默默撿到舊任務的 log，判定結果看似有效但不是這次的
    if repo.is_dir() and repo.stat().st_mtime > path.stat().st_mtime:
        print(f"⚠ 最新的 log（{path.name}）比 repo 的複製時間還舊——這次執行可能沒有成功呼叫 API，請看終端機輸出")
        return 2
    if not any("mini_repo" in str(v) for c in calls for v in c.get("input", {}).values()):
        print("⚠ 這份 log 看起來不是 mini_repo 測驗，不做判定")
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
    return res

if __name__ == "__main__":
    sys.exit(2 if main(sys.argv[1:]) == 2 else 0)
