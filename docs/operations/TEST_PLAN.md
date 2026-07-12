# 超短线策略实盘测试方案

## 测试目标

在美国服务器直连 Binance 环境下，验证以下改善在实际行情中的表现：

1. **全局市场结构保护层** — 市场状态判断、方向许可、风险系数
2. **交易锁** — 多进程互斥
3. **信号去重** — 同信号只开一单
4. **止损巡检** — 裸仓自动补挂
5. **API熔断** — 异常时只读

## 测试阶段

### 阶段1：自检（部署后立即）
```
运行: python3 market_structure.py
检查输出:
  ✅ 市场状态不应为 DATA_INVALID
  ✅ 市场宽度应为 0~1
  ✅ 方向许可应输出合理的 primary_reason
  ✅ K 线延迟 < 90s

运行: python3 -m compileall .
  ✅ 无语法错误

手动: 检查 config.py
  ✅ BINANCE_TESTNET=false（实盘）
  ✅ API_KEY/API_SECRET 非空
  ✅ PROXY=""（直连）
```

### 阶段2：只读监控（第1天）
```
启动: TRADING_ENABLED=true python3 realtime_monitor.py
初始模式: MARKET_STRUCTURE_MODE=monitor（只记录，不阻止）

观察 24 小时日志:
  🔍 检查 market_structure 的状态切换记录
  🔍 检查 market_structure 是否应阻止但未阻止的交易
  🔍 检查 止损巡检 是否发现裸仓
  🔍 检查 交易锁 日志是否正常
  🔍 检查 信号去重 是否正常工作
```

### 阶段3：灰度启用（第2-3天）
```
设置: MARKET_STRUCTURE_MODE=active

观察指标:
  📊 禁止开单的原因分布（primary_reason）
  📊 被阻止的交易数量 vs 已执行交易数量
  📊 市场状态切换频率（不应过于频繁）
  📊 是否存在误阻止（本应赚钱的单被阻止）

回滚条件:
  🔴 连续 5 次误阻止
  🔴 市场状态每分钟切换一次
  🔴 RISK_OFF 对空单也禁止了
```

### 阶段4：完整运行（第4-7天）
```
启用全部保护:
  ✅ 市场结构保护（active）
  ✅ 交易锁
  ✅ 信号去重
  ✅ 止损巡检
  ✅ API熔断

对比:
  前7天 vs 后7天
  - 胜率变化
  - 最大回撤变化
  - 最大持有时间变化
  - 异常事件次数
```

## 监控命令

```bash
# 查看实时日志
tail -f /tmp/realtime_monitor.log | grep -E 'mkt_struct|MKT'

# 统计状态切换
grep '市场状态' /tmp/realtime_monitor.log

# 查看被阻止的交易
grep '市场结构禁止' /tmp/realtime_monitor.log

# 查看裸仓
grep '缺少止损' /tmp/realtime_monitor.log

# 当前状态快照
python3 -c "from market_structure import build_global_market_snapshot as s; import json; r=s(); print(json.dumps({'regime':r.regime.value,'breadth':r.breadth_smooth,'long_freeze':r.long_freeze},indent=2))"
```

## 回滚方案

```bash
# 回滚到 monitor 模式
export MARKET_STRUCTURE_MODE=monitor
# 重启实时监控
```

如果出现严重问题，直接关掉实时监控进程就行，交易所的 SL/TP 不受影响。
