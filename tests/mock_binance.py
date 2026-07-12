#!/usr/bin/env python3
"""币安合约REST模拟器"""
import json, os, random, sys, threading, time
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BALANCE = 250.0
POSITIONS = {}
ORDERS = {}
ALGO_ORDERS = {}
ORDER_ID = 1000000
ALGO_ID = 2000000

COINS = {
    'BTCUSDT':64300,'ETHUSDT':3450,'SOLUSDT':145,'ADAUSDT':0.17,
    'ONDOUSDT':0.334,'THETAUSDT':0.155,'TIAUSDT':0.414,
    'SOXLUSDT':198.0,'WLDUSDT':0.409,'ARBUSDT':0.098,
    'LDOUSDT':0.318,'PYTHUSDT':0.049,'KAITOUSDT':0.670,'ZECUSDT':512.0,
    'LITUSDT':2.65,'ENAUSDT':0.083,'FARTCOINUSDT':0.152,
    'VIRTUALUSDT':0.603,'REUSDT':0.585,'GRASSUSDT':0.404,
    'SNDKUSDT':1958.0,'OUSDT':0.563,'DEXEUSDT':38.3,'IOTAUSDT':0.1,
    'UNIUSDT':3.73,'ANKRUSDT':0.0038,'ARXUSDT':0.186,
    'SYRUPUSDT':0.196,'MOODENGUSDT':0.05,'EPICUSDT':0.02,
}

def _next_id():
    global ORDER_ID; ORDER_ID += 1; return ORDER_ID

def _next_algo_id():
    global ALGO_ID; ALGO_ID += 1; return ALGO_ID

def _tick(sym):
    return COINS.get(sym, 1.0) * (1 + random.uniform(-0.002, 0.002))

class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _ok(self, data):
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _err(self, code, msg):
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'code':code,'msg':msg}).encode())

    def _params(self):
        p = urlparse(self.path)
        return p.path, parse_qs(p.query)

    def do_GET(self):
        path, qs = self._params()

        if path == '/fapi/v2/account':
            pl = [{'symbol':sym,'positionAmt':str(p['positionAmt']),
                   'entryPrice':str(p['entryPrice']),'markPrice':str(_tick(sym)),
                   'unRealizedProfit':'0','leverage':'5',
                   'positionSide':p.get('side','LONG')} for sym,p in POSITIONS.items()]
            return self._ok({'totalWalletBalance':str(BALANCE),
                'availableBalance':str(BALANCE-10),'totalUnrealizedProfit':'0',
                'totalMarginBalance':str(BALANCE),'positions':pl})

        if '/positionRisk' in path:
            sym = qs.get('symbol',[None])[0]
            r = [{'symbol':s,'positionAmt':str(p['positionAmt']),
                  'entryPrice':str(p['entryPrice']),'markPrice':str(_tick(s)),
                  'unRealizedProfit':str(round((_tick(s)-p['entryPrice'])*p['positionAmt'],4)),
                  'leverage':'5','positionSide':p.get('side','LONG')}
                 for s,p in POSITIONS.items() if not sym or s==sym]
            return self._ok(r)

        if '/ticker/24hr' in path:
            return self._ok([{'symbol':s,'lastPrice':str(_tick(s)),
                'priceChangePercent':str(random.uniform(-5,5)),
                'quoteVolume':str(random.randint(100000,50000000)),
                'highPrice':str(_tick(s)*1.01),'lowPrice':str(_tick(s)*0.99)}
                for s in COINS])

        if '/klines' in path:
            sym = qs.get('symbol',['BTCUSDT'])[0]
            limit = int(qs.get('limit',[60])[0])
            step = 300000 if '5m' in self.path else 60000
            now = int(time.time()*1000)
            k = []
            for i in range(limit):
                t = now-(limit-i)*step
                o = _tick(sym); h = o*1.003; l = o*0.997; c = (h+l)/2
                k.append([t,str(o),str(h),str(l),str(c),'1000',
                          t+step-1,'10000','500','500','5000','0'])
            return self._ok(k)

        if '/openOrders' in path:
            return self._ok([o for o in ORDERS.values() if o['status']=='NEW'])

        if '/allAlgoOrders' in path:
            return self._ok(list(ALGO_ORDERS.values()))

        if '/openAlgoOrders' in path:
            return self._ok([a for a in ALGO_ORDERS.values()
                            if a.get('algoStatus')=='NEW'])

        return self._err(404,'Unknown GET '+path)

    def do_POST(self):
        path, qs = self._params()

        if '/order' in path and 'algo' not in path:
            sym = qs.get('symbol',['BTCUSDT'])[0]
            side = qs.get('side',['BUY'])[0]
            qty = float(qs.get('quantity',[1])[0])
            price = _tick(sym)

            if sym in POSITIONS:
                pos = POSITIONS[sym]
                ps = pos.get('side','LONG')
                if (ps=='LONG' and side=='SELL') or (ps=='SHORT' and side=='BUY'):
                    nq = pos['positionAmt']-qty
                    if nq<=0: del POSITIONS[sym]
                    else: pos['positionAmt']=nq
            else:
                POSITIONS[sym]={'entryPrice':price,'positionAmt':qty,
                                'side':'LONG' if side=='BUY' else 'SHORT'}

            oid = _next_id()
            ORDERS[oid]={'orderId':oid,'symbol':sym,'side':side,
                         'type':qs.get('type',['MARKET'])[0],
                         'origQty':str(qty),'price':str(price),
                         'status':'FILLED','avgPrice':str(price)}
            return self._ok(ORDERS[oid])

        if '/algoOrder' in path:
            sym = qs.get('symbol',['BTCUSDT'])[0]
            aid = _next_algo_id()
            ALGO_ORDERS[aid]={'algoId':aid,'symbol':sym,'algoStatus':'NEW',
                'orderType':qs.get('type',['STOP_MARKET'])[0],
                'triggerPrice':qs.get('triggerprice',
                    [qs.get('stopPrice',['0'])[0]])[0]}
            return self._ok({'algoId':aid,'algoStatus':'NEW'})

        return self._err(404,'Unknown POST '+path)

    def do_DELETE(self):
        path = self.path
        for part in path.strip('/').split('/'):
            if part.isdigit():
                oid = int(part)
                ALGO_ORDERS.pop(oid,None)
                if oid in ORDERS: ORDERS[oid]['status']='CANCELED'
                return self._ok({'status':'CANCELED'})
        return self._err(404,'Unknown DELETE')

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = HTTPServer(('0.0.0.0', port), MockHandler)
    print(f'Mock Binance Futures REST API on port {port}')
    server.serve_forever()

if __name__ == '__main__':
    main()
