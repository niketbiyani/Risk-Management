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
        """Download and parse the Dhan scrip master CSV."""
        logger.info("Downloading instrument data from Dhan...")
        start = time.time()

        try:
            resp = requests.get(SCRIP_MASTER_URL, timeout=60)
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to download scrip master: %s", e)
            return 0

        reader = csv.DictReader(io.StringIO(resp.text))
        instruments = []

        for row in reader:
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
        tokens = query_upper.split()
        results = []

        for inst in self._instruments:
            # Match against multiple fields
            search_fields = (
                inst.trading_symbol.upper(),
                inst.custom_symbol.upper(),
                inst.symbol_name.upper(),
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
                    exp = datetime.strptime(inst.expiry_date, "%Y-%m-%d")
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
