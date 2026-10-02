# 雪球组合变动监控

## 项目概述
定时监控雪球（xueqiu.com）模拟组合持仓变动，通过钉钉群机器人发送 Markdown 通知。

## 技术栈
- Python 3（类型注解风格）
- 依赖: `requests`, `python-dotenv`（可选）
- 监控主逻辑：单文件 `xueqiu_monitor.py`
- Web 控制台：`web_app.py`（FastAPI + 后台监控线程）+ `web/` 静态前端
- 一键启动：`python web_app.py` 或 `start.bat`（默认打开浏览器）

## 核心架构

### 主循环 (`main()`)
- 启动时立即检查一次
- 然后以 `CHECK_INTERVAL`（默认 300s）间隔定时轮询
- 每次遍历 `MONITORED_CUBES` 中的所有组合

### 类 & 关键函数

| 组件 | 职责 |
|------|------|
| `XueQiuClient` | 雪球 API 客户端，三级接口降级策略 |
| `DingTalkNotifier` | 钉钉机器人通知，含 60s 冷却期 |
| `detect_changes()` | 新旧持仓对比，检测新增/卖出/加减仓 |
| `build_markdown()` | 构建钉钉 Markdown 消息 |
| `monitor_once()` | 单次检查流程（获取持仓 → 防重复 → 对比 → 推送） |

### 防重复机制（双重）
1. **调仓记录 ID**：保存 `last_rb_id`，ID 未变则跳过
2. **冷却期**：同一组合 60 秒内不重复推送

### 配置（环境变量/.env）
- `XUEQIU_COOKIE`（必填）
- `DINGTALK_WEBHOOK`（必填）
- `MONITORED_CUBES`（必填，逗号分隔）
- `CHECK_INTERVAL`（默认 300）
- `WEIGHT_CHANGE_THRESHOLD`（默认 1.0%）
- `AT_ALL`（默认 false）
- `LOG_FILE`（默认 xueqiu_monitor.log）

## 开发规范
- 保持单文件架构，避免过度模块化
- API 接口变更时更新 `XueQiuClient` 中的降级策略
- 通知格式使用钉钉 Markdown（非标准 Markdown）
- 状态文件 `monitor_state.json` 保持向后兼容
- 启动自检保持严格（配置不完整直接退出）