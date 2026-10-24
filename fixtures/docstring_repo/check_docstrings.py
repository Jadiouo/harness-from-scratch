"""v3 測驗：檢查 repo 裡每個函式都有 docstring，而且沒有函式被刪掉。全部通過印 ALL OK。"""
import ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPECTED_FUNCS = 56   # 寫死：防止模型用「刪掉函式」來取巧通過

failures, total, missing = [], 0, []
for path in sorted(ROOT.rglob("*.py")):
    if path == Path(__file__).resolve() or "__pycache__" in path.parts:
        continue
    rel = path.relative_to(ROOT).as_posix()
    src = path.read_text()
    try:
        compile(src, rel, "exec")   # 能編譯才算數；順便抓模型改壞的縮排
        tree = ast.parse(src)
    except SyntaxError as e:
        failures.append(f"{rel} 無法編譯：{e}")
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            total += 1
            if not ast.get_docstring(node):
                missing.append(f"{rel}:{node.name}")

for m in missing:
    print(f"FAIL 沒有 docstring：{m}", file=sys.stderr)
for f in failures:
    print(f"FAIL {f}", file=sys.stderr)
print(f"函式總數 {total}（預期 {EXPECTED_FUNCS}），缺 docstring {len(missing)} 個")
if total != EXPECTED_FUNCS:
    failures.append("函式數量不符")
    print(f"FAIL 函式數量 {total} != 預期 {EXPECTED_FUNCS}（不可刪除或新增函式）", file=sys.stderr)
if failures or missing:
    sys.exit(1)
print("ALL OK")
