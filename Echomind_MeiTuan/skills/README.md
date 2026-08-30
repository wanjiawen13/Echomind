# Skills

本目录存放外卖客服场景的动态业务规则，启动时由 `SkillManager` 自动加载。

当前内置三类客服技能：

- `delivery_service`：订单状态、配送进度、骑手联系、商家出餐、地址修改。
- `refund_support`：退款、取消订单、漏送、错送、餐品质量、补偿说明。
- `platform_support`：登录异常、验证码、App 报错、账号安全、页面排障。

每个 Skill 使用 front matter 声明适用关键词和 Agent：

```yaml
---
name: 外卖配送客服规范
description: 适用于配送进度、订单状态、骑手联系和商家出餐问题
keywords: 外卖,配送,骑手,送到,多久到,订单,出餐,催单
agents: delivery_service
enabled: true
---
```

更新规则后可调用 `POST /skills/reload` 热加载。
