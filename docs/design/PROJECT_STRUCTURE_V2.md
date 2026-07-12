# 项目目录结构 V3 (2026-07-12)

```
trading-bot/
├── main.py                     # 唯一启动入口
├── src/trading_bot/            # 全部生产代码
│   ├── engine.py               # 单进程500ms调度 (WS+REST hybrid)
│   ├── core/                   # 配置加载、运行时参数 (runtime_config, env_config)
│   ├── exchange/               # Binance API (gateway, client, protection, market_data)
│   ├── strategy/               # 信号扫描、评分、路由、市场结构
│   ├── risk/                   # 风险引擎、冷却、限流
│   ├── execution/              # 仓位监管、执行队列、幂等
│   ├── services/               # 持仓管理、连通性
│   ├── data/                   # WebSocket 行情客户端
│   ├── domain/                 # 类型定义 (TradeType, TradePlan, PositionKey)
│   ├── storage/                # 状态持久化 (state_store, migrations)
│   ├── integrations/           # QQ 通知
│   └── portfolio/              # 仓位对账 (reconciler)
├── config/
│   ├── runtime.yaml            # → symlink shared/config/runtime.yaml
│   └── hard_limits.yaml
├── shared/
│   ├── config/                 # 生产配置 (runtime.yaml v6, hard_limits.yaml)
│   ├── state/                  # bot_state.json
│   └── logs/                   # engine.log
├── logs/                       # 本地日志
├── tests/
│   ├── mock_binance.py         # Hedge Mode REST mock
│   ├── test_full_pipeline.py   # 22-case full pipeline
│   └── test_all.py
├── tools/
│   └── monitor.py              # 实时持仓监控
├── experimental/               # 已归档模块 (不参与生产)
│   └── archived_modules/
└── docs/
    ├── ALGORITHM.md            # 完整算法说明 (7章)
    ├── market_structure_protection.md
    ├── design/                 # 当前架构设计
    ├── operations/             # 运维手册 (TEST_PLAN, HEARTBEAT_CHECK)
    └── history/                # 已废弃文档
```

## 运行方式

```bash
source /etc/trading-bot/live.env
PYTHONPATH=src .venv/bin/python3 main.py
```

## 主循环 (engine.py, 500ms)

```
每次循环: 执行队列 + WS行情同步 + Supervisor持仓监控 + 插针狙击
每N秒:   持仓管理 (runtime: engine.manage_interval_seconds)
每N秒:   策略扫描 (runtime: engine.scan_interval_seconds)
每30秒:  健康检查
每5分钟: 孤儿Algo清理
```

## 禁止事项

- 不使用 systemd / cron
- 不启动 archive/ 或 experimental/ 下代码
- 不直接编辑 shared/config/runtime.yaml (先 chattr -i)
