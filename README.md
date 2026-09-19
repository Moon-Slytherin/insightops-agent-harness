# InsightOps Agent Harness

用一个产品事故调查任务展示可恢复、可观测、可评测的工具调用运行时。数据是模拟的：45 条用户反馈、版本发布说明与事故文档。调查结论只能说明相关性，不能证明 SDK 变更导致支付故障。

## 快速运行

Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

浏览 <http://127.0.0.1:8000>。默认的**离线演示**使用确定性的脚本模型生成工具调用和答案，真实地执行本地只读工具、写入检查点和轨迹；它不是 LLM，也不能证明模型的泛化能力。选择 **OpenAI API** 前，在服务端设置 `OPENAI_API_KEY`，可选 `OPENAI_MODEL`（默认 `gpt-4.1-mini`），再启动服务。API 调用可能产生费用。客户端不接触密钥；真实 API 模式报错时不会悄悄切回脚本模型。

```bash
curl -s localhost:8000/api/harness/runs \
  -H 'Content-Type: application/json' \
  -d '{"question":"3.2.1 版本发布后支付失败投诉为什么增加？","mode":"demo"}'
```

返回 `run_id`、`status`、`answer`、`transcript`、`rounds`、`tool_calls`。用 `GET /api/harness/runs/{run_id}/events` 看逐步事件；`GET /api/harness/runs/{run_id}` 看状态；失败暂停时用 `POST /api/harness/runs/{run_id}/resume` 继续。

## 调用链和边界

1. 保存用户问题，调用模型适配器。真实模式用 Responses API 和严格的 function schema，手动传入完整对话和 `function_call_output`，`store=false`。
2. 模型返回的整轮输出先写入 SQLite 检查点，再执行它所请求的工具；工具结果与事件在同一事务中提交。进程重启后，未完成的工具调用继续执行，已提交的结果不再重复执行。
   同一个 run 通过 SQLite 租约保持单处理者：同时恢复返回 HTTP 409；执行期间续约，进程终止后租约过期可接管；旧处理者失去租约后无法写入新检查点。
3. 两个只读工具分别查询固定统计窗口内的支付反馈和本地知识文档。参数用 Pydantic 校验、拒绝额外字段，工具结果有长度限制；模型不能提交任意 SQL 或 Shell 命令。
4. 循环至最终回答或预算上限（默认最多 6 轮模型响应、8 次工具调用）；工具超时（默认 5 秒）或执行异常重试一次，仍失败则把失败结果交回模型。模型调用失败重试一次，仍失败则暂停。每次输出事件供调试与评测。

```text
backend/app/harness/
  model.py      Responses HTTP 适配器 + 显式脚本模型
  runtime.py    调用循环、预算、暂停与恢复
  tools.py      严格 schema、白名单、参数验证和只读执行
  store.py      SQLite 运行状态与事件日志
  eval.py       固定离线场景评测
evals/harness_cases.json
tests/test_harness.py
```

## 验证

```bash
pytest -q
python -m backend.app.harness.eval
```

新评测覆盖准确问题、改写问题、两种无证据拒答，并检查结束状态、工具选择、每次工具结果是否成功、答案关键词，以及投诉数量、增长率与文档路径是否对应真实工具输出。它也拒绝未出现在工具结果里的额外“几条投诉”，但无法判断任意自然语言结论的真假。26 条自动测试覆盖 SQLite 检查点、Windows 临时文件清理、进程重建后的未完成工具调用、恢复、参数边界、预算、API、并发租约与旧处理者写入保护；还故意注入无来源数字和缺失工具结果，验证评测会报错。模拟 HTTP 响应的适配器测试不需要密钥，也不能代替真实网络调用。旧的 `GET /api/evals` 及 `/api/investigate` 保留作为 V1 规则路由回归基线；新的 `GET /api/harness/evals` 只评估脚本演示模式，不能当作真实模型效果分数。

### 真实 OpenAI API 冒烟检查（需要你本机的有效密钥）

Windows PowerShell 中激活项目环境后运行：

```powershell
$secret = Read-Host "输入 OPENAI_API_KEY（输入内容不会显示）" -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
python -m backend.app.harness.smoke_openai
Remove-Item Env:OPENAI_API_KEY
```

请勿把真实密钥粘贴到对话、截图、源码或 Git。脚本使用临时数据库，检查是否完成一轮真实模型调用和两个工具，并打印状态、工具名及回答供人工核对。通过后仍需分别评估答案正确性与引用真实性；未运行前不得声称已通过真实模型端到端测试。命令可能产生 API 费用。

## 已知限制和后续工作

- 实际 OpenAI API 流程需要有效凭证和网络，未在离线环境里完成端到端验证；当前实测是脚本模型与本地工具的完整循环。
- 当前工具只读。若将来加入有副作用的工具，需要在调用外部系统时使用幂等键，并对需要审批的操作加入人工确认。
- 同 run 的多 worker 互斥有 SQLite 租约与续约测试；尚未做真实多进程压力与长时间运行测试。`asyncio.to_thread` 超时不能终止底层线程，因此只读工具可安全重试，有副作用的工具仍需外部幂等键。运行记录包含原始问题与模型输出，生产部署需增加鉴权、保留期和脱敏。
- 文档检索仍是简单关键词检索，任务范围局限于支付事故；真实模型在复杂问题上的能力与误差尚未评测。

刚开始接触 Python、测试与 Agent 时，可以从 [项目运行与验证入门](docs/LEARNING_NOTES.md) 开始读。
