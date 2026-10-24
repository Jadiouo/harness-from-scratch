"""post_tool_use hook：mini_repo/shop/ 底下的 .py 不准有 print()（專案規定用 logging）。
stdin 收 agent 傳來的 JSON；有違規印原因到 stderr 並 exit 2，否則 exit 0。"""
import ast, json, os, sys
from pathlib import Path

data = json.load(sys.stdin)
path = data.get("input", {}).get("path", "")
if data.get("tool") not in ("edit", "write_file") or not path.endswith(".py"):
    sys.exit(0)

# 工具的 path 相對於 WORKDIR；用 AGENT_WORKDIR 組出實際檔案
file = (Path(os.environ["AGENT_WORKDIR"]) / path).resolve()
shop = (Path(os.environ["AGENT_WORKDIR"]) / "mini_repo" / "shop").resolve()
if not file.is_relative_to(shop) or not file.is_file():
    sys.exit(0)

source = file.read_text(encoding="utf-8")
try:
    compile(source, str(file), "exec")   # 等同 py_compile 的檢查，但不寫任何檔案（__pycache__）
except SyntaxError as e:
    print(f"{path}:{e.lineno} 語法錯誤：{e.msg}", file=sys.stderr)
    sys.exit(2)

for node in ast.walk(ast.parse(source)):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
        print(f"{path}:{node.lineno} 使用了 print()；本專案規定 shop/ 裡一律用 logging（logger = logging.getLogger(__name__)），請改掉",
              file=sys.stderr)
        sys.exit(2)
sys.exit(0)
