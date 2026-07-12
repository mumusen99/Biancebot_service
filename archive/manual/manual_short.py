"""手动开 XAUUSDT 空单 - 使用现有 trader 类 + 重试"""
import sys, time
sys.path.insert(0, ".")
from trader import Trader, _get_price, _api
from config import MAX_POSITION_USDT
from notifications import push as add_notification

# 1. 获取价格（重试）
print("📊 获取 XAUUSDT 价格...")
price = None
for i in range(8):
    try:
        price = _get_price("XAUUSDT")
        print(f"✅ 价格: {price:.2f}")
        break
    except Exception as e:
        print(f"⏳ 第{i+1}次: {type(e).__name__}")
        time.sleep(5)
if not price:
    print("❌ 无法获取价格")
    sys.exit(1)

trader = Trader()
usdt_amount = min(10.0, MAX_POSITION_USDT / 2)

# 2. 设置杠杆
for i in range(3):
    try:
        trader.set_leverage("XAUUSDT")
        break
    except:
        time.sleep(3)

# 3. 设置保证金（忽略已设错误）
for i in range(3):
    try:
        trader.set_margin_mode("XAUUSDT")
        break
    except:
        time.sleep(3)

# 4. 计算数量
amount_str = trader.calculate_position_size("XAUUSDT", price, usdt_amount)
amount_f = float(amount_str)
side_map = {"SELL": "SHORT"}
print(f"📤 开空: XAUUSDT SELL {amount_str}张 @ {price:.2f} (价值{amount_f*price:.2f}U)")

# 5. 下市价空单（重试）
order = None
for i in range(5):
    try:
        order = _api("POST", "order", {
            "symbol": "XAUUSDT",
            "side": "SELL",
            "type": "MARKET",
            "quantity": amount_str,
            "positionSide": "SHORT",
        })
        print(f"✅ 空单成交: {order.get('orderId')}")
        break
    except Exception as e:
        print(f"⏳ 第{i+1}次下单失败: {type(e).__name__}, 等5秒...")
        time.sleep(5)

if not order:
    print("❌ 下单失败")
    sys.exit(1)

# 6. 挂止盈止损
entry = float(order.get("avgPrice", price))
filled = float(order.get("executedQty", amount_f))
print(f"✅ 成交价: {entry:.2f}  数量: {filled}")

trader._set_stop_loss_take_profit("XAUUSDT", "SHORT", entry, filled)

print(f"""
🎯 XAUUSDT 空单已开!
   入场: {entry:.2f}
   数量: {filled}
   价值: {filled*entry:.2f}U
   理由: KOL🐻 + 用户指令
""")

add_notification(
    f"📉 手动开空: XAUUSDT SHORT\n"
    f"入场: {entry:.2f}\n"
    f"数量: {filled}\n"
    f"价值: {filled*entry:.2f}U\n"
    f"理由: KOL🐻 + 用户指令",
    "open"
)
