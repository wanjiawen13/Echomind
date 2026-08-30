# Echomind_MeiTuan 后端

Echomind_MeiTuan 是基于 EchoMind 框架轻量重构的美团外卖客服后端。
项目使用 FastAPI 对外提供接口，模型侧统一通过 DeepSeek OpenAI 兼容接口接入。

核心链路：

```text
用户请求
  -> FastAPI /chat
  -> MemoryManager 读取会话记忆与用户画像
  -> IntentRecognizer 识别外卖客服意图
  -> MCPToolManager 调用知识库或订单查询工具
  -> AgentOrchestrator 路由到配送、退款、平台或升级 Agent
  -> 生成客服回复
  -> 写回记忆
  -> Monitor / Evaluator 观察效果
```

## 项目结构

```text
Echomind_MeiTuan/
├── api/main.py
├── agents/agent_orchestrator.py
├── core/deepseek_client.py
├── core/intent_recognizer.py
├── core/skill_loader.py
├── memory/conversation_memory.py
├── mcp/tool_manager.py
├── mcp/order_store.py
├── mcp/knowledge_base.py
├── monitor/performance_monitor.py
├── evaluation/evaluator.py
├── skills/
├── data/demo_docs/
├── data/mock_orders.json
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## 能力范围

当前内置 4 类 Agent：

- `delivery_service`：订单状态、配送进度、骑手联系、商家出餐、地址修改。
- `refund_support`：退款、取消订单、漏送、错送、餐品质量、优惠券与补偿。
- `platform_support`：登录异常、验证码、App 报错、账号安全。
- `escalation`：投诉、高风险、规则无法判断或需要人工确认的问题。

当前支持的意图：

```text
order_status, delivery_progress, rider_contact, merchant_delay,
refund, cancel_order, address_change, coupon_rule,
missing_item, wrong_item, complaint,
account_security, login_issue, platform_error,
human_handoff, other
```

## 环境配置

复制环境变量示例：

```powershell
Copy-Item .env.example .env
```

最少配置：

```env
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

Redis 和 ChromaDB 都是可选增强项。未配置时，项目会自动使用内存兜底，便于先跑通演示。

## 本地启动

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

启动服务：

```powershell
python -m uvicorn api.main:app --host 127.0.0.1 --port 8011
```

常用入口：

```text
http://127.0.0.1:8011/
http://127.0.0.1:8011/docs
http://127.0.0.1:8011/health
```

## Docker 启动

```powershell
docker compose up -d --build
```

默认端口：

- API：`http://localhost:8011`
- Swagger：`http://localhost:8011/docs`
- ChromaDB：`http://localhost:8001`
- Prometheus：`http://localhost:9090`

## 接口说明

核心接口：

- `GET /`：项目入口信息
- `GET /health`：服务和 Agent 状态
- `POST /chat`：主客服对话接口
- `GET /skills`：查看已加载 Skills
- `POST /skills/reload`：热加载 Skills
- `POST /search`：搜索外卖客服知识库
- `POST /knowledge/add`：批量增加知识库文档
- `POST /knowledge/upload`：上传知识库文件
- `GET /knowledge/stats`：知识库片段数量
- `GET /monitor`：监控摘要
- `GET /metrics`：Prometheus 指标
- `POST /eval/run`：运行默认评测集

`POST /chat` 示例：

```json
{
  "message": "我的外卖还没到，订单号是 MT20260829001",
  "user_id": "u1001",
  "conv_id": "demo-001"
}
```

示例返回会包含：

```json
{
  "response": "已帮您查询到订单 MT20260829001，当前状态是：骑手已接单。预计还需要 18 分钟左右。",
  "intent": "order_status",
  "intent_group": "delivery",
  "agent_type": "delivery_service",
  "tracking_info": {
    "found": true,
    "clarify_needed": false
  }
}
```

## 订单查询工具

当前没有接真实美团接口，使用 `data/mock_orders.json` 作为演示订单源。

`order_tracking` 输入：

```json
{
  "user_id": "u1001",
  "order_id": "MT20260829001",
  "phone_last4": "1234"
}
```

工具会返回订单状态、商家状态、骑手信息、ETA、异常原因、是否可取消和是否可退款。

## Skills

内置 Skills：

- `skills/delivery_service/SKILL.md`
- `skills/refund_support/SKILL.md`
- `skills/platform_support/SKILL.md`

每个 Skill 都包含角色定位、核验要求、回复风格、处理流程和禁止事项。

修改后可以调用：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8011/skills/reload"
```

## 验证命令

健康检查：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8011/health" | ConvertTo-Json -Depth 6
```

查订单：

```powershell
$body = @{
  message = "我的外卖还没到，订单号是 MT20260829001"
  user_id = "u1001"
  conv_id = "demo-001"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8011/chat" -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 8
```

运行评测：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8011/eval/run" -ContentType "application/json" -Body "{}" | ConvertTo-Json -Depth 8
```

## 当前边界

- 订单查询使用 mock 数据，不连接真实美团生产系统。
- 退款、取消、赔付只做客服解释和流程引导，不执行真实资金动作。
- MCP 当前是内部工具管理层，不暴露完整外部 MCP 协议。
- 没有单独前端，Swagger 和 HTTP API 用于验证。

后续如果要扩展真实业务，只需要把 `mcp/order_store.py` 的 mock 查询替换为真实订单服务适配器，并继续复用现有意图、Agent、Skills、记忆、监控和评测链路。

