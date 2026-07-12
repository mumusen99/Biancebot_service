# Trading Bot

生产代码采用标准 `src/` 布局。正式服务只运行根目录 `main.py` 或 `python -m trading_bot`。

## 目录

- `src/trading_bot/`：生产代码
- `config/`：Hermes 可调运行参数和只读硬限制
- `ops/`：部署、systemd、Hermes Skill 与受控运维命令
- `docs/`：设计、运行和历史文档
- `archive/`：旧入口、人工工具、回测与历史数据，不参与生产运行

## 启动

```bash
export PYTHONPATH="$PWD/src"
export LIVE_TRADING_ENABLED=YES
python main.py
```

旧脚本仅保存在 `archive/`，禁止配置为生产 cron。
