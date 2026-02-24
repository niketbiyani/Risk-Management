"""
Configuration loader for the Trade Management Platform.
Reads from .env file and provides typed access to all settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Dhan API
    DHAN_CLIENT_ID: str = os.getenv("DHAN_CLIENT_ID", "")
    DHAN_ACCESS_TOKEN: str = os.getenv("DHAN_ACCESS_TOKEN", "")

    # Auto Token Refresh
    DHAN_PIN: str = os.getenv("DHAN_PIN", "")
    DHAN_TOTP_SECRET: str = os.getenv("DHAN_TOTP_SECRET", "")

    # Risk Limits (INR)
    DAILY_MAX_LOSS: float = float(os.getenv("DAILY_MAX_LOSS", "5000"))
    DAILY_PROFIT_TARGET: float = float(os.getenv("DAILY_PROFIT_TARGET", "20000"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    MAX_SINGLE_TRADE_RISK: float = float(os.getenv("MAX_SINGLE_TRADE_RISK", "2000"))
    MAX_ORDER_QUANTITY: int = int(os.getenv("MAX_ORDER_QUANTITY", "1800"))

    # Profit Lock
    PROFIT_LOCK_THRESHOLD: float = float(os.getenv("PROFIT_LOCK_THRESHOLD", "10000"))
    PROFIT_LOCK_PERCENTAGE: float = float(os.getenv("PROFIT_LOCK_PERCENTAGE", "50"))

    # Trailing Drawdown
    TRAILING_DRAWDOWN_ENABLED: bool = os.getenv("TRAILING_DRAWDOWN_ENABLED", "true").lower() == "true"
    TRAILING_DRAWDOWN_PERCENTAGE: float = float(os.getenv("TRAILING_DRAWDOWN_PERCENTAGE", "50"))

    # Dashboard
    DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "5555"))
    DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")

    # State
    STATE_ENCRYPTION_KEY: str = os.getenv("STATE_ENCRYPTION_KEY", "")
    STATE_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

    # Monitor
    MONITOR_INTERVAL: int = int(os.getenv("MONITOR_INTERVAL", "2"))

    # Position Sizing Defaults
    DEFAULT_RISK_AMOUNT: float = float(os.getenv("DEFAULT_RISK_AMOUNT", "1000"))
    DEFAULT_PRODUCT_TYPE: str = os.getenv("DEFAULT_PRODUCT_TYPE", "MARGIN")

    # Quick SL/TP offsets (points, shown as buttons on positions)
    QUICK_SL_OFFSETS: list = [50, 100, 200]
    QUICK_TP_OFFSETS: list = [100, 200, 500]

    # Trading Hours (IST)
    MARKET_OPEN_HOUR: int = 9
    MARKET_OPEN_MINUTE: int = 15
    MARKET_CLOSE_HOUR: int = 15
    MARKET_CLOSE_MINUTE: int = 30

    # Fyers DOM Analyzer
    FYERS_API_KEY: str = os.getenv("FYERS_API_KEY", "")
    FYERS_API_SECRET: str = os.getenv("FYERS_API_SECRET", "")
    FYERS_REDIRECT_URL: str = os.getenv("FYERS_REDIRECT_URL", "http://127.0.0.1:5555/fyers/callback")
    FYERS_WEBSOCKET_URL: str = os.getenv("FYERS_WEBSOCKET_URL", "wss://rtsocket-api.fyers.in/versova")
    FYERS_SYMBOL: str = os.getenv("FYERS_SYMBOL", "NSE:NIFTY25JULFUT")
    FYERS_LOT_SIZE: int = int(os.getenv("FYERS_LOT_SIZE", "50"))
    FYERS_ENABLED: bool = os.getenv("FYERS_ENABLED", "false").lower() == "true"

    @classmethod
    def validate(cls) -> list[str]:
        """Validate required configuration. Returns list of errors."""
        errors = []
        if not cls.DHAN_CLIENT_ID:
            errors.append("DHAN_CLIENT_ID is required")
        if not cls.DHAN_ACCESS_TOKEN:
            errors.append("DHAN_ACCESS_TOKEN is required")
        if cls.DAILY_MAX_LOSS <= 0:
            errors.append("DAILY_MAX_LOSS must be positive")
        if cls.PROFIT_LOCK_PERCENTAGE < 0 or cls.PROFIT_LOCK_PERCENTAGE > 100:
            errors.append("PROFIT_LOCK_PERCENTAGE must be between 0 and 100")
        return errors
