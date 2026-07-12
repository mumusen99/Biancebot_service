# 项目目录结构 V2

```text
bot_code/
├── main.py                     # 唯一启动入口
├── pyproject.toml              # Python 包配置
├── requirements.txt
├── .env.example
├── src/trading_bot/            # 全部生产代码
│   ├── engine.py               # 单进程调度器
│   ├── core/                   # 静态设置、Hermes 热配置
│   ├── exchange/               # Binance API 与行情读取
│   ├── strategy/               # 信号、指标、市场结构
│   ├── risk/                   # 冷却和账户风险
│   ├── execution/              # 订单计划与执行流程
│   ├── services/               # 持仓管理、连接健康
│   ├── storage/                # 后续 SQLite/状态仓储
│   └── integrations/           # 通知与外部集成
├── config/                     # Hermes 可调参数和硬限制
├── state/                      # 本地运行状态，不放源码包逻辑
├── logs/                       # 运行日志
├── ops/
│   ├── bin/                    # Hermes 受控运维命令
│   ├── systemd/                # 服务定义
│   ├── install/                # 安装和迁移脚本
│   └── hermes/                 # Hermes Skill
├── docs/
│   ├── design/
│   ├── operations/
│   └── history/
└── archive/                    # 禁止生产加载的旧代码和数据
```

生产代码不得从 `archive/` 导入。正式服务器只启动 `main.py`。
