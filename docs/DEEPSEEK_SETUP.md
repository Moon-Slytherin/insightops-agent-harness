# DeepSeek 真实模型模式

DeepSeek 模式按照官方“首次调用 API”和 Tool Calls 指南调用 Chat Completions API。默认模型为 `deepseek-flash`；密钥、余额和赠送额度由 DeepSeek 平台单独管理，ChatGPT Plus 与 OpenAI API 密钥不能替代。DeepSeek 并非免费无限调用：没有可用赠送额度或充值余额时，真实请求会失败。

在仓库根目录使用 PowerShell：

```powershell
conda activate insightops
$secret = Read-Host "输入 DEEPSEEK_API_KEY（输入内容不会显示）" -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
python -m backend.app.harness.smoke_deepseek
Remove-Item Env:DEEPSEEK_API_KEY
```

这条冒烟检查应该打印 `status=completed`、工具名和回答；它验证一次真实模型、两种本地工具的连接，不等于准确率评测。密钥只写进当前终端的环境变量，不会进入 Git。不要把密钥发给别人、贴进聊天或截图。

网页模式：先在启动后端服务的终端设置同一个 `DEEPSEEK_API_KEY`，然后启动 `python -m uvicorn backend.app.main:app --reload`；打开 <http://127.0.0.1:8000>，下拉选择 `DeepSeek API`。停止服务、清除环境变量后再次启动网页会回到缺少密钥的状态。

离线模式及 `python -m backend.app.harness.eval` 不用密钥，但离线脚本的评测分数不是 DeepSeek 模型准确率。代码对 DeepSeek 的请求格式与余额失败恢复做了模拟 HTTP 测试；在提供真实密钥、余额和网络前，不能宣称真实 DeepSeek 调用已通过。

适配器在 DeepSeek Chat Completions 消息与项目内部的统一调用记录之间转换；工具参数仍由本地 Pydantic 严格校验，运行时负责逐个执行工具。DeepSeek 使用非思考模式，避免多轮工具调用时额外维护 `reasoning_content`。
