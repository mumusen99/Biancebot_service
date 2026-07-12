"""
Binance 合约自动化交易 - 配置文件
=================================
环境配置统一由 core.env_config 管理。
本文件只保留策略参数、交易标的、状态路径等业务配置。
"""
import os
from pathlib import Path
from trading_bot.core.env_config import get_exchange_config, is_testnet, is_live

# ─── 环境配置（统一来源）───────────────────────────────
try:
    _exchange = get_exchange_config()
    API_KEY = _exchange.api_key
    API_SECRET = _exchange.api_secret
    PROXY = _exchange.proxy
    IS_TESTNET = is_testnet()
    FAPI_BASE = _exchange.fapi_v1_base
    WSS_URL = _exchange.ws_base_url
except EnvironmentError:
    # 非生产环境（测试/开发）：提供安全默认值
    API_KEY = os.getenv("BINANCE_API_KEY", "")
    API_SECRET = os.getenv("BINANCE_API_SECRET", "")
    PROXY = os.getenv("BINANCE_PROXY", "")
    IS_TESTNET = True  # 默认测试网安全模式
    FAPI_BASE = "https://testnet.binancefuture.com/fapi/v1"
    WSS_URL = "wss://stream.binancefuture.com/ws"

# ─── 自动从 .env 加载 (如果环境变量未设置) ──────────────
PROJECT_ROOT = Path(__file__).resolve().parents[3]
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
            if _k and not os.environ.get(_k):
                os.environ[_k] = _v

# ─── 交易标的 (可追加/删减) ───────────────────────────────
SYMBOLS = [
    "BTCUSDT",   # 比特币
    "ETHUSDT",   # 以太坊
    "BNBUSDT",   # 币安币
    "SOLUSDT",   # Solana
    "XRPUSDT",   # Ripple
    "DOGEUSDT",  # 狗狗币
    "ADAUSDT",   # Cardano
    "AVAXUSDT",  # Avalanche
    "LINKUSDT",  # Chainlink
    "DOTUSDT",   # Polkadot
    "MATICUSDT", # Polygon
    "NEARUSDT",  # Near Protocol
    "APTUSDT",   # Aptos
    "ARBUSDT",   # Arbitrum
    "OPUSDT",    # Optimism
    "PEPEUSDT",  # Pepe
]

# ─── 默认杠杆 & 保证金模式 ───────────────────────────────
DEFAULT_LEVERAGE = 3          # 杠杆倍数
ISOLATED_MARGIN = True        # True = 逐仓

# ─── 风险控制 ───────────────────────────────────────────
MAX_POSITION_USDT = 20        # AI 单笔上限
MAX_TOTAL_POSITION_USDT = 80  # 总仓位上限
INITIAL_BUDGET = 80.0         # 初始预算
STOP_LOSS_PERCENT = 10.5      # 止损百分比 (保证金%)
TAKE_PROFIT_PERCENT = 40.0    # 止盈百分比 (保证金%)
TRAILING_STOP_ACTIVATE = 15.0
TRAILING_STOP_DISTANCE = 5.0

# ─── 策略止盈止损参数 ────────────────────────────────────
STRATEGY_PROFILES = {
    'band': {
        'label': '波段', 'leverage': 3,
        'sl_margin_pct': 10.5, 'tp_margin_pct': 25.0,
        'sl_price_pct': 10.5 / 3, 'tp_price_pct': 25.0 / 3,
        'trailing_activate_pct': 3.0,
        'trailing_target_pct': 6.0,
        'trailing_profit_pct': 12.0,
        'max_age_hours': 72,
    },
    'scalp': {
        'label': '超短线', 'leverage': 3,
        'sl_margin_pct': 5.0, 'tp_margin_pct': 10.0,
        'sl_price_pct': 5.0 / 3, 'tp_price_pct': 10.0 / 3,
        'trailing_activate_pct': 3.0,
        'trailing_target_pct': 5.0,
        'trailing_profit_pct': 8.0,
        'max_age_hours': 48,
    },
    'scan': {
        'label': '扫盘', 'leverage': 5,
        'sl_margin_pct': 5.0, 'tp_margin_pct': 10.0,
        'sl_price_pct': 5.0 / 5, 'tp_price_pct': 10.0 / 5,
        'trailing_activate_pct': 2.0,
        'trailing_target_pct': 4.0,
        'trailing_profit_pct': 6.0,
        'max_age_hours': 36,
    },
    'unknown': {
        'label': '未知', 'leverage': 3,
        'sl_margin_pct': 10.0, 'tp_margin_pct': 20.0,
        'sl_price_pct': 10.0 / 3, 'tp_price_pct': 20.0 / 3,
        'trailing_activate_pct': 5.0,
        'trailing_target_pct': 8.0,
        'trailing_profit_pct': 15.0,
        'max_age_hours': 48,
    },
}

# ─── K线周期 ────────────────────────────────────────────
TIMEFRAMES = ["1h", "4h", "1d"]

# ─── 策略参数 ──────────────────────────────────────────
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BOLL_PERIOD = 20
BOLL_STD = 2

# ─── 数据/状态文件 ──────────────────────────────────────
STATE_DIR = Path(os.getenv("TRADING_STATE_DIR", PROJECT_ROOT / "state"))
LOG_DIR = Path(os.getenv("TRADING_LOG_DIR", PROJECT_ROOT / "logs"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"
ANALYSIS_FILE = STATE_DIR / "analysis.json"
LOG_FILE = LOG_DIR / "trades.log"
BOT_STATE_FILE = STATE_DIR / "bot_state.json"
