"""
Instrument Cache - Loads and searches Dhan's scrip master.
Downloads the compact CSV at startup, parses into memory, and provides
fast instrument search with lot size lookups for position sizing.
"""

import csv
import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


@dataclass
class Instrument:
    security_id: str
    trading_symbol: str
    custom_symbol: str
    symbol_name: str
    exchange: str
    segment: str
    instrument_type: str
    lot_size: int
    expiry_date: str
    strike_price: float
    option_type: str
    tick_size: float
    exchange_segment: str  # Pre-computed: NSE_FNO, NSE_EQ, etc.


class InstrumentCache:
    """In-memory instrument cache with search."""

    def __init__(self):
        self._instruments: list[Instrument] = []
        self._by_id: dict[str, Instrument] = {}
        self._loaded_at: float = 0
        self.count: int = 0

    def load(self) -> int:
        """Download and parse the scrip master CSV based on the active broker."""
        from config import Config
        if Config.ACTIVE_BROKER == "KOTAK":
            return self.load_kotak()

        logger.info("Downloading instrument data from Dhan...")
        start = time.time()
        
        urls = [
            "https://images.dhan.co/api-data/api-scrip-master.csv",
            "https://images.dhan.co/api-data/api-scrip-master-bse.csv"
        ]
        
        rows = []
        for url in urls:
            try:
                logger.info("Downloading from %s", url)
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                reader = csv.DictReader(io.StringIO(resp.text))
                rows.extend(list(reader))
            except Exception as e:
                logger.error("Failed to download scrip master from %s: %s", url, e)

        if not rows:
            logger.error("No scrip data downloaded.")
            return 0

        instruments = []
        for row in rows:
            exchange = row.get("SEM_EXM_EXCH_ID", "")
            segment = row.get("SEM_SEGMENT", "")
            inst_type = row.get("SEM_INSTRUMENT_NAME", "")

            # Filter: only NSE/BSE equity + F&O
            if exchange not in ("NSE", "BSE"):
                continue
            if inst_type not in ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK",
                                 "EQUITY", "ETF"):
                continue

            # Compute exchange_segment for Dhan API
            if segment == "D":
                exchange_segment = f"{exchange}_FNO"
            elif segment in ("E", "I"):
                exchange_segment = "NSE" if exchange == "NSE" else "BSE"
            else:
                exchange_segment = exchange

            # Parse lot size
            try:
                lot_size = int(float(row.get("SEM_LOT_UNITS", "1") or "1"))
            except (ValueError, TypeError):
                lot_size = 1

            # Parse strike price
            try:
                strike = float(row.get("SEM_STRIKE_PRICE", "0") or "0")
            except (ValueError, TypeError):
                strike = 0.0

            # Parse tick size
            try:
                tick = float(row.get("SEM_TICK_SIZE", "0.05") or "0.05")
            except (ValueError, TypeError):
                tick = 0.05

            inst = Instrument(
                security_id=row.get("SEM_SMST_SECURITY_ID", ""),
                trading_symbol=row.get("SEM_TRADING_SYMBOL", ""),
                custom_symbol=row.get("SEM_CUSTOM_SYMBOL", ""),
                symbol_name=row.get("SM_SYMBOL_NAME", ""),
                exchange=exchange,
                segment=segment,
                instrument_type=inst_type,
                lot_size=lot_size,
                expiry_date=row.get("SEM_EXPIRY_DATE", ""),
                strike_price=strike,
                option_type=row.get("SEM_OPTION_TYPE", "XX"),
                tick_size=tick,
                exchange_segment=exchange_segment,
            )
            instruments.append(inst)

        self._instruments = instruments
        self._by_id = {inst.security_id: inst for inst in instruments}
        self._loaded_at = time.time()
        self.count = len(instruments)

        elapsed = time.time() - start
        logger.info("Loaded %d instruments in %.1fs", self.count, elapsed)
        return self.count

    def load_kotak(self) -> int:
        """Download and parse Kotak Neo master scrips for NSE/BSE Equity & Derivatives."""
        logger.info("Initializing Kotak Neo scrip master download...")
        start = time.time()
        
        csv_data_list = []
        
        # 1. Download via SDK or read locally cached files
        try:
            from kotak_api import KotakNeoAPI
            api = KotakNeoAPI()
            if api.client:
                logger.info("Downloading Kotak Neo scrip master files via SDK...")
                for segment in ("nse_cm", "nse_fo", "bse_fo"):
                    try:
                        res = api.client.scrip_master(exchange_segment=segment)
                        if isinstance(res, str) and res.endswith(".csv"):
                            with open(res, "r", encoding="utf-8") as f:
                                csv_data_list.append(f.read())
                        elif isinstance(res, str):
                            csv_data_list.append(res)
                    except Exception as ex:
                        logger.warning("Failed to download Kotak segment %s: %s", segment, ex)
        except Exception as e:
            logger.warning("Could not download Kotak scrips dynamically: %s. Attempting to load from local cache.", e)

        # 2. Local fallback if no dynamic data
        if not csv_data_list:
            for fname in ("nse_cm.csv", "nse_fo.csv", "bse_fo.csv"):
                if os.path.exists(fname):
                    logger.info("Found local cached Kotak scrip file: %s", fname)
                    with open(fname, "r", encoding="utf-8") as f:
                        csv_data_list.append(f.read())
        
        if not csv_data_list:
            logger.error("No Kotak scrip master files available (neither dynamic download nor local files found).")
            return 0

        # 3. Parse the CSV files
        instruments = []
        for csv_text in csv_data_list:
            reader = csv.DictReader(io.StringIO(csv_text))
            for row in reader:
                row_lower = {k.lower(): v for k, v in row.items() if k is not None}
                
                security_id = row_lower.get("psymbol", row_lower.get("instrument_token", row_lower.get("security_id", "")))
                trading_symbol = row_lower.get("ptrdsymbol", row_lower.get("trading_symbol", ""))
                symbol_name = row_lower.get("psymbolname", row_lower.get("symbol_name", ""))
                exchange = row_lower.get("pexchange", row_lower.get("exchange", "NSE")).upper()
                segment = row_lower.get("psegment", row_lower.get("segment", "nse_fo")).lower()
                inst_type = row_lower.get("pinsttype", row_lower.get("instrument_name", "OPTIDX")).upper()
                
                if not security_id or not trading_symbol:
                    continue
                
                if exchange not in ("NSE", "BSE"):
                    continue
                if inst_type not in ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK", "EQUITY", "ETF"):
                    continue
                
                if segment == "nse_fo":
                    exchange_segment = "NSE_FNO"
                elif segment == "bse_fo":
                    exchange_segment = "BSE_FNO"
                elif segment == "nse_cm":
                    exchange_segment = "NSE_EQ"
                elif segment == "bse_cm":
                    exchange_segment = "BSE_EQ"
                else:
                    exchange_segment = "NSE_FNO"
                
                if symbol_name in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") and inst_type == "OPTIDX":
                    exchange_segment = "NSE_FNO"
                elif symbol_name == "SENSEX" and inst_type == "OPTIDX":
                    exchange_segment = "BSE_FNO"
                
                try:
                    lot_size = int(float(row_lower.get("plotsize", row_lower.get("lot_size", row_lower.get("lot_units", "1"))) or "1"))
                except (ValueError, TypeError):
                    lot_size = 1
                
                try:
                    strike = float(row_lower.get("pstrikeprice", row_lower.get("strike_price", "0")) or "0")
                except (ValueError, TypeError):
                    strike = 0.0
                
                try:
                    tick = float(row_lower.get("pticksize", row_lower.get("tick_size", "0.05")) or "0.05")
                except (ValueError, TypeError):
                    tick = 0.05
                
                expiry_raw = row_lower.get("pexpirydate", row_lower.get("expiry_date", ""))
                expiry_date = ""
                if expiry_raw:
                    try:
                        dt = datetime.strptime(expiry_raw.strip(), "%d-%b-%Y")
                        expiry_date = dt.strftime("%Y-%m-%d")
                    except Exception:
                        try:
                            dt = datetime.strptime(expiry_raw.strip(), "%Y-%m-%d")
                            expiry_date = dt.strftime("%Y-%m-%d")
                        except Exception:
                            expiry_date = expiry_raw
                
                inst = Instrument(
                    security_id=str(security_id),
                    trading_symbol=trading_symbol,
                    custom_symbol="",
                    symbol_name=symbol_name,
                    exchange=exchange,
                    segment=segment,
                    instrument_type=inst_type,
                    lot_size=lot_size,
                    expiry_date=expiry_date,
                    strike_price=strike,
                    option_type=row_lower.get("poptiontype", row_lower.get("option_type", "XX")).upper(),
                    tick_size=tick,
                    exchange_segment=exchange_segment
                )
                instruments.append(inst)

        self._instruments = instruments
        self._by_id = {inst.security_id: inst for inst in instruments}
        self._loaded_at = time.time()
        self.count = len(instruments)

        elapsed = time.time() - start
        logger.info("Loaded %d Kotak instruments in %.1fs", self.count, elapsed)
        return self.count

    def reload(self) -> int:
        """Force reload the instrument data."""
        return self.load()

    def get_by_id(self, security_id: str) -> Instrument | None:
        """Look up an instrument by security_id."""
        return self._by_id.get(str(security_id))

    def get_exchange_segment(self, security_id: str) -> str:
        """Get the exchange segment string for a security_id."""
        inst = self._by_id.get(str(security_id))
        return inst.exchange_segment if inst else "NSE_FNO"

    def get_lot_size(self, security_id: str) -> int:
        """Get lot size for a security_id."""
        inst = self._by_id.get(str(security_id))
        return inst.lot_size if inst else 1

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """
        Search instruments by query string.
        Matches against trading_symbol, custom_symbol, and symbol_name.
        Prioritizes NSE, near-term expiries, and exact prefix matches.
        """
        if not query or len(query) < 2:
            return []

        query_upper = query.upper().strip()
        # Filter out timeframe noise tokens (e.g. 1m, 3m, 5m, 15m, 1d, 1h)
        noise = {"1M", "2M", "3M", "5M", "10M", "15M", "30M", "1H", "2H", "4H", "1D", "1W", "D", "W", "MIN"}
        tokens = [t for t in query_upper.split() if t not in noise and not (len(t) >= 2 and t[:-1].isdigit() and t[-1] in ("M", "H", "D", "W"))]
        if not tokens:
            tokens = query_upper.split()
        results = []

        for inst in self._instruments:
            # Match against multiple fields including constructed strike + option type representation
            strike_str = str(int(inst.strike_price)) if inst.strike_price > 0 else ""
            search_fields = (
                inst.trading_symbol.upper(),
                inst.custom_symbol.upper(),
                inst.symbol_name.upper(),
                f"{inst.symbol_name.upper()} {strike_str} {inst.option_type.upper()}",
                f"{inst.symbol_name.upper()}{strike_str}{inst.option_type.upper()}",
                strike_str,
                inst.option_type.upper()
            )

            # Check if all tokens match at least one field
            all_match = True
            score = 0
            for token in tokens:
                token_found = False
                for field_val in search_fields:
                    if token in field_val:
                        token_found = True
                        # Prefix match scores higher
                        if field_val.startswith(token):
                            score += 10
                        else:
                            score += 1
                        break
                if not token_found:
                    all_match = False
                    break

            if not all_match:
                continue

            # Bonus: NSE preferred over BSE
            if inst.exchange == "NSE":
                score += 5

            # Bonus: near-term expiry preferred
            if inst.expiry_date:
                try:
                    # Try parsing with time first, then fallback to date-only
                    try:
                        exp = datetime.strptime(inst.expiry_date.strip(), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        exp = datetime.strptime(inst.expiry_date.strip(), "%Y-%m-%d")
                        
                    days_to_expiry = (exp - datetime.now()).days
                    if 0 <= days_to_expiry <= 7:
                        score += 20  # Current week
                    elif 0 <= days_to_expiry <= 14:
                        score += 15  # Next week
                    elif 0 <= days_to_expiry <= 35:
                        score += 10  # Current month
                    elif days_to_expiry < 0:
                        score -= 50  # Expired
                except ValueError:
                    pass

            # Bonus: exact symbol match
            if inst.symbol_name.upper() == tokens[0]:
                score += 25

            results.append((score, inst))

        # Sort by score descending, limit results
        results.sort(key=lambda x: x[0], reverse=True)

        return [
            {
                "security_id": inst.security_id,
                "trading_symbol": inst.trading_symbol,
                "custom_symbol": inst.custom_symbol,
                "symbol_name": inst.symbol_name,
                "exchange": inst.exchange,
                "exchange_segment": inst.exchange_segment,
                "instrument_type": inst.instrument_type,
                "lot_size": inst.lot_size,
                "expiry_date": inst.expiry_date,
                "strike_price": inst.strike_price,
                "option_type": inst.option_type,
                "tick_size": inst.tick_size,
            }
            for _, inst in results[:limit]
        ]
