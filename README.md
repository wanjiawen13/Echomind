# Echomind_MeiTuan

一个基于 EchoMind 框架重构的美团外卖客服项目。

当前仓库包含两部分：

- `Echomind_MeiTuan/`：Python 后端，提供意图识别、订单查询、RAG、Skills、记忆、监控和评测。
- `Echomind_MeiTuanFrontend/`：Vue 前端工作台，用于客服调试、查单和知识库联调。

## 项目目标

把 EchoMind 的通用客服框架迁移到外卖客服场景，形成一条可跑通的最小闭环：

用户提问 -> 意图识别 -> 工具查询 -> Agent 路由 -> 知识库检索 -> 回复生成 -> 记忆写入 -> 监控评测

## 核心能力

- 外卖客服意图识别
- 配送进度与订单状态查询
- 退款、取消、漏送、错送、优惠券、验证码等问题处理
- 订单查询工具 `order_tracking`
- RAG 知识库检索
- Skills 热加载
- 对话记忆与用户画像
- 监控与评测
- 前后端联调工作台

## 仓库结构

```text
Echomind_MeiTuan/
├── Echomind_MeiTuan/
│   ├── api/
│   ├── agents/
│   ├── core/
│   ├── data/
│   ├── evaluation/
│   ├── memory/
│   ├── mcp/
│   ├── monitor/
│   ├── skills/
│   ├── README.md
│   ├── requirements.txt
│   └── .env.example
└── Echomind_MeiTuanFrontend/
    ├── src/
    ├── docker/
    ├── README.md
    ├── package.json
    └── vite.config.js
```

## 后端启动

```powershell
cd "D:\Code\Encomind\EchoMind所有代码+简历\EchoMind_wjw\Echomind_MeiTuan\Echomind_MeiTuan"
Copy-Item .env.example .env
```

编辑 `.env` 后启动：

```powershell
$env:DEEPSEEK_API_KEY="你的真实key"
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:DEEPSEEK_MODEL="deepseek-chat"
python -m uvicorn api.main:app --host 127.0.0.1 --port 8011
```

常用地址：

- `http://127.0.0.1:8011/`
- `http://127.0.0.1:8011/docs`
- `http://127.0.0.1:8011/health`

## 前端启动

```powershell
cd "D:\Code\Encomind\EchoMind所有代码+简历\EchoMind_wjw\Echomind_MeiTuan\Echomind_MeiTuanFrontend"
npm install
npm run dev
```

前端地址：

```text
http://127.0.0.1:5173
```

开发模式下，前端默认代理到：

```text
/api/meituan -> http://127.0.0.1:8011
```

## API 简述

- `GET /`：服务入口
- `GET /health`：健康检查
- `POST /chat`：主客服对话
- `GET /skills`：Skills 列表
- `POST /skills/reload`：热加载 Skills
- `POST /search`：知识库检索
- `POST /knowledge/add`：批量导入知识
- `POST /knowledge/upload`：上传知识文件
- `GET /knowledge/stats`：知识片段统计
- `GET /monitor`：监控摘要
- `GET /metrics`：Prometheus 指标
- `POST /eval/run`：运行评测

## 当前实现说明

这版项目不是纯 demo，而是完整闭环原型：

- 订单查询使用 mock 数据源，方便演示和联调
- 订单、意图、路由、RAG、记忆、Skills、监控、评测都已串起来
- 后续如果要接真实业务，只需要把 `mcp/order_store.py` 替换成真实订单服务适配器

## 主要文档

- [后端说明](./Echomind_MeiTuan/README.md)
- [前端说明](./Echomind_MeiTuanFrontend/README.md)

