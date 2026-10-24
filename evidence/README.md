# evidence/ — experiment logs behind the numbers

These are the raw per-turn logs (`jsonl`, one request/response record per line) that `docs/PROGRESS.md` and the README results table cite. Only logs that are actually referenced from `docs/` or `fixtures/README.md` are included (14 files, about 2 MB); other local runs are not published. Every experiment is a **single run (n=1)**; read the numbers as demonstrations, not statistics.

## Log index

| Log | Experiment | PROGRESS.md section |
| --- | --- | --- |
| `20261015-225449.jsonl` | v0 completion check: "create hello.py and run it" (3 tools, 3 turns) | v0 / 狀態 |
| `v1-A-baseline.jsonl` | v1 baseline: model prefers `bash cat/find` over `read_file`/`grep`/`glob` | v1 真實測驗 A |
| `v1-B-bash-desc.jsonl` | v1 variant B: one sentence added to the `bash` tool description | v1 真實測驗 B |
| `v2-run1-all-yes.jsonl` | v2 run 1: every permission prompt answered `y`, so no rejection was observed | v2 真實測驗第 1 次 |
| `v2-run2-reject.jsonl` | v2 run 2: rejection with a stated reason (confounded: the reason told the model what to do) | v2 真實測驗第 2 次 |
| `v3-run1-bindfail.jsonl` | v3 run 1: summary request returns HTTP 400 (thinking signature bound to system prompt) | v3 真實測驗第 1 次 |
| `v3-run2-pass.jsonl` | v3 run 2: 91 tool calls, 4 compactions under an artificially low 8,000-token threshold | v3 真實測驗第 2 次 |
| `v4-mem-with.jsonl` | v4 memory, with `sandbox/CLAUDE.md` | v4-1 真實測驗 |
| `v4-mem-control.jsonl` | v4 memory, control (no `CLAUDE.md`) | v4-1 真實測驗 |
| `v4-todo-run1-nohint.jsonl` | v4 TODO, tool description only: `todo_write` used 0 times | v4-2 真實測驗第 1 次 |
| `v4-todo-run2-hint.jsonl` | v4 TODO, one sentence added to the system prompt: `todo_write` used 6 times | v4-2 真實測驗第 2 次 |
| `v4-sub-run-task.jsonl` | v4 subagent, experiment group (`task` tool) | v4-3 真實測驗 |
| `v4-sub-run-control.jsonl` | v4 subagent, control (main conversation searches itself) | v4-3 真實測驗 |
| `v4-hooks-run1.jsonl` | v4 hooks: post hook reports `print` use, model switches to `logging` | v4-4 真實測驗 |

Task prompts and fixtures for every experiment are in `fixtures/README.md`. All runs used `claude-opus-5-5`.

## Replaying a log

```bash
.venv/bin/python scripts/summarize_log.py evidence/logs/v4-sub-run-task.jsonl
```

This prints one line per turn (tokens, tool calls, compactions) and then the completion-criteria judgement for that stage. No API key or network access is needed.

Caveats when replaying (the judge was written for live use right after a run):

- Some checks read the **current state of `sandbox/`**, not the log: the v1 judge (is the bug fixed?) and item 4 of the v3 judge (`check_docstrings.py`). On a fresh clone, or after another experiment has modified `sandbox/`, these items can show FAIL for a log that passed when it was recorded. The v1 judge also prints for logs that are not v1 runs (v2, subagent control) and its FAIL lines there are not meaningful.
- Logs recorded before a judge item existed are judged by the current rules. `v2-run2-reject.jsonl` fails item 5 (skipping calls after a rejection) on purpose: the feature was added because this log showed the problem.
- `v3-run1-bindfail.jsonl` shows FAIL by design (it is the failed run). `v4-todo-run1-nohint.jsonl` prints "long task but no TODO tool used", which is the experimental result.
- `20261015-225449.jsonl` and the two memory logs are not `mini_repo` runs, so the v1 judge is skipped (exit code 2).
- Replay done at publication time: every file loads and summarizes without error. The stage-appropriate judges report all-pass for `v3-run2-pass`, `v4-todo-run2-hint`, `v4-sub-run-task`, `v4-hooks-run1` and `v4-mem-with`.

## De-identification

`scripts/sanitize_logs.py` copies the referenced logs from the local, git-ignored `logs/` and rewrites them (re-runnable):

- the absolute project path becomes `<repo>`, `/home/<user>` becomes `<home>`, `/tmp/claude-<uid>/...` becomes `<tmp>`, and the user/group name in `ls -l` output becomes `<user>`;
- it scans for `sk-ant-` key patterns, e-mail addresses and `ANTHROPIC_` variables, and refuses to write anything if a key or e-mail appears;
- every line is re-parsed with `json.loads` after rewriting.

At publication time: 10 `<repo>` replacements and 61 `<user>` replacements (all in the two v1 logs), no keys, no e-mail addresses, no `ANTHROPIC_` strings. Model output and tool results are otherwise untouched, including the Chinese prompts.

## 繁體中文簡述

這個資料夾放 `docs/PROGRESS.md` 與 README 結果表所引用的原始實驗 log（共 14 份，約 2 MB，只帶出被引用的）。每個實驗都只跑 1 次（n=1），屬示範，不是統計結論。上表列出每份 log 對應的實驗與 PROGRESS 小節。重播方式：`.venv/bin/python scripts/summarize_log.py evidence/logs/<檔名>`，不需要 API key。注意 v1 判定與 v3 第 4 項讀的是目前 `sandbox/` 的狀態，不是 log，所以在乾淨 clone 上可能顯示 FAIL；詳見上面的說明。log 已用 `scripts/sanitize_logs.py` 去識別化（本機路徑換成 `<repo>`、`<home>`、`<tmp>`），掃描未發現 API key 或 email。
