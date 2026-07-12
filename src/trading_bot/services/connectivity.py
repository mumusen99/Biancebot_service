"""
代理自动恢复 + 连通性检查 + 订阅管理
=======================================
每次心跳先跑这个，如果 Binance API 连不上则自动：
1. 测试当前代理
2. 拉最新订阅测所有节点（并发）
3. 找到工作节点 → 更新配置 → 重启 xray
4. 重试 API

支持 --update 参数强制刷新订阅并重新测节点。
订阅 URL 可通过环境变量 SUB_URL 覆盖。
"""

import base64, json, time, os, socket, subprocess, sys, threading, shutil
import urllib.request, urllib.error
import hmac, hashlib, urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from trading_bot.core.env_config import get_exchange_config

import requests as rq
from trading_bot.core.settings import API_KEY, API_SECRET, PROXY

# ─── 配置 ────────────────────────────────────────────────
SUB_URL = os.getenv("SUB_URL",
    "https://jmssub.net/members/getsub.php?service=416373&id=c87af1ab-a91c-4746-8c2b-2177a0d1f018")
UUID = os.getenv("PROXY_UUID", "c87af1ab-a91c-4746-8c2b-2177a0d1f018")
XRAY = "/tmp/xray/xray"
CONFIG_FILE = "/tmp/xray/config.json"
WORKING_BACKUP = "/tmp/xray/config.working.json"
SUB_CACHE = "/tmp/xray/sub_cache.json"          # 上次订阅缓存
SUB_CONFIG = "/vol2/@apphome/trim.openclaw/data/workspace/binance-bot/.sub_config.json"  # 持久化订阅配置
TIMEOUT = 6
MAX_WORKERS = 8                                  # 并发测试数

# ─── 数据结构 ────────────────────────────────────────────
@dataclass
class VmessNode:
    """解析后的 VMess 节点"""
    ip: str
    port: int = 11767
    net: str = "tcp"
    aid: int = 0
    security: str = "auto"
    raw: dict = field(default_factory=dict)

    @staticmethod
    def from_vmess_link(b64part: str) -> Optional["VmessNode"]:
        try:
            padding = 4 - len(b64part) % 4
            if padding != 4:
                b64part += "=" * padding
            d = json.loads(base64.b64decode(b64part))
            return VmessNode(
                ip=d.get("add", ""),
                port=int(d.get("port", 11767)),
                net=d.get("net", "tcp"),
                aid=int(d.get("aid", 0)),
                security=d.get("security", "auto"),
                raw=d,
            )
        except Exception:
            return None


# ─── 工具函数 ────────────────────────────────────────────

def log(msg):
    print(f"[proxy] {msg}")


def kill_xray():
    os.system("kill $(ps aux | grep '/tmp/xray/xray' | grep -v grep | awk '{print $2}') 2>/dev/null")
    time.sleep(1)


def start_xray(cfg_path: str):
    kill_xray()
    proc = subprocess.Popen([XRAY, "-c", cfg_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    return proc


def wait_port(host="127.0.0.1", port=10809, timeout=3) -> bool:
    """等待端口可用"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect((host, port))
            s.close()
            return True
        except:
            time.sleep(0.3)
    return False


# ─── 订阅管理 ────────────────────────────────────────────

def save_sub_url(url: str):
    """持久化保存订阅 URL"""
    cfg = {}
    if os.path.exists(SUB_CONFIG):
        with open(SUB_CONFIG) as f:
            cfg = json.load(f)
    cfg["sub_url"] = url
    cfg["updated_at"] = int(time.time())
    os.makedirs(os.path.dirname(SUB_CONFIG), exist_ok=True)
    with open(SUB_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    log(f"订阅 URL 已保存")


def get_saved_sub_url() -> str:
    """读取持久化的订阅 URL"""
    if os.path.exists(SUB_CONFIG):
        with open(SUB_CONFIG) as f:
            cfg = json.load(f)
            return cfg.get("sub_url", SUB_URL)
    return SUB_URL


def update_subscription(new_url: str = None) -> bool:
    """
    更新订阅 URL 并立即拉取、测试。
    用法: proxy_guard.update_subscription("https://new-sub-url")
    或: python proxy_guard.py --update https://new-sub-url
    """
    global SUB_URL
    if new_url:
        SUB_URL = new_url
        save_sub_url(new_url)
    else:
        SUB_URL = get_saved_sub_url()

    log(f"📡 正在拉取订阅: {SUB_URL[:60]}...")
    nodes = fetch_subscription()
    if not nodes:
        log("❌ 订阅拉取失败或格式不支持")
        return False

    log(f"✅ 成功获取 {len(nodes)} 个节点")
    # 缓存到文件
    cache = [{"ip": n.ip, "port": n.port, "net": n.net, "raw": n.raw} for n in nodes]
    os.makedirs("/tmp/xray", exist_ok=True)
    with open(SUB_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

    # 并发测试所有节点
    working = test_nodes_concurrent(nodes)
    if working:
        log(f"🎉 找到工作节点: {working.ip}:{working.port} ({working.net})")
        save_working_config(working.ip, working.port, working.net)
        start_xray(CONFIG_FILE)
        if wait_port("127.0.0.1", 10809, timeout=5):
            if test_binance(use_proxy=True):
                log("✅ 代理恢复成功！")
                return True
        log("⚠️ 节点测试通过但 API 仍不可用，可能被临时封禁")
        return False
    else:
        log("❌ 所有节点均不通 Binance")
        return False


def fetch_subscription() -> list[VmessNode]:
    """拉订阅获取节点列表"""
    try:
        req = urllib.request.Request(SUB_URL, headers={"User-Agent": "curl/8.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode().strip()
        decoded = base64.b64decode(raw).decode().strip()
        nodes = []
        for line in decoded.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith(("vmess://", "Vmess://")):
                b64 = line.split("//", 1)[1]
                node = VmessNode.from_vmess_link(b64)
                if node and node.ip:
                    nodes.append(node)
            # 支持更多协议格式可扩展
            elif line.startswith("ss://"):
                # Shadowsocks - 暂不实现
                pass
            elif line.startswith("trojan://"):
                # Trojan - 暂不实现
                pass
        return nodes
    except Exception as e:
        log(f"订阅拉取失败: {e}")
        # 尝试从缓存读取
        if os.path.exists(SUB_CACHE):
            log("📂 从缓存读取上次订阅...")
            try:
                with open(SUB_CACHE) as f:
                    cached = json.load(f)
                return [VmessNode(ip=c["ip"], port=c["port"], net=c.get("net", "tcp"), raw=c.get("raw", {}))
                        for c in cached]
            except:
                pass
        return []


# ─── 节点测试 ────────────────────────────────────────────

_TEST_PORT_COUNTER = 0
_TEST_PORT_LOCK = threading.Lock()


def _next_test_port() -> int:
    """分配唯一测试端口，避免并发冲突"""
    global _TEST_PORT_COUNTER
    with _TEST_PORT_LOCK:
        _TEST_PORT_COUNTER += 1
        return 10900 + _TEST_PORT_COUNTER  # 10901, 10902, ...


def build_vmess_config(ip: str, port: int, net: str, test_port: int = 0) -> dict:
    """生成 xray VMess 配置
    test_port: 测试时用独立端口，避免多线程冲突
    """
    http_port = test_port if test_port else 10809
    socks_port = test_port - 1 if test_port > 0 else 10808
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"port": socks_port, "listen": "127.0.0.1", "protocol": "socks",
             "settings": {"auth": "noauth", "udp": False}},
            {"port": http_port, "listen": "127.0.0.1", "protocol": "http", "settings": {}},
        ],
        "outbounds": [
            {"tag": "proxy", "protocol": "vmess",
             "settings": {"vnext": [{"address": ip, "port": port,
                                     "users": [{"id": UUID, "alterId": 0, "security": "auto"}]}]},
             "streamSettings": {"network": net}},
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "outboundTag": "direct", "domain": ["geosite:cn"]},
                {"type": "field", "outboundTag": "direct", "ip": ["geoip:cn", "geoip:private"]},
                {"type": "field", "inboundTag": ["socks-in", "http-in"], "outboundTag": "proxy"},
            ],
        },
    }


def test_single_node(node: VmessNode, test_signed: bool = True) -> bool:
    """测试单个节点是否通 Binance（并发安全：使用独立端口）"""
    import requests as rq

    test_port = _next_test_port()
    cfg = build_vmess_config(node.ip, node.port, node.net, test_port=test_port)
    cfg_file = f"/tmp/xray/test_{node.ip}.json"
    with open(cfg_file, "w") as f:
        json.dump(cfg, f)

    # 只杀自己的 xray 进程（不全局 kill）
    subprocess.run(["pkill", "-f", f"xray.*test_{node.ip}"], capture_output=True)
    time.sleep(0.3)
    proc = subprocess.Popen([XRAY, "-c", cfg_file],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not wait_port("127.0.0.1", test_port, timeout=6):
        proc.kill()
        return False

    proxies = {"http": f"http://127.0.0.1:{test_port}", "https": f"http://127.0.0.1:{test_port}"}
    success = True

    # 测试1: ping (无签名)
    try:
        _fapi = get_exchange_config().fapi_v1_base
        r = rq.get(f"{_fapi}/ping", proxies=proxies, timeout=TIMEOUT)
        if r.status_code != 200:
            success = False
    except:
        success = False

    # 测试2: klines (无签名)
    if success:
        try:
            r = rq.get(f"{_fapi}/klines?symbol=BTCUSDT&interval=1h&limit=2",
                       proxies=proxies, timeout=TIMEOUT)
            if r.status_code != 200:
                success = False
        except:
            success = False

    # 测试3: 签名 API (openOrders)
    if success and test_signed:
        try:
            p = {"symbol": "BTCUSDT", "timestamp": int(time.time() * 1000), "recvWindow": 10000}
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = rq.get(f"{_fapi}/openOrders?{q}&signature={sig}",
                        headers={"X-MBX-APIKEY": API_KEY}, proxies=proxies, timeout=TIMEOUT)
            if r.status_code != 200:
                success = False
        except:
            success = False

    subprocess.run(["pkill", "-f", f"xray.*test_{node.ip}"], capture_output=True)
    try:
        os.remove(cfg_file)
    except:
        pass
    return success


def test_nodes_concurrent(nodes: list[VmessNode], max_workers: int = MAX_WORKERS) -> Optional[VmessNode]:
    """并发测试所有节点，返回第一个可用的"""
    if not nodes:
        return None

    log(f"并发测试 {len(nodes)} 个节点 (max {max_workers} 线程)...")
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(test_single_node, n): n for n in nodes}
        for i, fut in enumerate(as_completed(fut_map), 1):
            node = fut_map[fut]
            ip = node.ip
            try:
                ok = fut.result()
            except:
                ok = False
            results[ip] = ok
            status = "✅" if ok else "❌"
            print(f"  [{i}/{len(nodes)}] {ip}:{node.port} {status}")
            if ok:
                # 找到可用节点，取消剩余任务
                for f in fut_map:
                    if not f.done():
                        f.cancel()
                return node

    return None


# ─── 连通性检查 ──────────────────────────────────────────

def test_binance(use_proxy=True) -> bool:
    """快速测试 Binance API 是否可用（只 ping）"""
    import requests as rq
    from trading_bot.core.env_config import get_exchange_config
    _fapi = get_exchange_config().fapi_v1_base
    proxies = {"http": "http://127.0.0.1:10809", "https": "http://127.0.0.1:10809"} if use_proxy else {}
    try:
        r = rq.get(f"{_fapi}/ping", proxies=proxies, timeout=TIMEOUT)
        if r.status_code != 200:
            return False
        # 再轻测一个签名 API
        p = {"symbol": "BTCUSDT", "timestamp": int(time.time() * 1000), "recvWindow": 10000}
        q = urllib.parse.urlencode(sorted(p.items()))
        sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        r2 = rq.get(f"{_fapi}/openOrders?{q}&signature={sig}",
                     headers={"X-MBX-APIKEY": API_KEY}, proxies=proxies, timeout=TIMEOUT)
        return r2.status_code == 200
    except:
        return False


def save_working_config(ip, port=11767, net="tcp"):
    """保存工作配置到主配置 + 备份（用标准端口 10809）"""
    cfg = build_vmess_config(ip, port, net, test_port=0)
    os.makedirs("/tmp/xray", exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)
    with open(WORKING_BACKUP, "w") as f:
        json.dump(cfg, f)
    log(f"配置已保存: {ip}:{port} ({net})")


# ─── 主入口 ──────────────────────────────────────────────

def ensure_xray_alive() -> bool:
    """确保xray进程运行并且端口正常监听，否则重启"""
    # 检查端口
    if wait_port("127.0.0.1", 10809, timeout=1):
        return True
    
    # 检查配置文件
    cfg_path = CONFIG_FILE
    if not os.path.exists(cfg_path):
        # 试备份
        if os.path.exists(WORKING_BACKUP):
            cfg_path = WORKING_BACKUP
        else:
            log("❌ 无可用配置文件")
            return False
    
    # 尝试重启
    log("🔄 重启 xray...")
    start_xray(cfg_path)
    if wait_port("127.0.0.1", 10809, timeout=5):
        log("✅ xray 重启成功")
        return True
    
    log("❌ xray 重启失败")
    return False


def auto_recover() -> bool:
    """
    全自动代理恢复：
    1. 确保 xray 进程活着
    2. 测当前节点
    3. 不行就遍历缓存节点
    4. 再不行就拉新订阅换节点
    返回 True = 恢复成功
    """
    # 第0步：确保xray在跑
    ensure_xray_alive()
    
    # 第1步：测当前代理
    if test_binance(use_proxy=True):
        return True
    
    # 第2步：遍历缓存节点
    log("⚠️ 当前节点异常，检查缓存节点...")
    current_ip = None
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
            for out in cfg.get("outbounds", []):
                for v in out.get("settings", {}).get("vnext", []):
                    current_ip = v.get("address", "")
    except:
        pass
    
    if os.path.exists(SUB_CACHE):
        try:
            with open(SUB_CACHE) as f:
                cached = json.load(f)
            for c in cached:
                ip = c.get("ip", "")
                port = c.get("port", 11767)
                net = c.get("net", "tcp")
                if ip == current_ip:
                    continue
                log(f"  🔄 切换节点: {ip}:{port}")
                save_working_config(ip, port, net)
                start_xray(CONFIG_FILE)
                if wait_port("127.0.0.1", 10809, timeout=3):
                    if test_binance(use_proxy=True):
                        log(f"✅ 缓存节点切换成功: {ip}:{port}")
                        return True
        except Exception as e:
            log(f"  遍历缓存异常: {e}")
    
    # 第3步：拉新订阅并测试
    log("📡 缓存无效，拉取新订阅...")
    return update_subscription()


def ensure_connection() -> bool:
    """
    主入口：确保 Binance API 可达。
    优先直连（美国/国际服务器），不可用时再走代理恢复。
    返回 True = 可用, False = 无法恢复
    """
    global SUB_URL
    SUB_URL = get_saved_sub_url()

    log("检查 Binance API 连通性...")

    # 先试直连（美国服务器不需要代理）
    if test_binance(use_proxy=False):
        log("✅ 直连正常")
        return True

    # 再试代理
    if test_binance(use_proxy=True):
        log("✅ 当前代理正常")
        return True

    return auto_recover()


def show_status():
    """显示当前代理状态"""
    import requests as rq
    print("=" * 50)
    print("📊 代理状态报告")
    print("=" * 50)

    r = subprocess.run(["pgrep", "-a", "xray"], capture_output=True, text=True, timeout=3)
    print(f"xray 进程: {'✅ 运行中' if r.stdout else '❌ 未运行'}")
    for line in r.stdout.strip().split("\n"):
        if line:
            print(f"   {line}")

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        for ob in cfg.get("outbounds", []):
            if ob["tag"] == "proxy":
                v = ob["settings"]["vnext"][0]
                print(f"当前节点: {v['address']}:{v['port']}")
                print(f"网络协议: {ob['streamSettings']['network']}")

    try:
        r = rq.get("http://httpbin.org/ip", proxies={"http": f"http://127.0.0.1:10809"},
                   timeout=5)
        print(f"出口 IP: {r.json().get('origin', '未知')}")
    except:
        print("出口 IP: ❌ 无法获取")

    ok = test_binance(use_proxy=True)
    print(f"Binance API: {'✅ 正常' if ok else '❌ 异常'}")

    url = get_saved_sub_url()
    print(f"订阅 URL: {url[:60]}...")
    if os.path.exists(SUB_CACHE):
        with open(SUB_CACHE) as f:
            cached = json.load(f)
        print(f"缓存节点数: {len(cached)}")
    print("=" * 50)


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--status" in args or "-s" in args:
        show_status()
        sys.exit(0)

    if "--update" in args:
        idx = args.index("--update")
        new_url = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("--") else None
        ok = update_subscription(new_url)
        print(f"\n订阅更新: {'✅ 成功' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)

    ok = ensure_connection()
    print(f"\n连通性: {'✅ 正常' if ok else '❌ 异常'}")
    sys.exit(0 if ok else 1)

