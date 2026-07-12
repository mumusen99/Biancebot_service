#!/usr/bin/env python3
"""实时持仓监控"""
import os, sys, json, time, requests, hmac, hashlib

ENV = '/etc/trading-bot/live.env'
BOLD = '\033[1m'
CLR = '\033[0m'
GRN = '\033[32m'
RED = '\033[31m'
CYN = '\033[36m'
YEL = '\033[93m'
WARN = '\033[91m'

def load_env():
    for line in open(ENV):
        if '=' in line.strip() and not line.startswith('#'):
            k,v = line.strip().split('=',1)
            os.environ[k] = v.strip()

def signed_get(path):
    key = os.environ['BINANCE_API_KEY']
    sec = os.environ['BINANCE_API_SECRET']
    t = int(time.time()*1000)
    q = 'timestamp=' + str(t)
    s = hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
    sep = '&' if '?' in path else '?'
    url = 'https://fapi.binance.com' + path + sep + q + '&signature=' + s
    return requests.get(url, headers={'X-MBX-APIKEY':key}, timeout=5).json()

def c(pct):
    if pct > 1: return GRN
    if pct > 0: return YEL
    if pct < -1: return RED
    if pct < 0: return WARN
    return ''

def main():
    load_env()
    last = 0
    print(chr(27) + '[2J' + chr(27) + '[H', end='')
    
    while True:
        now = time.time()
        if now - last < 0.5:
            time.sleep(0.05)
            continue
        last = now
        
        try:
            poses = signed_get('/fapi/v2/positionRisk')
            bal = signed_get('/fapi/v2/account')
            total = float(bal.get('totalWalletBalance', 0))
        except:
            time.sleep(1)
            continue
        
        t = time.strftime('%H:%M:%S')
        lines = []
        lines.append(CYN + '╔══════════════════════════════════════════════════╗' + CLR)
        lines.append(CYN + '║ 实时持仓  %s  余额:%.1fU 杠杆:5x             ║' % (t, total) + CLR)
        lines.append(CYN + '╠══════════════════════════════════════════════════╣' + CLR)
        
        active = False
        for p in poses:
            amt = abs(float(p['positionAmt']))
            if amt <= 0: continue
            active = True
            entry = float(p['entryPrice'])
            mark = float(p['markPrice'])
            side = p.get('positionSide', 'LONG')
            pnl = float(p['unRealizedProfit'])
            margin = amt * entry / 5
            pnl_pct = pnl / margin * 100 if margin > 0 else 0
            
            if side == 'LONG':
                sl = round(entry * 0.995, 5)
                R = entry - sl
                tp1 = round(entry + 1.0 * R, 5)
                tp2 = round(entry + 1.5 * R, 5)
                tp3 = round(entry + 2.5 * R, 5)
                sl_hit = mark <= sl
                tp_hit = mark >= tp1
            else:
                sl = round(entry * 1.005, 5)
                R = sl - entry
                tp1 = round(entry - 1.0 * R, 5)
                tp2 = round(entry - 1.5 * R, 5)
                tp3 = round(entry - 2.5 * R, 5)
                sl_hit = mark >= sl
                tp_hit = mark <= tp1
            
            color = c(pnl_pct)
            icon = RED + 'SL!' + CLR if sl_hit else (GRN + 'TP!' + CLR if tp_hit else '   ')
            
            sym = p['symbol']
            lines.append(' %s %s%s%s %5s @%s mark=%s %sPnL%+.2fU(%+.1f%%)%s' % (
                icon, BOLD, sym.ljust(12), CLR, side,
                '%8.4f' % entry, '%8.4f' % mark,
                color, pnl, pnl_pct, CLR
            ))
            lines.append('     ├ SL:%s  TP1:%s  TP2:%s  TP3:%s  qty=%d  保证金%.1fU' % (
                '%.5f' % sl, '%.5f' % tp1, '%.5f' % tp2, '%.5f' % tp3,
                int(amt), margin
            ))
        
        if not active:
            lines.append('  暂无持仓')
        
        lines.append(CYN + '╚══════════════════════════════════════════════════╝' + CLR)
        lines.append('Ctrl+C退出')
        
        print(chr(27) + '[H', end='')
        print('\n'.join(lines))

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n退出')

