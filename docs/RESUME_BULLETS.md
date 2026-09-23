# 简历项目表述

## 项目名称

**AgentHarness Lab｜可恢复、可观测、可评测的多工具 Agent 运行时**

## 三条项目描述

- 基于 FastAPI、Pydantic 与 SQLite 自研多轮 Function Calling 运行时，接入 DeepSeek Chat Completions 与 OpenAI Responses API 适配层，实现工具白名单、严格参数校验、调用预算、超时重试及暂停恢复。
- 设计 SQLite checkpoint 与任务租约机制，支持进程中断后续跑、过期任务接管和旧 worker 写入隔离；记录模型调用、工具结果、错误、延迟与 Token 用量，并以44条 Pytest 用例覆盖恢复、并发和异常路径。
- 构建20条真实 DeepSeek 场景评测，分别度量工具选择、执行成功、证据命中、因果边界与数字一致性；经评测器误判审计后严格通过19/20，并保留未检索文档即错误拒答的失败案例用于回归分析。

## 使用时必须保留的限定

- “19/20”只针对项目自建20条案例，不写成行业 Benchmark；
- OpenAI 适配器只做了模拟 HTTP 契约测试，不写“OpenAI真实调用已验证”；
- 不写已实现 MCP、Sandbox、HITL、并行工具执行或多 Agent；
- 数据为模拟数据，不写“服务真实企业客户”或“线上准确率”。
