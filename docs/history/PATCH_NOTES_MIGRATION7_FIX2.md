# Migration 7 Fix 2

- LIMIT 入场改为成交后保护：未成交时不预挂全额 SL/TP。
- 检测部分/全部成交后先取消剩余入场单，再读取真实 entryPrice/positionAmt。
- 市价入场使用交易所真实持仓均价和数量建立保护。
- 保护失败时执行平仓并轮询 positionRisk 确认；无法确认则保存 UNPROTECTED。
- update_sltp 删除 allAlgoOrders，改为新单确认后按 algoId 精确删除旧单。
- 市场宽度有效样本不足 40 或请求失败时返回无效哨兵并进入 DATA_INVALID。
