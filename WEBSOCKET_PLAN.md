# WebSocket Integration Plan — Phase 1 & 2

## Goal
Replace REST polling for SL/TP triggers and order status with Dhan WebSocket feeds.
- Phase 1: MarketFeed LTP → SL/TP latency 2s → <50ms
- Phase 2: OrderUpdate → instant fill/rejection notifications

---

## Current Architecture (REST Polling)

```
_monitor_loop() — every 2s:
  _tick():
    api.get_positions()          → REST ~500ms
    api.get_order_book()         → REST ~300ms
    trade_mgr.check_sl_tp_triggers(positions)  ← reads LTP from positions
    _execute_sl_tp(trigger)
    check_pending_spreads()
    _execute_spread()
```

**Problems:**
- SL/TP latency = up to 2s (poll interval) + ~500ms (REST) = 2.5s worst case
- Order status shown in dashboard only after next poll cycle

---

## SDK Facts (already imported in dhan_api.py)

```python
from dhanhq import DhanContext, dhanhq, MarketFeed, OrderUpdate
```

**MarketFeed:**
```python
# Blocking — must run in thread
feed = MarketFeed(context, instruments, version="v2", on_message=callback)
feed.run_forever()  # blocks

# instruments = [(exchange_segment_int, "security_id", feed_type), ...]
# exchange_segment_int: 1=NSE_FNO, 2=BSE_FNO (check exact SDK constants)
# feed_type: "LTP" is sufficient for SL/TP
```

**OrderUpdate:**
```python
# Blocking — must run in thread
client = OrderUpdate(context)
client.on_update = callback
client.connect_to_dhan_websocket_sync()  # blocks
```

Both already have wrappers in dhan_api.py:
- `api.start_market_feed(instruments, callback)` — blocking
- `api.start_order_updates(callback)` — blocking

---

## Phase 1: MarketFeed for SL/TP

### 1a. Add to `trade_manager.py`

Add method `check_sl_tp_for_security(security_id, ltp)` — single-instrument version
of existing `check_sl_tp_triggers(positions)`:

```python
def check_sl_tp_for_security(self, security_id: str, ltp: float) -> list[dict]:
    """Called from WebSocket tick callback. Thread-safe read."""
    if security_id not in self._sl_tp_orders:
        return []
    config = self._sl_tp_orders[security_id]
    if not config.is_active:
        return []

    # Need position metadata (qty, exchange_segment, product_type) from cache
    pos = self._position_cache.get(security_id)
    if not pos:
        return []

    net_qty = pos.get("netQty", 0)
    if net_qty == 0:
        return []

    is_long = net_qty > 0
    triggered = []

    # Trailing SL update
    if config.trailing_sl and config.trailing_sl_points > 0:
        # Same trailing logic as in check_sl_tp_triggers
        profit_pts = (ltp - pos.get("avgPrice", 0)) * (1 if is_long else -1)
        if profit_pts >= config.trailing_sl_trigger:
            new_sl = ltp - config.trailing_sl_points if is_long else ltp + config.trailing_sl_points
            if is_long and new_sl > (config.current_sl_price or 0):
                config.current_sl_price = new_sl
            elif not is_long and new_sl < (config.current_sl_price or float('inf')):
                config.current_sl_price = new_sl

    sl_price = config.current_sl_price or config.stop_loss_price
    tp_price = config.take_profit_price

    if sl_price:
        if (is_long and ltp <= sl_price) or (not is_long and ltp >= sl_price):
            triggered.append({
                "action": "STOP_LOSS",
                "security_id": security_id,
                "exchange_segment": pos.get("exchangeSegment", ""),
                "product_type": pos.get("productType", ""),
                "quantity": abs(net_qty),
                "transaction_type": "SELL" if is_long else "BUY",
                "trigger_price": sl_price,
                "ltp": ltp,
            })
            config.is_active = False

    if tp_price and not triggered:
        if (is_long and ltp >= tp_price) or (not is_long and ltp <= tp_price):
            triggered.append({
                "action": "TAKE_PROFIT",
                "security_id": security_id,
                "exchange_segment": pos.get("exchangeSegment", ""),
                "product_type": pos.get("productType", ""),
                "quantity": abs(net_qty),
                "transaction_type": "SELL" if is_long else "BUY",
                "trigger_price": tp_price,
                "ltp": ltp,
            })
            config.is_active = False

    return triggered

def update_position_cache(self, positions: list[dict]):
    """Called from monitor poll loop to keep position metadata fresh."""
    self._position_cache = {
        str(p.get("securityId", "")): p
        for p in positions if p.get("netQty", 0) != 0
    }
```

Also add `self._position_cache: dict = {}` to `TradeManager.__init__()`.

---

### 1b. Add to `dhan_api.py`

Add non-blocking wrappers (start in daemon thread, return thread):

```python
def start_market_feed_async(self, instruments: list, callback) -> threading.Thread:
    """Start MarketFeed in a daemon thread with auto-reconnect."""
    def _run():
        while True:
            try:
                logger.info("MarketFeed: connecting with %d instruments", len(instruments))
                self._market_feed = MarketFeed(
                    self._context, instruments, version="v2", on_message=callback
                )
                self._market_feed.run_forever()
            except Exception as e:
                logger.error("MarketFeed disconnected: %s — reconnecting in 5s", e)
            time.sleep(5)

    t = threading.Thread(target=_run, daemon=True, name="MarketFeed")
    t.start()
    return t

def start_order_updates_async(self, callback) -> threading.Thread:
    """Start OrderUpdate in a daemon thread with auto-reconnect."""
    def _run():
        while True:
            try:
                logger.info("OrderUpdate: connecting")
                self._order_update_client = OrderUpdate(self._context)
                self._order_update_client.on_update = callback
                self._order_update_client.connect_to_dhan_websocket_sync()
            except Exception as e:
                logger.error("OrderUpdate disconnected: %s — reconnecting in 5s", e)
            time.sleep(5)

    t = threading.Thread(target=_run, daemon=True, name="OrderUpdate")
    t.start()
    return t

def update_market_feed_instruments(self, instruments: list):
    """
    Restart MarketFeed with new instrument list.
    Called when positions change (new trade opened/closed).
    No live subscribe method in SDK — must restart.
    """
    try:
        if self._market_feed:
            self._market_feed.close()
    except Exception:
        pass
    # Thread will auto-restart via reconnect loop
    # Caller must restart the thread with new instruments
    # (handled in monitor.py _refresh_market_feed)
```

Also add `import time` and `import threading` at top of dhan_api.py if not already there.

---

### 1c. Changes to `monitor.py`

**In `__init__`:**
```python
self._ws_lock = threading.Lock()          # protects WS state
self._market_feed_thread: threading.Thread | None = None
self._order_update_thread: threading.Thread | None = None
self._subscribed_instruments: list = []   # currently subscribed
self._sl_tp_executing: set = set()        # prevent double-fire
```

**In `start()`**, before `self._monitor_loop()`:
```python
# Start WebSocket feeds (non-blocking)
self._start_market_feed()
```

**New method `_start_market_feed()`:**
```python
def _start_market_feed(self):
    """Build instrument list from current positions and start MarketFeed."""
    try:
        positions = self.api.get_positions()
        instruments = self._build_instrument_list(positions)
        if not instruments:
            logger.info("No positions — MarketFeed not started yet")
            return
        self._subscribed_instruments = instruments
        self._market_feed_thread = self.api.start_market_feed_async(
            instruments, self._on_market_tick
        )
        logger.info("MarketFeed started for %d instruments", len(instruments))
    except Exception as e:
        logger.error("Failed to start MarketFeed: %s", e)

def _build_instrument_list(self, positions: list[dict]) -> list:
    """Convert positions to MarketFeed instrument tuples."""
    # exchange_segment string → int mapping (verify against SDK constants)
    seg_map = {
        "NSE_FNO": 1,
        "BSE_FNO": 2,
        "NSE_EQ": 3,
        "BSE_EQ": 4,
        # add others as needed
    }
    instruments = []
    for pos in positions:
        if pos.get("netQty", 0) == 0:
            continue
        seg_str = pos.get("exchangeSegment", "NSE_FNO")
        seg_int = seg_map.get(seg_str, 1)
        sec_id = str(pos.get("securityId", ""))
        if sec_id:
            instruments.append((seg_int, sec_id, "LTP"))
    return instruments
```

**New callback `_on_market_tick()`:**
```python
def _on_market_tick(self, tick_data: dict):
    """
    Called from MarketFeed thread on every LTP tick.
    Must be fast — no blocking I/O here.
    """
    try:
        security_id = str(tick_data.get("security_id") or tick_data.get("securityId", ""))
        ltp = float(tick_data.get("LTP") or tick_data.get("ltp") or 0)
        if not security_id or ltp <= 0:
            return

        # Update live bar on chart
        try:
            from dashboard import emit_ltp_tick
            emit_ltp_tick(security_id, ltp)
        except Exception:
            pass

        # Check SL/TP — skip if currently executing to prevent double-fire
        if security_id in self._sl_tp_executing:
            return
        triggers = self.trade_mgr.check_sl_tp_for_security(security_id, ltp)
        for trigger in triggers:
            self._sl_tp_executing.add(security_id)
            # Execute in separate thread so tick callback returns immediately
            threading.Thread(
                target=self._execute_sl_tp_and_cleanup,
                args=(trigger,),
                daemon=True
            ).start()

    except Exception as e:
        logger.error("Error in market tick callback: %s", e)

def _execute_sl_tp_and_cleanup(self, trigger: dict):
    """Execute SL/TP then remove from executing set."""
    try:
        self._execute_sl_tp(trigger)
    finally:
        self._sl_tp_executing.discard(trigger["security_id"])
```

**Changes to `_tick()`:**

Remove `check_sl_tp_triggers` from poll loop (WebSocket handles it now).
Keep poll loop for: positions (P&L calc), orders (display), risk evaluation.
Add `trade_mgr.update_position_cache(positions)` so WS callback has fresh metadata.
Add `_refresh_market_feed(positions)` to re-subscribe when positions change.

```python
def _tick(self):
    with self._lock:
        if self.state.is_locked_out and self._lockout_executed:
            return

        positions = self.api.get_positions()
        self._last_positions = positions

        # Keep position cache fresh for WS SL/TP callback
        self.trade_mgr.update_position_cache(positions)

        # Re-subscribe MarketFeed if positions changed
        self._refresh_market_feed(positions)

        # ... rest of P&L calc, risk check, spread triggers ...
        # REMOVE: sl_tp_triggers = self.trade_mgr.check_sl_tp_triggers(positions)
        # KEEP: spread_triggers, risk eval, lockout check, logging

def _refresh_market_feed(self, positions: list[dict]):
    """Restart MarketFeed if instrument list has changed."""
    new_instruments = self._build_instrument_list(positions)
    new_set = set((i[0], i[1]) for i in new_instruments)
    old_set = set((i[0], i[1]) for i in self._subscribed_instruments)

    if new_set == old_set:
        return  # No change

    logger.info("Position change detected — refreshing MarketFeed subscriptions")
    self._subscribed_instruments = new_instruments
    if new_instruments:
        self._market_feed_thread = self.api.start_market_feed_async(
            new_instruments, self._on_market_tick
        )
    else:
        logger.info("No open positions — MarketFeed paused")
```

---

### 1d. Tick data format (IMPORTANT — verify on first run)

The SDK's `on_message` callback receives data in unknown format until tested.
Log the raw tick to find field names:

```python
def _on_market_tick(self, tick_data):
    logger.debug("RAW TICK: %s", tick_data)  # Remove after confirming format
    ...
```

Likely fields (verify): `security_id`, `LTP`, `last_price`, `ltp`, `securityId`
The `_on_market_tick` code above tries multiple key names as fallback.

---

## Phase 2: OrderUpdate for real-time order status

### 2a. Start in `monitor.start()` alongside MarketFeed:

```python
self._order_update_thread = self.api.start_order_updates_async(
    self._on_order_update
)
```

### 2b. New callback `_on_order_update()` in monitor.py:

```python
def _on_order_update(self, update: dict):
    """
    Called from OrderUpdate thread on every order status change.
    Dhan pushes: PENDING, TRANSIT, TRADED, REJECTED, CANCELLED
    """
    try:
        logger.debug("OrderUpdate RAW: %s", update)  # Remove after confirming format
        order_id = str(update.get("orderId", ""))
        status = update.get("orderStatus", "")
        symbol = update.get("tradingSymbol", order_id)
        qty = update.get("tradedQuantity", 0)
        reason = update.get("omsErrorDescription") or update.get("rejectedReason", "")

        logger.info("Order %s → %s (%s)", order_id, status, symbol)

        # Emit to dashboard via SocketIO
        try:
            from dashboard import socketio
            socketio.emit("order_update", {
                "orderId": order_id,
                "orderStatus": status,
                "symbol": symbol,
                "tradedQty": qty,
                "reason": reason,
            })

            if status == "REJECTED":
                socketio.emit("SPREAD_FAILED", {
                    "orderId": order_id,
                    "reason": reason or "Rejected by exchange",
                })

        except Exception:
            pass

        # Remove the 2.5s post-spread poll from _execute_spread
        # (handled here in real-time instead)

    except Exception as e:
        logger.error("Error in order update callback: %s", e)
```

### 2c. Remove from `_execute_spread()` in monitor.py:

Remove the entire "Poll order status after brief delay" block (lines ~333-380):
```python
# DELETE THIS ENTIRE BLOCK:
time.sleep(2.5)
hedge_status = "UNKNOWN"
sell_status = "UNKNOWN"
...
# (the 2.5s sleep + order book poll)
```

Replace with just:
```python
logger.info("Spread %s: both orders placed — awaiting OrderUpdate confirmation", spread_id)
# Status will arrive via _on_order_update callback
```

### 2d. dashboard.py JS — listen for new SocketIO events:

Add to the existing `setupSocketListeners()` function:

```javascript
socket.on('order_update', function(data) {
    // Show toast for fills
    if (data.orderStatus === 'TRADED') {
        showToast('✓ Order filled: ' + data.symbol + ' qty ' + data.tradedQty, 'success');
        playAlert('order');
    }
    // Rejections already handled by existing SPREAD_FAILED listener
});
```

---

## Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Unknown tick field names | Log raw tick on startup, field-check multiple keys |
| MarketFeed connection limit | Dhan allows ~3 concurrent WS; we use depth(1) + market(1) + order(1) = 3 total, should be fine |
| Double SL/TP execution | `_sl_tp_executing` set prevents re-entry |
| Position cache stale between polls | SL/TP only needs qty sign (long/short) + exchange_segment; these change rarely |
| Thread crash silently | Auto-reconnect loop in `_run()` restarts after 5s; logs the error |
| Token expiry mid-session | On reconnect, `DhanContext` re-reads from Config (tokens auto-refresh daily at 8:45am) |

---

## Files Changed Summary

| File | Changes |
|---|---|
| `trade_manager.py` | Add `_position_cache`, `update_position_cache()`, `check_sl_tp_for_security()` |
| `dhan_api.py` | Add `start_market_feed_async()`, `start_order_updates_async()` |
| `monitor.py` | Add `_start_market_feed()`, `_build_instrument_list()`, `_on_market_tick()`, `_on_order_update()`, `_refresh_market_feed()`, `_execute_sl_tp_and_cleanup()`; modify `_tick()` to remove SL/TP poll; modify `_execute_spread()` to remove 2.5s sleep |
| `dashboard.py` | Add `emit_ltp_tick()` server function; add `order_update` SocketIO listener in JS |

---

## Implementation Order

1. `trade_manager.py` — add cache + new method (no risk, additive only)
2. `dhan_api.py` — add async wrappers (additive)
3. `monitor.py` — add Phase 1 WS feed, keep old SL/TP poll as fallback initially
4. Test Phase 1: confirm tick format, confirm SL/TP fires faster
5. Remove old SL/TP poll from `_tick()` once confirmed working
6. `monitor.py` — add Phase 2 OrderUpdate
7. Remove 2.5s sleep from `_execute_spread()`
8. `dashboard.py` — add JS listener for `order_update`

---

## How to Resume

Say: "Read WEBSOCKET_PLAN.md and implement the WebSocket integration"
