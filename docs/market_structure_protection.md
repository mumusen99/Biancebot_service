# 超短线策略补充：全局市场结构保护层修订建议

## 1. 修订目标

本文件用于补充和修正《超短线策略补充：全局市场结构保护层》中可能导致实盘误判的部分。

原方案核心方向正确，尤其是：

- 单币信号必须服从全局市场结构；
- BTC 结构转弱后冻结山寨多单；
- 批量止盈后进入利润保护；
- 连续亏损后暂停交易；
- EMA 回踩不能脱离趋势环境独立使用。

本次修订重点解决：

- `DISTRIBUTION` 和 `RISK_OFF` 触发过于敏感；
- 市场状态频繁切换；
- 摆动点确认延迟；
- ATR 阈值不统一；
- 冻结解除过于严格；
- 风险系数重复叠加；
- `RISK_OFF` 后低位追空；
- ETH 和市场宽度作用不明确；
- 同一轮扫描使用不同全局状态；
- 数据过期时仍继续开仓。

---

# 2. 全局状态判断原则

市场状态不能由单一指标决定。

必须采用：

```text
结构条件
+
趋势确认
+
市场宽度确认
+
状态持续时间
```

禁止仅凭以下单一条件直接进入风险状态：

- 价格未站回 EMA20；
- EMA20 轻微向下；
- 单次 Lower High；
- 单根 K 线跌破前低；
- 市场宽度短暂低于阈值。

---

# 3. DISTRIBUTION 修订规则

## 3.1 问题

原规则允许以下任意一项触发：

```text
连续 Lower High
未站回 EMA20
BTC 未创新高
市场宽度下降
```

这会导致正常回调被误判为派发。

## 3.2 建议采用评分制

```text
DISTRIBUTION_SCORE
```

评分建议：

| 条件 | 分值 |
|---|---:|
| 连续两个有效 Lower High | +2 |
| 最近三个反弹高点依次降低 | +2 |
| 反弹未站回 5m EMA20 | +1 |
| 5m EMA20 斜率向下 | +1 |
| 市场宽度连续 3 分钟下降 | +1 |
| BTC 未创新高但山寨批量冲高 | +1 |
| 上涨成交量持续衰减 | +1 |
| 价格上涨但新高币种比例下降 | +1 |

状态判定：

```text
score < 2：
保持当前状态

score = 2：
进入 DISTRIBUTION_WATCH

score >= 3：
进入 DISTRIBUTION
```

## 3.3 强制要求

进入 `DISTRIBUTION` 至少满足：

```text
一个结构条件
+
一个弱势确认条件
```

结构条件包括：

- 连续 Lower High；
- 最近三个反弹高点递减；
- 关键高点无法突破。

弱势确认包括：

- 未站回 EMA20；
- EMA20 斜率向下；
- 市场宽度下降；
- 成交量衰减。

---

# 4. RISK_OFF 修订规则

## 4.1 问题

`5m EMA20 向下` 不应单独触发 `RISK_OFF`。

EMA 只能作为确认条件，不能代替结构破坏。

## 4.2 建议条件

进入 `RISK_OFF` 必须满足以下之一：

### 结构破坏 A

```text
BTC 跌破 confirmed_swing_low
且有效跌破幅度超过阈值
```

### 结构破坏 B

```text
BTC 已形成 Lower High + Lower Low
```

同时必须满足至少一个确认条件：

- 5m EMA20 斜率向下；
- 破位后连续 3 根 1m K 未收回；
- 市场宽度低于 40%；
- 下跌成交量明显放大；
- ETH 同步进入弱势结构。

推荐逻辑：

```python
risk_off = (
    structure_broken
    and confirmation_count >= 1
)
```

---

# 5. 状态抗抖动机制

## 5.1 最短保持时间

建议：

```text
RISK_ON：最少保持 5 分钟
PULLBACK：最少保持 3 分钟
DISTRIBUTION_WATCH：最少保持 3 分钟
DISTRIBUTION：最少保持 5 分钟
RISK_OFF：最少保持 10 分钟
CHOP：最少保持 5 分钟
```

紧急结构破坏可以立即升级，但恢复必须经过确认。

## 5.2 状态升级和降级使用不同门槛

例如：

```text
PULLBACK → RISK_OFF：
结构破坏后可快速触发

RISK_OFF → PULLBACK：
必须连续两根 5m K 收回关键位

DISTRIBUTION → RISK_ON：
必须重新形成 Higher Low
且市场宽度恢复
```

## 5.3 建议状态优先级

```text
SYSTEM_HALT
> DATA_INVALID
> LOSS_COOLDOWN
> GLOBAL_DIRECTION_FREEZE
> RISK_OFF
> DISTRIBUTION
> PROFIT_LOCK_MODE
> PULLBACK
> CHOP
> RISK_ON
```

---

# 6. 摆动高低点修订

## 6.1 区分候选点和确认点

必须区分：

```text
candidate_swing_high
confirmed_swing_high

candidate_swing_low
confirmed_swing_low
```

只有 `confirmed` 状态的摆动点才能用于：

- Lower High；
- Lower Low；
- Higher High；
- Higher Low；
- 前低破位；
- 全局方向冻结。

## 6.2 确认条件

建议：

```text
局部高点确认：
后续回落 >= max(0.5 × ATR_1m, 最小百分比阈值)

局部低点确认：
后续反弹 >= max(0.5 × ATR_1m, 最小百分比阈值)
```

## 6.3 ATR 统一

统一定义：

```text
ATR_1m = ATR(14)，仅使用已收盘 K 线
ATR_5m = ATR(14)，仅使用已收盘 K 线
```

禁止使用未收盘 K 线计算结构阈值。

---

# 7. 结构阈值下限

当市场低波动时，ATR 过小可能导致结构信号过度敏感。

建议：

```text
lower_high_threshold
= max(
    0.2 × ATR_5m,
    current_price × 0.0005
)

lower_low_threshold
= max(
    0.2 × ATR_5m,
    current_price × 0.0005
)
```

其中 `0.0005` 表示 0.05%，可根据实盘数据调整。

---

# 8. GLOBAL_LONG_FREEZE 修订

## 8.1 触发条件量化

推荐触发条件：

```text
BTC close_1m
<
previous_confirmed_swing_low
-
max(0.2 × ATR_5m, price × 0.0005)
```

或者：

```text
BTC low_1m
<
previous_confirmed_swing_low
-
0.5 × ATR_1m

且破位后连续 3 根 1m K
没有收盘重新站回前低
```

## 8.2 冻结时长分级

```text
普通有效破位：
20 分钟

Lower High + Lower Low：
30 分钟

放量破位且市场宽度 < 35%：
45 分钟
```

## 8.3 两阶段解除

### 第一阶段：解除完全冻结

同时满足：

- 连续两根 5m K 收回破位点；
- 市场宽度恢复到 50% 以上。

处理：

```text
允许 A 级确认型多单
long_factor = 0.3
```

### 第二阶段：完全恢复

同时满足：

- 重新形成 Higher Low；
- 市场宽度高于 55%；
- BTC 重新站稳 5m EMA20。

处理：

```text
恢复正常多单权限
```

---

# 9. “快速收回”统一定义

禁止使用模糊描述。

建议定义为：

```text
破位后 3 根 1m K 内，
至少有一根收盘价重新站回 previous_swing_low
```

或者：

```text
破位后 5 分钟内，
BTC 收盘重新站回：
previous_swing_low + 0.1 × ATR_5m
```

代码中只能保留一种统一定义。

---

# 10. ETH 结构作用

BTC 拥有全局否决权，ETH 用于确认和调整风险。

建议：

```text
BTC 权重 = 0.7
ETH 权重 = 0.3
```

规则：

```text
BTC RISK_OFF：
直接冻结山寨多单

BTC PULLBACK + ETH RISK_OFF：
多单风险系数最多 0.3

BTC RISK_ON + ETH DISTRIBUTION：
禁止 B 级以下山寨多单

BTC 与 ETH 同时 RISK_ON：
正常放行

BTC DISTRIBUTION + ETH RISK_OFF：
优先观望或只做反抽空
```

---

# 11. 市场宽度修订

## 11.1 Top50 样本池

建议：

```text
每天 UTC 00:00 更新一次
按过去 7 天合约成交额排序
当天运行期间保持不变
```

排除：

- 稳定币；
- 杠杆代币；
- 指数产品；
- 新上线且数据不足的币；
- 流动性异常币种。

## 11.2 宽度平滑

```text
breadth_raw
= Top50 中位于 5m EMA20 上方的比例

breadth_smooth
= breadth_raw 的 3~5 分钟 EMA
```

使用原则：

```text
普通状态判断：
使用 breadth_smooth

紧急破位判断：
可参考 breadth_raw
```

## 11.3 建议阈值

```text
breadth_smooth > 55%：
多单环境正常

45%~55%：
中性或回调环境

35%~45%：
弱势环境

< 35%：
只允许空单或观望
```

---

# 12. PROFIT_LOCK_MODE 修订

## 12.1 触发条件

建议保留：

```text
10 分钟内 >= 3 个 position_id 完成 TP2
```

账户收益条件改为：

```text
10 分钟内 realized_pnl
/
cycle_start_equity
>= 1%
```

禁止使用账户总权益变化作为唯一判断，因为权益可能受：

- 未实现浮盈；
- 入金；
- 转账；
- 资金费；
- 其他账户变化影响。

## 12.2 统计单位

必须以完整仓位 `position_id` 为单位。

同一仓位分批止盈不能重复计数。

---

# 13. 连亏统计修订

## 13.1 统计方式

连续亏损按：

```text
完整仓位关闭后的净已实现盈亏
```

统计。

净盈亏：

```text
realized_pnl
-
commission
-
funding_fee
```

## 13.2 不重复计算

同一仓位：

- TP1；
- TP2；
- 部分止损；
- 最终平仓；

全部完成后只统计为一笔交易。

## 13.3 优先级

```text
SYSTEM_HALT
> LOSS_COOLDOWN
> GLOBAL_LONG_FREEZE
> PROFIT_LOCK_MODE
> MARKET_STATE_FACTOR
```

当多个机制同时触发时，必须记录主因和次因。

示例：

```json
{
  "allowed": false,
  "primary_reason": "LOSS_COOLDOWN",
  "secondary_reasons": [
    "RISK_OFF",
    "GLOBAL_LONG_FREEZE"
  ]
}
```

---

# 14. 风险系数修订

## 14.1 问题

多个系数连续相乘，可能让仓位小到低于最小下单额。

原公式：

```text
actual_risk
= base_risk
× signal_factor
× mode_factor
× global_direction_factor
× cooldown_factor
```

可能产生过度缩小。

## 14.2 推荐公式

```text
strategy_risk
= base_risk
× signal_factor
× mode_factor

global_cap
= base_risk
× global_direction_factor

actual_risk
= min(strategy_risk, global_cap)
```

利润锁再单独应用：

```text
actual_risk
= actual_risk × profit_lock_factor
```

冷却期间：

```text
actual_risk = 0
```

## 14.3 最小有效风险

```text
actual_risk < minimum_effective_risk
→ 放弃交易
```

禁止为了满足最小下单额而反向放大仓位。

---

# 15. RISK_OFF 下禁止低位追空

## 15.1 状态细分

建议拆分为：

```text
RISK_OFF_BREAKDOWN
RISK_OFF_REBOUND
RISK_OFF_CONTINUATION
```

## 15.2 交易许可

```text
RISK_OFF_BREAKDOWN：
刚破位，不追空，等待反抽

RISK_OFF_REBOUND：
反抽压力位，允许反抽空
short_factor = 0.8

RISK_OFF_CONTINUATION：
形成新平台后再次破位
允许突破空
short_factor = 0.5
```

这样可以避免市场急跌后在最低点继续追空。

---

# 16. 交易类型统一

所有文档和代码必须使用同一枚举。

建议：

```python
from enum import Enum

class TradeType(str, Enum):
    TREND_PULLBACK = "trend_pullback"
    BREAKOUT_RETEST = "breakout_retest"
    MOMENTUM_SCALP = "momentum_scalp"
    RANGE_REVERSAL = "range_reversal"
    FAKE_BREAKOUT_REVERSAL = "fake_breakout_reversal"
    REBOUND_SHORT = "rebound_short"
    CONTINUATION_SHORT = "continuation_short"
```

禁止同时使用：

```text
standard_pullback
pullback_long
回踩标准多
breakout_pullback
```

等不同名称表示同一交易类型。

---

# 17. 全局状态快照

同一轮扫描中的所有币必须使用同一个全局市场快照。

推荐流程：

```python
snapshot = build_global_market_snapshot()
scan_all_symbols(snapshot)
```

快照至少包含：

```text
state_version
generated_at
btc_state
eth_state
market_regime
breadth_raw
breadth_smooth
global_long_freeze
loss_cooldown
profit_lock_mode
long_factor
short_factor
data_valid
```

禁止前半批币种使用旧状态，后半批币种使用新状态。

---

# 18. 状态过期保护

建议：

```text
global_state_age > 60 秒：
禁止新开仓

BTC 最后一根 1m K 延迟 > 90 秒：
禁止新开仓

ETH 最后一根 1m K 延迟 > 90 秒：
降低风险或禁止开仓

市场宽度有效样本率 < 80%：
禁止新开仓
```

数据异常时：

```text
允许管理已有仓位
禁止新开仓
```

---

# 19. 最终执行优先级

建议固定为：

```text
1. 更新 BTC/ETH 1m、5m、15m 数据
2. 校验数据完整性和时间戳
3. 更新 confirmed swing points
4. 更新市场宽度
5. 生成全局状态快照
6. 更新 LOSS_COOLDOWN
7. 更新 GLOBAL_LONG_FREEZE
8. 更新 PROFIT_LOCK_MODE
9. 判断方向许可
10. 扫描单币信号
11. 识别 TradeType
12. 计算策略风险
13. 应用全局风险上限
14. 计算止盈止损
15. 最终执行检查
16. 下单
```

禁止使用上一轮未校验的全局状态直接开仓。

---

# 20. 最终开仓许可返回结构

建议统一返回：

```python
{
    "allowed": False,
    "direction": "long",
    "trade_type": "trend_pullback",
    "risk_factor": 0.0,
    "primary_reason": "GLOBAL_LONG_FREEZE",
    "secondary_reasons": [
        "BTC_LOWER_LOW",
        "BREADTH_BELOW_35"
    ],
    "market_regime": "RISK_OFF",
    "state_version": 1872,
    "generated_at": 1783756800
}
```

这样可以用于：

- 实盘日志；
- 回测；
- 复盘；
- 误判分析；
- 参数优化；
- 统计未开仓原因。

---

# 21. 建议保留的核心强制规则

```text
BTC 从 Higher High / Higher Low
切换为 Lower High / Lower Low 后，
山寨多单必须冻结。
```

```text
EMA 回踩只有在 RISK_ON
或确认型 PULLBACK 中才是买点。
```

```text
DISTRIBUTION 和 RISK_OFF 中，
EMA 回踩可能是诱多位置。
```

```text
批量止盈后不得立即重新满频铺仓。
```

```text
连续亏损必须暂停交易并重新判断市场环境。
```

```text
数据失效、状态过期或市场宽度不足时，
禁止新开仓。
```

---

# 22. 实施优先级

## P0：必须先补

- 修正 DISTRIBUTION 单条件误触发；
- 修正 RISK_OFF 单条件误触发；
- 加入状态最短保持时间；
- 加入状态升级与恢复滞回；
- 明确 ATR 和摆动点定义；
- 明确快速收回条件；
- 全局状态使用统一快照；
- 状态过期禁止开仓。

## P1：随后补充

- ETH 结构权重；
- 市场宽度固定样本池；
- PROFIT_LOCK 使用已实现收益；
- 连亏按完整仓位统计；
- 风险系数避免过度叠乘；
- RISK_OFF 禁止破位后直接追空；
- 统一 TradeType 枚举。

## P2：观察后优化

- DISTRIBUTION 分数阈值；
- 冻结时间分级；
- 宽度阈值；
- ATR 最小百分比；
- 不同市场状态下的方向风险系数。

---

# 23. 最终结论

原全局市场结构保护层的方向正确，但实盘落地前必须补充：

- 状态组合条件；
- 抗抖动机制；
- 数据有效性判断；
- 明确的量化阈值；
- 统一状态快照；
- 风险优先级；
- 低位追空保护。

完成这些修订后，该保护层才能从“策略原则”升级为“可稳定编码和复盘的全局风控模块”。
