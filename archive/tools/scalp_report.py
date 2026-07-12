"""
超短线复盘报告生成器 (urllib3 patch)
每30分钟输出持仓+交易+状态到微信
"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
os.chdir(str(BASE))

# ─── 读取持仓 ──────────────────────────────────────

def get_positions():
    """从交易所获取当前持仓"""
    from config import API_KEY, API_SECRET, PROXY
    import urllib3, hmac, hashlib, urllib.parse
    urllib3.disable_warnings()
    ts = int(time.time() * 1000)
    params = {'timestamp': str(ts), 'recvWindow': '10000'}
    q = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    http = urllib3.ProxyManager(PROXY)
    r = http.request('GET',
        f'https://fapi.binance.com/fapi/v2/positionRisk?{q}&signature={sig}',
        headers={'X-MBX-APIKEY': API_KEY}, timeout=10.0)
    raw = r.json()
    positions = []
    for p in raw:
        amt = float(p['positionAmt'])
        if abs(amt) < 0.001: continue
        entry = float(p['entryPrice'])
        mark = float(p['markPrice'])
        upnl = float(p['unRealizedProfit'])
        lev = int(p.get('leverage', 5))
        notional = entry * abs(amt) / lev
        pnl_pct = round(upnl / notional * 100, 1) if notional > 0 else 0
        positions.append({
            "sym": p['symbol'], "side": p['positionSide'],
            "size": abs(amt), "entry": entry, "mark": mark,
            "pnl": upnl, "pnl_pct": pnl_pct, "leverage": lev,
        })
    return positions

def get_wallet():
    """获取账户余额"""
    from config import API_KEY, API_SECRET, PROXY
    import urllib3, hmac, hashlib, urllib.parse
    urllib3.disable_warnings()
    ts = int(time.time() * 1000)
    params = {'timestamp': str(ts), 'recvWindow': '10000'}
    q = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    http = urllib3.ProxyManager(PROXY)
    r = http.request('GET',
        f'https://fapi.binance.com/fapi/v2/account?{q}&signature={sig}',
        headers={'X-MBX-APIKEY': API_KEY}, timeout=10.0)
    acct = r.json()
    for a in acct.get('assets', []):
        if a['asset'] == 'USDT':
            return float(a.get('walletBalance', 0))
    return 0

def get_recent_trades(log_file: str, since: float):
    """从日志获取最近的交易"""
    recent = []
    try:
        with open(log_file) as f:
            # Read last 2000 lines
            lines = f.readlines()[-2000:]
        for line in lines:
            if "✅ 平仓" in line and "v4" in line:
                ts_str = line[:23]
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").timestamp()
                if ts > since:
                    recent.append(line.strip())
    except Exception:
        pass
    return recent[-10:]  # Last 10

def get_last_report_time():
    try:
        with open(BASE / ".last_scalp_report") as f:
            return float(f.read().strip())
    except:
        return time.time() - 3600

def save_report_time():
    with open(BASE / ".last_scalp_report", "w") as f:
        f.write(str(time.time()))

# ─── 生成报告 ──────────────────────────────────────

def generate_report():
    now = datetime.now().strftime("%H:%M")
    
    # 系统状态
    ps_result = os.popen("ps aux | grep rt_scalper.py | grep -v grep").read()
    running = "✅" if "rt_scalper.py" in ps_result else "❌"
    
    # 持仓
    positions = get_positions()
    wallet = get_wallet()
    
    # 最近交易
    since = get_last_report_time()
    recent_trades = get_recent_trades(BASE / "rt_scalper.log", since)
    save_report_time()
    
    # 钱包/总PnL跟踪
    try:
        with open(BASE / ".scalp_pnl_start") as f:
            start_wallet = float(f.read().strip())
    except:
        start_wallet = wallet
        with open(BASE / ".scalp_pnl_start", "w") as f:
            f.write(str(wallet))
    
    day_pnl = wallet - start_wallet
    
    # 观测区
    obs_count = "?"
    try:
        import subprocess
        r = subprocess.run(['grep', '观测:', 'rt_scalper.log'], capture_output=True, text=True)
        lines = [l for l in r.stdout.split('\n') if 'v4' in l]
        if lines:
            obs = lines[-1].split('观测:')[-1].split()[0]
            obs_count = obs
    except:
        pass
    
    # 报告
    report = []
    report.append(f"📊 超短线复盘 ({now})")
    report.append(f"{'='*30}")
    report.append(f"系统: {running}  观测区: {obs_count}")
    report.append(f"余额: {wallet:.2f}U | 日内: {day_pnl:+.2f}U")
    report.append("")
    
    if positions:
        total_upnl = sum(p['pnl'] for p in positions)
        report.append(f"持仓 {len(positions)}个 (未实现: {total_upnl:+.2f}U):")
        for p in positions:
            icon = "🟢" if p['pnl'] > 0 else "🔴"
            report.append(f"  {icon} {p['sym']:12s} {p['side']:5s} {p['size']:.2f}张  PnL={p['pnl']:+.2f}U({p['pnl_pct']:+.1f}%)")
        report.append("")
    else:
        report.append("  📭 无持仓")
        report.append("")
    
    if recent_trades:
        report.append(f"最近平仓 ({len(recent_trades)}笔):")
        for t in recent_trades[-5:]:
            # Format: ✅ 平仓 SYMBOL 原因 PnL=...
            parts = t.split()
            if len(parts) >= 6:
                sym = parts[3] if len(parts) > 3 else "?"
                pnl_part = parts[-1] if len(parts) > 0 else ""
                reason = " ".join(parts[4:-1]) if len(parts) > 5 else ""
                icon = "🟢" if "+" in pnl_part else "🔴"
                report.append(f"  {icon} {sym} {reason} {pnl_part}")
        report.append("")
    
    report.append(f"📈 自统计以来: {wallet-start_wallet:+.2f}U")
    
    return "\n".join(report)


if __name__ == "__main__":
    print(generate_report())
