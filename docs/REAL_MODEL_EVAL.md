# 真实模型评测说明

这一步回答一个具体问题：**换成真正的 DeepSeek 后，这个 Agent 到底在哪些任务上能完成、会怎么失败？**

它与 `pytest`、离线 `4/4` 的含义不同：

| 验证 | 使用模型 | 证明什么 | 不能证明什么 |
|---|---|---|---|
| `python -m pytest -q` | 模拟响应 | 代码路径、恢复、租约、参数校验等没有回归 | 真实模型会正确选择工具 |
| `python -m backend.app.harness.eval` | 确定性脚本模型 | 固定演示链路满足预期 | 大模型准确率 |
| `python -m backend.app.harness.eval_deepseek` | 真实 DeepSeek | 当前模型在这组项目自建案例上的实际表现 | 通用 Agent 能力或生产效果 |

## 评测集

`evals/deepseek_cases.json` 共 20 条，覆盖：

- 正常事故调查和同义改写；
- 只查统计或只查文档；
- 要求模型把相关性说成因果的陷阱；
- 当前数据不支持的问题与无关问题；
- 伪造的“500 条投诉”前提；
- Prompt Injection 和要求调用 Shell 的越权请求。

这是项目作者编写的回归集，不是公开 Benchmark，也不是真实公司的线上数据。

## 指标

每个案例分别检查：

- `completed`：运行是否正常结束；
- `tool_selection`：工具集合是否符合案例预期；
- `tool_execution`：每个调用是否得到成功且可解析的结果；
- `required_facts`：答案是否包含该题要求的关键事实；
- `policy`：该拒答时是否拒答、该提醒因果不足时是否提醒；
- `numeric_consistency`：答案是否触发已实现的数字矛盾检查；
- `forbidden_phrases`：是否照抄了案例中的虚假前提。

报告还会从每轮模型调用事件汇总模型响应耗时和 DeepSeek 返回的 Token 用量。这里的耗时是 API 调用在本机观察到的墙钟时间，不等同于服务端纯推理时间；工具耗时目前没有计入该字段。

程序不会再调用另一个大模型当裁判，所有规则都能在 `eval_deepseek.py` 中直接查看。规则仍不可能理解所有自然语言，因此失败案例最后需要人工复核。

## 安全运行

先用 3 条确认密钥和接口正常：

```powershell
$deepseekKey = (Get-Clipboard -Raw).Trim()
$env:DEEPSEEK_API_KEY = $deepseekKey
python -m backend.app.harness.eval_deepseek --limit 3
```

确认生成报告后，再运行完整 20 条：

```powershell
python -m backend.app.harness.eval_deepseek
```

默认报告写入 `evals/results/deepseek_latest.json`。报告不包含 API 密钥，但包含问题、模型答案、工具名和错误，提交前应人工看一遍。

运行结束后清除密钥：

```powershell
Remove-Item Env:DEEPSEEK_API_KEY
$deepseekKey = $null
Set-Clipboard -Value " "
```

评测返回非零退出码不代表程序崩溃：只要有案例未通过，命令就返回 1，便于 CI 发现回归。先打开 JSON 报告查看 `summary.failed_case_ids` 和每题 `checks`，不要为了得到 100% 随意降低标准。
