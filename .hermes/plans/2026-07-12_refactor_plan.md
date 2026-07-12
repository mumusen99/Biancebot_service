# Biancebot 架构重构实施计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 消除代码中多套重复逻辑，统一交易链路为 `全局快照 → 信号 → 路由 → 止损/止盈 → 风控 → 执行 → 保护 → 管理`，修复安全漏洞（无交易所硬止损、双向仓互相干扰、裸 except:pass）。

**Architecture:** 分 3 个阶段执行，每阶段独立可测（mock Binance），每阶段结束可部署。不推翻重写，而是把已有的正确模块接入主链路，删除绕过它们的旧路径。

**Tech Stack:** Python 3.12, Binance Futures API, pytest, mock server (tests/mock_binance.py)

---

## 阶段一：订单安全（3-4小时）

> **不涉及策略参数，纯安全修复。Mock 测试通过后直接部署。**

### Task 1: 建立 PositionKey，所有仓位操作按 symbol+side

**Objective:** 消除双向持仓时平 LONG 误读 SHORT 数量、撤 LONG 止损误删 SHORT 保护单的 bug。

**Files:**
- Create: `src/trading_bot/domain/position_key.py`
- Modify: `src/trading_bot/services/position_manager.py` — `market_close_position()` 加 positionSide 匹配
- Modify: `src/trading_bot/exchange/protection.py` — 撤单加 positionSide 过滤

**Step 1: 创建 PositionKey**

```python
# src/trading_bot/domain/position_key.py
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class PositionKey:
    symbol: str
    side: str  # "LONG" | "SHORT"

    def __str__(self) -> str:
        return f"{self.symbol}:{self.side}"
```

**Step 2: 修复 market_close_position()**

当前 L774-776:
```python
if p['symbol'] == symbol:
    actual_qty = ...
    break
```

改为:
```python
p_side = p.get('positionSide', 'LONG')
if p['symbol'] == symbol and p_side.upper() == side.upper():
    actual_qty = ...
    break
```

**Step 3: 修复撤单逻辑**

在 `protection.py` 的 `cancel_all_protection()` 增加 positionSide 参数和过滤。

**Verification:** 
```bash
cd /opt/trading-bot/current && PYTHONPATH="$PWD/src" .venv/bin/python3 tests/mock_binance.py 8765 &
# 创建 LONG+SHORT 双仓 → 平 LONG → 确认 SHORT 保护单不受影响
pytest tests/test_all.py -v -k "bidirectional"
```

---

### Task 2: 平仓统一经过 Gateway，强制 reduceOnly

**Objective:** 删除 `position_manager.py` 中直接 `_post("order", ...)` 的平仓路径，所有平仓经过 Gateway 并强制 `reduceOnly=true`。

**Files:**
- Modify: `src/trading_bot/services/position_manager.py` — `market_close_position()` 改用 gateway
- Modify: `src/trading_bot/exchange/gateway.py` — 新增 `place_exit_order()` 方法

**Step 1: Gateway 新增 place_exit_order**

```python
# exchange/gateway.py
def place_exit_order(self, symbol: str, side: str, qty: float) -> OrderResult:
    """强制 reduceOnly 的市价平仓"""
    close_side = 'SELL' if side.upper() == 'LONG' else 'BUY'
    params = {
        'symbol': symbol, 'side': close_side,
        'type': 'MARKET', 'quantity': str(qty),
        'positionSide': side.upper(),
        'reduceOnly': 'true',
    }
    return self._call("POST", self._fapi_v1, "order", params, ...)
```

**Step 2: market_close_position 改用 gateway**

删除 `_post()` 调用，改为 `gateway.place_exit_order()`。

**Verification:**
```bash
# mock测试: 确认平仓请求包含 reduceOnly=true, positionSide 正确
```

---

### Task 3: 开仓后创建交易所硬止损

**Objective:** 当前只有 WS 本地监控，断网/崩溃时无保护。改为开仓成交后立即创建交易所 STOP_MARKET。

**Files:**
- Modify: `src/trading_bot/strategy/scalper.py` — 开仓成功后调用保护单创建
- Modify: `src/trading_bot/exchange/protection.py` — `ensure_position_protection()` 确认止损存在

**Step 1: 开仓后创建止损**

在 scalper.py 的开仓成功后（LINE 1617 附近），增加：
```python
# 市价成交后立即创建交易所硬止损
protected = ensure_position_protection(sym, side, actual_price, actual_qty, risk_pct_plan, reward_pct_plan)
if not protected:
    logger.critical(f'🚨 {sym} 止损创建失败，紧急平仓')
    gateway.place_exit_order(sym, side, actual_qty)
    return
```

**Step 2: 确认止损存在**

`ensure_position_protection()` 创建后查询确认 `algoStatus == 'NEW'`，否则返回 False。

**Verification:**
```bash
# mock测试: 开仓 → 确认 STOP_MARKET 已创建 → 确认无止损时紧急平仓
```

---

### Task 4: 清理裸 except:pass

**Objective:** 安全关键路径（平仓、撤单、状态保存）不再吞异常。

**Files:**
- 全项目扫描修改

**Step 1: 扫描**
```bash
grep -rn "except:" src/trading_bot/ --include="*.py" | grep -v "except Exception"
grep -rn "except Exception:" src/trading_bot/ --include="*.py" | grep "pass"
```

**Step 2: 分类处理**
- 平仓/撤单异常 → 记录 + 加入 reconciliation 队列
- 市场数据异常 → 标记数据过期
- 通知异常 → 只打 WARNING，不影响交易

**Verification:**
```bash
grep -rn "except:" src/trading_bot/ | grep "pass" | wc -l  # 应为 0
```

---

## 阶段二：逻辑收敛（4-5小时）

> **统一 TradeType/TradePlan/路由，删除 scalper.py 和 engine.py 中的重复实现。**

### Task 5: 统一 TradeType 枚举

**Objective:** 用一个枚举替换散落的字符串和重复定义。

**Files:**
- Create: `src/trading_bot/domain/trade_type.py`
- Modify: `src/trading_bot/strategy/scalper.py` — 替换字符串
- Modify: `src/trading_bot/strategy/trade_router.py` — 替换字符串
- Modify: `src/trading_bot/strategy/sl_tp.py` — 替换字符串

```python
# domain/trade_type.py
from enum import Enum

class TradeType(str, Enum):
    TREND_PULLBACK = "TREND_PULLBACK"
    RANGE_REVERSAL = "RANGE_REVERSAL"
    MOMENTUM_SCALP = "MOMENTUM_SCALP"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    FAILED_BREAKOUT_REVERSAL = "FAILED_BREAKOUT_REVERSAL"
```

**Verification:** 编译通过，mock 测试不报 KeyError。

---

### Task 6: 将 engine.py 的 TP/SL 逻辑迁移到 PositionSupervisor

**Objective:** engine.py 不再直接计算止盈止损，改为调度。新增 PositionSupervisor 统一管理。

**Files:**
- Create: `src/trading_bot/execution/position_supervisor.py`
- Modify: `src/trading_bot/engine.py` — 删除 TP/SL 块，改为调度

**Step 1: 创建 PositionSupervisor**

```python
# execution/position_supervisor.py
class PositionSupervisor:
    """统一持仓监控：止损/止盈/移动止损/时间退出"""
    
    def __init__(self, gateway, market_cache, state_store):
        self.gateway = gateway
        self.market = market_cache
        self.store = state_store
    
    def evaluate_all(self) -> list[ExitDecision]:
        """对所有活跃持仓评估退出条件，返回决策列表（不下单）"""
        decisions = []
        state = self.store.load()
        for key, pos in state.get('positions', {}).items():
            sym, side = key.split(':')
            price = self._get_price(sym)
            if not price: continue
            
            # SL check
            sl = float(pos.get('sl_price', 0))
            if self._is_sl_hit(side, price, sl, pos):
                decisions.append(ExitDecision.exit(sym, side, price, 'SL'))
                continue
            
            # TP1/TP2 check (使用统一的分段止盈配置)
            # ...
        return decisions
```

**Step 2: engine.py 精简**

删除 engine.py L80-238 整个 try 块，改为：
```python
supervisor = PositionSupervisor(...)
while not _STOP:
    decisions = supervisor.evaluate_all()
    for d in decisions:
        execution_queue.submit(d)
    time.sleep(0.5)
```

**Verification:** mock 测试断网/穿透止损/TP1触发/移动止损场景。

---

### Task 7: 删除 scalper.py 中重复的路由和阈值

**Objective:** scalper.py 只做信号生成，路由/阈值/止损范围全部交给 trade_router.py。

**Files:**
- Modify: `src/trading_bot/strategy/scalper.py` — 删除 thresholds/stop_rules/TTL 字典
- Modify: `src/trading_bot/strategy/trade_router.py` — 成为唯一路由入口

**Step 1: 扩展 trade_router.py**

```python
class TradeRouter:
    def route(self, snapshot, signal) -> TradeType | None:
        """唯一交易类型路由"""
        ...
    
    def validate_scores(self, signal, trade_type) -> str | None:
        """四维门槛校验，返回 None=通过"""
        ...
    
    def get_stop_range(self, trade_type) -> tuple[float, float]:
        """返回 (min_stop_pct, max_stop_pct)"""
        ...
```

**Step 2: scalper.py 改为调用 router**

删除 scalper.py 中 L1282-1370 的 if/else 交易类型路由，改为：
```python
trade_type = trade_router.route(btc_env, best)
if not trade_type:
    continue  # 禁止
reject = trade_router.validate_scores(best, trade_type)
if reject:
    continue
stop_min, stop_max = trade_router.get_stop_range(trade_type)
```

**Verification:** mock 测试不同 BTC 环境下交易类型路由正确性。

---

### Task 8: 统一 TradePlan

**Objective:** 只有一个 TradePlan 数据结构，执行层只接受 TradePlan。

**Files:**
- Create: `src/trading_bot/domain/trade_plan.py`
- Modify: `src/trading_bot/strategy/scalper.py` — 字典改为 TradePlan
- Delete: `src/trading_bot/execution/order_planner.py` 中的重复 TradePlan

```python
@dataclass(frozen=True, slots=True)
class TradePlan:
    plan_id: str
    symbol: str
    side: str
    trade_type: TradeType
    entry_price: float
    sl_price: float
    tp_levels: tuple[TakeProfitLevel, ...]
    quantity: float
    margin: float
    risk_pct: float
```

**Verification:** 所有创建仓位的地方都用 TradePlan 而非字典。

---

## 阶段三：策略完善（2-3小时）

> **优化止损算法、止盈分配合规、时间止损。**

### Task 9: 止损算法改为 floor 模式

**Objective:** 止损距离 = max(结构距离+ATR缓冲, ATR下限, 成本下限, 最小距离)。超过最大距离拒绝交易，不做压缩。

**Files:**
- Create: `src/trading_bot/strategy/stop_loss.py`
- Modify: `src/trading_bot/strategy/sl_tp.py` — 接入新算法

**Verification:** 
```python
def test_stop_rejected_when_too_wide():
    # 结构止损距离 > max → 拒绝
def test_stop_not_smaller_than_cost():
    # 止损至少覆盖手续费+滑点
```

---

### Task 10: TP1 覆盖成本下限

**Objective:** 动量单 TP1 当前 0.4R 可能不够覆盖手续费。改为 `max(0.7R, 成本×2.5)`。

**Files:**
- Modify: `src/trading_bot/strategy/sl_tp.py`

---

### Task 11: 实现时间止损

**Objective:** 超时且未达 MFE 阈值 → 减仓或退出。

**Files:**
- Modify: `src/trading_bot/execution/position_supervisor.py`

**规则：**
- >1500s, MFE<0.15% → 强制平仓
- >480s, MFE<0.15% → 减仓50%  
- >360s, MFE<0.10% → 减仓50%

---

## 阶段四：清理（1小时）

### Task 12: 删除旧代码

- 删除 `src/trading_bot/legacy/` 目录
- 删除 `archive/` 中的旧脚本引用
- pytest 配置限制只扫描 tests/

### Task 13: 验收检查

```bash
# 1. engine.py 不含止盈止损公式
grep -c "tp1\|tp2\|risk_dist" src/trading_bot/engine.py  # 应为 0

# 2. 只有一个 TradePlan
grep -rl "class TradePlan" src/trading_bot/ | wc -l  # 应为 1

# 3. 只有一个 TradeType
grep -rl "class TradeType" src/trading_bot/ | wc -l  # 应为 1

# 4. 没有裸 except:pass
grep -rn "except:" src/trading_bot/ | grep "pass" | wc -l  # 应为 0

# 5. 编译通过
python -m compileall -q src/

# 6. mock 全链路测试
cd /opt/trading-bot/current && .venv/bin/python3 tests/mock_binance.py 8765 &
pytest tests/test_all.py -v
```

---

## 执行策略

| 阶段 | 时间 | 风险 | 可回滚 |
|------|------|------|--------|
| 一：安全 | 3-4h | 低 | ✅ 每步独立 |
| 二：收敛 | 4-5h | 中 | ✅ mock 测试后部署 |
| 三：策略 | 2-3h | 中 | ✅ 参数可调 |
| 四：清理 | 1h | 低 | ✅ 仅删除 |

**全程原则：**
- 每步先在 mock 环境验证
- 每阶段结束时部署到实盘观察 30 分钟
- 任何异常立即回滚到上一阶段 commit
- 不修改当前运行的策略参数（止损范围、阈值等），只动架构
