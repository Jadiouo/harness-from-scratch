"""把 docs/ 與 fixtures/README.md 有引用到的 logs/*.jsonl 複製到 evidence/logs/，並去識別化。可重跑。
用法：python scripts/sanitize_logs.py
替換：專案絕對路徑 -> <repo>、/tmp/claude-1000/... -> <tmp>、/home/<user> -> <home>。
掃描（只回報，不自動處理）：API key 樣式 sk-ant-、email、ANTHROPIC_ 變數。出現 key 或 email 時 exit 1，不寫檔。"""
import getpass, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, DST = ROOT / "logs", ROOT / "evidence" / "logs"
REF = re.compile(r"logs/([A-Za-z0-9_.-]+\.jsonl)")
# 順序重要：先換最長、最具體的
RULES = [
    (re.compile(re.escape(str(ROOT)) + r"(?=[/\"\\\s]|$)"), "<repo>"),
    (re.compile(r"/tmp/claude-\d+(?:/[^\"\\\s]*)?"), "<tmp>"),
    # 家目錄與使用者名稱在執行時取得，不寫死在程式裡（腳本本身會公開）
    (re.compile(re.escape(str(Path.home())) + r"(?=[/\"\\\s]|$)"), "<home>"),
    (re.compile(r"(?<=\s)" + re.escape(getpass.getuser()) + " " + re.escape(getpass.getuser()) + r"(?=\s)"), "<user> <user>"),   # ls -l 輸出裡的擁有者／群組
]
SCAN = {"key": re.compile(r"sk-ant-[A-Za-z0-9_-]*"),
        "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "ANTHROPIC_": re.compile(r"ANTHROPIC_[A-Z_]*=?\S{0,12}")}

def referenced():
    names = set()
    for p in [*(ROOT / "docs").rglob("*.md"), ROOT / "fixtures" / "README.md", ROOT / "evidence" / "README.md"]:
        if p.exists():
            names |= set(REF.findall(p.read_text(encoding="utf-8")))
    return sorted(n for n in names if (SRC / n).exists())

def main():
    out, bad = [], False
    for name in referenced():
        text = (SRC / name).read_text(encoding="utf-8")
        counts = {}
        for rx, rep in RULES:
            text, n = rx.subn(rep, text)
            counts[rep] = n
        hits = {k: sorted(set(rx.findall(text))) for k, rx in SCAN.items()}
        for line in text.splitlines():
            json.loads(line)   # 去識別化後每行仍要能解析
        if hits["key"] or hits["email"]:
            bad = True
        out.append((name, text, counts, hits))
    if bad:
        for name, _, _, h in out:
            if h["key"] or h["email"]:
                print("STOP", name, h["key"], h["email"])
        sys.exit("發現 key 或 email 樣式，未寫任何檔，請人工判斷")
    DST.mkdir(parents=True, exist_ok=True)
    for name, text, counts, hits in out:
        (DST / name).write_text(text, encoding="utf-8")
        print(f"{name}: 替換 {counts}  掃描 key={len(hits['key'])} email={len(hits['email'])} ANTHROPIC_={hits['ANTHROPIC_']}")
    print(f"共 {len(out)} 份，總 {sum(len(t.encode()) for _, t, _, _ in out)} bytes")

if __name__ == "__main__":
    main()
