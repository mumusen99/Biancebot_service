# 心跳检测清单 — AI上线时自动执行

每次心跳/上线时，按顺序检查以下标记：

## 1. KOL 情绪分析
- 检查: `.kol_check_due` 是否存在
- 动作: 运行 `python3 auto_news.py` (全自动，无需AI干预)
- 注意: auto_news.py 自 00:00 起已全自动，不再需要AI手搜

## 2. 待发送通知
- 检查: `notifications.json` 是否有内容
- 动作: 读取全部 → 发送微信 (message tool) → 清空
- 目标: `o9cq803e4jZs42z09ApLk-b8CtrY@im.wechat` (openclaw-weixin)

## 3. 每日总结
- 检查: `.summary_pending` 是否存在
- 动作: 读取 `daily_summary.json` → 格式化发送微信 → 清标志

## 4. 机器人生存检查
- 检查: `bot_state.json` → stopped 字段
- 动作: 如果 true → 微信通知用户 "机器人已停止"
