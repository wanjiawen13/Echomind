# Echomind_MeiTuanFrontend

美团外卖客服场景的 Vue 前端工作台，基于原 `EchoMindFrontend` 的轻量结构改造而来，只适配 `Echomind_MeiTuan` Python 后端。

## 功能

- 外卖客服聊天调试：配送进度、订单状态、退款、取消、优惠券、验证码等问题。
- 展示 `/chat` 返回的意图、意图组、Agent 路由、置信度、是否使用知识库、是否转人工。
- 展示订单查询结果：订单号、状态、预计时间、商家状态、骑手、异常原因、可取消/可退款。
- 调用 `/health`、`/monitor`、`/skills`、`/skills/reload`、`/knowledge/stats`。
- 支持知识库检索、文档添加、文件上传。
- 支持运行 `/eval/run` 查看后端评测结果。

## 后端要求

默认连接当前项目后端：

```text
http://127.0.0.1:8011
```

后端启动示例：

```powershell
cd Echomind_MeiTuan
$env:DEEPSEEK_API_KEY="你的真实key"
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:DEEPSEEK_MODEL="deepseek-chat"
python -m uvicorn api.main:app --host 127.0.0.1 --port 8011
```

## 本地运行

```powershell
cd Echomind_MeiTuanFrontend
npm install
npm run dev
```

访问：

```text
http://127.0.0.1:5173
```

开发模式下，Vite 会将：

```text
/api/meituan
```

代理到：

```text
http://127.0.0.1:8011
```

如需覆盖后端地址：

```powershell
$env:VITE_MEITUAN_API_URL="http://127.0.0.1:8011"
npm run dev
```

## 构建

```powershell
npm run build
```

产物目录：

```text
dist/
```

## Docker

先构建前端静态文件：

```powershell
npm run build
```

再启动 Nginx 容器：

```powershell
docker compose up -d --build
```

访问：

```text
http://127.0.0.1:5174
```

Docker 模式下，Nginx 会把 `/api/meituan/` 代理到宿主机的 `8011` 端口。

## 推荐验证样例

在聊天框输入：

```text
我的外卖还没到，订单号是 MT20260829001
```

预期现象：

- 右侧展示 `intent = order_status`
- `intent_group = delivery`
- `primary_agent = delivery_service`
- 订单面板展示 `MT20260829001`
- 回复中包含订单状态、预计送达时间和骑手/商家进度

也可以测试：

```text
订单 MT20260829005 显示送达了，但我没收到
优惠券为什么不能用
验证码一直收不到，怎么办？
```

