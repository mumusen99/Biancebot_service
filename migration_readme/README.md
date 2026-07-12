# 量化交易机器人 · 部署文档

## 概述

自动交易系统，对接 Binance 合约，支持波段、超短线、扫盘三种策略，统一仓位管理，实时技术面监控。

---

## 一、系统架构

```
realtime_monitor.py  ← 主循环（一直跑）
  ├── 每5秒  → 检查持仓技术面，吃反转信号就平仓
  │               RSI超买回落 · 趋势反转 · 强行止损
  │
  ├── 每60秒 → 扫描超短线入场信号
  │               EMA9回调 · RSI中性区 · 缩量确认
  │               → 限价/市价开单 → 设动态SL/TP
  │
  └── 每60秒 → 统一仓位管理 (position_manager)
                  止盈止损触发 · 追踪止损 · 时间退出 ·
                  技术退出 · 新持仓自动打标

position_manager.py   ← 统一仓位管理中间层
  ├── sync_all_positions()
  │     从交易所拉所有持仓 → 打标(波段/超短线/扫盘)
  │
  └── manage_all_positions()
        止盈止损触发 · 追踪止损 · 时间退出 · 技术退出

scalper.py             超短线信号扫描+开单（被实时监控调用）
auto_trader.py         波段策略（每小时心跳调用）
auto_scan_hourly.py    全市场扫盘（每6小时）
review_orders.py       复查挂单+条件委托
proxy_guard.py         代理自动恢复（仅NAS需要，美国直连不需要）
```

## 二、策略说明

### 策略1：波段（auto_trader.py）
- 评分制：技术分析(0~15分) + KOL情绪(±50) 综合评分
- 杠杆 3x，限价挂单等回调入场
- 止损：保证金 -10.5%（价格 -3.5%）
- 止盈：保证金 +40%（价格 +13.3%）
- 追踪止损：PnL≥3% → 止损移到 -1%；≥6% → 保本；≥12% → +2%
- 时间退出：72小时未达标自动平仓

### 策略2：超短线（scalper.py + 实时监控）
- 5m K线，EMA9/EMA21 趋势过滤
- 回调到EMA9附近 + RSI 40~58 中性区 + 缩量 → 入场
- 杠杆 3x，每单保证金 10U
- 动态止损：ATR+EMA21+近期高低点计算
- 动态止盈：近期高点或 1.5x~2x 止损距离（上限+3%价格）
- 追踪止损：PnL≥3% → 止损移到 -1%；≥5% → 保本；≥8% → +2%
- 时间退出：48小时
- **实时技术退出**（5秒级）：
  - 趋势反转跌破EMA21 → 平仓
  - RSI>70跌破EMA9 → 超买回落平仓
  - 布林上轨受压+RSI>65 → 平仓
  - 亏损> -6% → 强行止损

### 策略3：扫盘（auto_scan_hourly.py）
- 全市场筛选：涨跌幅+成交量+技术面
- 杠杆 5x，市价折价1%限价入场
- SL：价格 -1%（保证金 -5%）
- TP1：+0.6%（卖60%），TP2：+1.2%（卖40%）
- 时间退出：36小时

## 三、安装部署

### 环境要求
- Ubuntu 24.04+（或任何 Linux 发行版）
- Python 3.11+
- 网络：美国服务器直连 Binance（无需代理）
- 内存：≥256MB
- 磁盘：≥50MB

### 快速安装

```bash
# 1. 解压
tar xzf trading_bot_migration.tar.gz
cd trading_bot_migration

# 2. 一键安装
sudo bash setup_new_server.sh
```

### 手动安装

```bash
# 1. Python 环境
sudo apt-get install -y python3 python3-pip python3-venv
python3 -m venv /path/to/venv
/path/to/venv/bin/pip install -r requirements.txt

# 2. 修改 config.py
#    设置你的 Binance API Key/Secret
#    PROXY = ""  （美国直连）

# 3. 启动实时监控
nohup /path/to/venv/bin/python3 /path/to/bot_code/realtime_monitor.py \
  > /tmp/realtime_monitor.log 2>&1 &

# 4. 验证
tail -f /tmp/realtime_monitor.log
```

### Hermes AI Agent 集成

Hermes 负责管理进程生命周期，不需要额外配置：

```python
# Hermes task 示例：启动监控
import subprocess
subprocess.Popen([
    "/path/to/venv/bin/python3",
    "/path/to/bot_code/realtime_monitor.py"
])

# Hermes task 示例：查看状态
import json
from pathlib import Path
state = json.loads(Path("/path/to/bot_code/bot_state.json").read_text())
print(f"当前持仓: {len(state['positions'])} 单")
print(f"累计盈亏: {state['total_pnl']:.2f} U")
```

## 四、文件说明

| 文件 | 用途 |
|------|------|
| `realtime_monitor.py` | **主程序**：实时监控持仓（5秒级）+ 扫描新信号（60秒级） |
| `position_manager.py` | 统一仓位管理：同步、打标、止盈止损、追踪、时间退出 |
| `scalper.py` | 超短线信号扫描+开单 |
| `auto_trader.py` | 波段策略（KOL情绪+技术分析） |
| `auto_scan_hourly.py` | 全市场扫盘 |
| `review_orders.py` | 复查挂单+条件委托 |
| `proxy_guard.py` | 代理自动恢复（美国服务器不需要） |
| `trader.py` | Binance API 封装（下单/精度处理） |
| `data_fetcher.py` | 行情数据获取 |
| `indicators.py` | 技术指标计算 |
| `config.py` | 配置（API Key / 策略参数） |
| `bot_state.json` | 仓位状态 + 交易记录 |
| `sentiment.json` | KOL 情绪数据 |
| `notifications.py` | 推送通知（微信/Telegram） |
| `format_report.py` | 持仓报告格式化 |
| `package_for_migration.sh` | 打包迁移脚本 |

## 五、监控指标

实时监控日志（`tail -f /tmp/realtime_monitor.log`）：
- `🛑 实时退出 XXX LONG: 趋势转空` → 技术退出
- `🛑 止损触发: XXX -8.5%` → 统一管理止损
- `🎯 止盈触发: XXX +12.3%` → 统一管理止盈
- `🔄 XXX 追踪止损: PnL+5.2% → 止损 0.1234` → 追踪移动
- `新信号: AVAXUSDT LONG 得分8.5` → 超短线开单
- `🔌 代理连续失败3次` → 代理自动恢复（仅NAS）

## 六、常见问题

**Q: 美国服务器需要代理吗？**
A: 不需要。`config.py` 中 `PROXY = ""` 即可直连 Binance。

**Q: 实时监控异常退出怎么办？**
A: Hermes 或 systemd 设置为自动重启即可。进程退出后重新拉起会自动同步交易所持仓。

**Q: 怎么改策略参数？**
A: `position_manager.py` 中的 `STRATEGY_PROFILES` 字典，每个策略的止盈止损/追踪/时间退出均可调。

**Q: 持仓对不上？**
A: 运行 `python -c "from position_manager import run_full_cycle; run_full_cycle()"` 会强制同步交易所数据。

---

*最后更新: 2026-07-11*
