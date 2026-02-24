"""
Fyers DOM Analyzer - WebSocket Module.
Handles real-time TBT market depth data via Fyers WebSocket,
processes protobuf messages, maintains 50-level order book,
and calculates imbalance metrics.
"""

import os
import json
import time
import asyncio
import threading

import websockets
import msg_pb2

from fyers_database import (
    db_session, FyersAuth, get_fyers_auth_data, is_fyers_token_valid_for_today
)

# Configuration
WEBSOCKET_URL = os.getenv('FYERS_WEBSOCKET_URL', 'wss://rtsocket-api.fyers.in/versova').strip("'")
SYMBOL = os.getenv('FYERS_SYMBOL', 'NSE:NIFTY25JULFUT').strip("'")
LOT_SIZE = int(os.getenv('FYERS_LOT_SIZE', '50'))

# Global state
websocket_enabled = False
websocket_task = None
order_books = {}

# Reference to SocketIO instance (set by dashboard at startup)
_socketio = None


def set_socketio(sio):
    """Set the SocketIO instance for emitting events."""
    global _socketio
    _socketio = sio


def get_fyers_config():
    """Return current Fyers configuration."""
    return {
        'symbol': SYMBOL,
        'lot_size': LOT_SIZE,
        'websocket_url': WEBSOCKET_URL,
        'connected': websocket_enabled,
    }


def enable_websocket():
    """Enable Fyers WebSocket (called after successful auth)."""
    global websocket_enabled
    websocket_enabled = True
    print("[FYERS WS] WebSocket enabled")


def disable_websocket():
    """Disable Fyers WebSocket (called on logout or midnight cleanup)."""
    global websocket_enabled
    websocket_enabled = False
    print("[FYERS WS] WebSocket disabled")


# ── Order Book Management ──────────────────────────────────────────────

def update_order_book(ticker, bids, asks, tbq, tsq, timestamp, is_snapshot):
    """Update order book with guaranteed 50-level depth maintenance."""
    global order_books

    if ticker not in order_books:
        order_books[ticker] = {
            'bids': {i: {'price': 0.0, 'qty': 0, 'orders': 0, 'level': i} for i in range(50)},
            'asks': {i: {'price': 0.0, 'qty': 0, 'orders': 0, 'level': i} for i in range(50)},
            'tbq': 0,
            'tsq': 0,
            'timestamp': 0,
            'initialized': False
        }

    order_books[ticker]['tbq'] = tbq
    order_books[ticker]['tsq'] = tsq
    order_books[ticker]['timestamp'] = timestamp

    first_update = not order_books[ticker].get('initialized', False)

    if is_snapshot or first_update:
        if first_update:
            for i in range(50):
                order_books[ticker]['bids'][i] = {'price': 0.0, 'qty': 0, 'orders': 0, 'level': i}
                order_books[ticker]['asks'][i] = {'price': 0.0, 'qty': 0, 'orders': 0, 'level': i}

    # Process bid updates
    for bid in bids:
        level = bid['level']
        if 0 <= level < 50:
            old_data = order_books[ticker]['bids'][level]
            if bid['price'] == 0.0 and bid['qty'] > 0:
                if old_data['price'] > 0:
                    order_books[ticker]['bids'][level] = {
                        'price': old_data['price'], 'qty': bid['qty'],
                        'orders': bid['orders'], 'level': level
                    }
            elif bid['qty'] == 0:
                if bid['price'] == 0.0 and old_data['price'] > 0:
                    order_books[ticker]['bids'][level] = {
                        'price': old_data['price'], 'qty': 0,
                        'orders': bid['orders'], 'level': level
                    }
                elif bid['price'] > 0:
                    order_books[ticker]['bids'][level] = bid
                else:
                    if old_data['price'] == 0.0:
                        order_books[ticker]['bids'][level] = {
                            'price': 0.0, 'qty': 0, 'orders': 0, 'level': level
                        }
                    else:
                        order_books[ticker]['bids'][level] = {
                            'price': old_data['price'], 'qty': 0,
                            'orders': bid['orders'], 'level': level
                        }
            else:
                order_books[ticker]['bids'][level] = bid

    # Process ask updates
    for ask in asks:
        level = ask['level']
        if 0 <= level < 50:
            old_data = order_books[ticker]['asks'][level]
            if ask['price'] == 0.0 and ask['qty'] > 0:
                if old_data['price'] > 0:
                    order_books[ticker]['asks'][level] = {
                        'price': old_data['price'], 'qty': ask['qty'],
                        'orders': ask['orders'], 'level': level
                    }
            elif ask['qty'] == 0:
                if ask['price'] == 0.0 and old_data['price'] > 0:
                    order_books[ticker]['asks'][level] = {
                        'price': old_data['price'], 'qty': 0,
                        'orders': ask['orders'], 'level': level
                    }
                elif ask['price'] > 0:
                    order_books[ticker]['asks'][level] = ask
                else:
                    if old_data['price'] == 0.0:
                        order_books[ticker]['asks'][level] = {
                            'price': 0.0, 'qty': 0, 'orders': 0, 'level': level
                        }
                    else:
                        order_books[ticker]['asks'][level] = {
                            'price': old_data['price'], 'qty': 0,
                            'orders': ask['orders'], 'level': level
                        }
            else:
                order_books[ticker]['asks'][level] = ask

    order_books[ticker]['initialized'] = True


def get_full_order_book(ticker):
    """Get the complete 50-level order book with proper depth reconstruction."""
    if ticker not in order_books:
        return None

    book = order_books[ticker]

    raw_bids = []
    for i in range(50):
        bid = book['bids'][i]
        if bid['price'] > 0:
            raw_bids.append({
                'price': bid['price'], 'qty': bid['qty'],
                'orders': bid['orders'], 'level': len(raw_bids)
            })
    raw_bids.sort(key=lambda x: x['price'], reverse=True)
    active_bids = [
        {'price': b['price'], 'qty': b['qty'], 'orders': b['orders'], 'level': i}
        for i, b in enumerate(raw_bids[:50])
    ]

    raw_asks = []
    for i in range(50):
        ask = book['asks'][i]
        if ask['price'] > 0:
            raw_asks.append({
                'price': ask['price'], 'qty': ask['qty'],
                'orders': ask['orders'], 'level': len(raw_asks)
            })
    raw_asks.sort(key=lambda x: x['price'])
    active_asks = [
        {'price': a['price'], 'qty': a['qty'], 'orders': a['orders'], 'level': i}
        for i, a in enumerate(raw_asks[:50])
    ]

    return {
        'ticker': ticker,
        'tbq': book['tbq'],
        'tsq': book['tsq'],
        'timestamp': book['timestamp'],
        'bids': active_bids,
        'asks': active_asks,
        'bidprice': [b['price'] for b in active_bids],
        'askprice': [a['price'] for a in active_asks],
        'bidqty': [b['qty'] for b in active_bids],
        'askqty': [a['qty'] for a in active_asks],
        'bidordn': [b['orders'] for b in active_bids],
        'askordn': [a['orders'] for a in active_asks]
    }


# ── Imbalance Calculation ──────────────────────────────────────────────

def calculate_order_book_imbalance(bids, asks, depth):
    """Calculate order book imbalance at specified depth level."""
    try:
        bid_qty = sum(bid['qty'] for bid in bids[:depth])
        ask_qty = sum(ask['qty'] for ask in asks[:depth])

        total_qty = bid_qty + ask_qty
        if total_qty > 0:
            imbalance = (bid_qty - ask_qty) / total_qty
            imbalance_pct = imbalance * 100
        else:
            imbalance = 0
            imbalance_pct = 0

        return {
            'bid_qty': bid_qty,
            'ask_qty': ask_qty,
            'imbalance': imbalance,
            'imbalance_pct': imbalance_pct,
            'interpretation': interpret_imbalance(imbalance_pct)
        }
    except Exception:
        return {
            'bid_qty': 0, 'ask_qty': 0, 'imbalance': 0,
            'imbalance_pct': 0, 'interpretation': 'Unknown'
        }


def interpret_imbalance(imbalance_pct):
    """Interpret the imbalance percentage."""
    if imbalance_pct > 30:
        return "Strong Buying Pressure"
    elif imbalance_pct > 15:
        return "Moderate Buying Pressure"
    elif imbalance_pct > 5:
        return "Slight Buying Pressure"
    elif imbalance_pct < -30:
        return "Strong Selling Pressure"
    elif imbalance_pct < -15:
        return "Moderate Selling Pressure"
    elif imbalance_pct < -5:
        return "Slight Selling Pressure"
    else:
        return "Balanced"


# ── Protobuf Processing ───────────────────────────────────────────────

def process_market_depth(message_bytes):
    """Process market depth protobuf message."""
    try:
        socket_message = msg_pb2.SocketMessage()
        socket_message.ParseFromString(message_bytes)

        if socket_message.error:
            print(f"[FYERS WS] Error in socket message: {socket_message.msg}")
            return None

        market_data = {}
        for ticker, feed in socket_message.feeds.items():
            timestamp = feed.feed_time.value if feed.feed_time else None
            tbq = feed.depth.tbq.value if feed.depth.tbq else 0
            tsq = feed.depth.tsq.value if feed.depth.tsq else 0
            is_snapshot = socket_message.snapshot

            update_bids = []
            for bid in feed.depth.bids:
                update_bids.append({
                    'price': bid.price.value / 100.0,
                    'qty': bid.qty.value,
                    'orders': bid.nord.value,
                    'level': bid.num.value
                })

            update_asks = []
            for ask in feed.depth.asks:
                update_asks.append({
                    'price': ask.price.value / 100.0,
                    'qty': ask.qty.value,
                    'orders': ask.nord.value,
                    'level': ask.num.value
                })

            update_order_book(ticker, update_bids, update_asks, tbq, tsq, timestamp, is_snapshot)

            full_book = get_full_order_book(ticker)
            if full_book:
                frontend_data = {
                    'ticker': full_book['ticker'],
                    'timestamp': full_book['timestamp'] * 1000,
                    'total_bid_qty': full_book['tbq'],
                    'total_sell_qty': full_book['tsq'],
                    'bids': [
                        {'level': b['level'], 'price': b['price'],
                         'quantity': b['qty'], 'orders': b['orders']}
                        for b in full_book['bids']
                    ],
                    'asks': [
                        {'level': a['level'], 'price': a['price'],
                         'quantity': a['qty'], 'orders': a['orders']}
                        for a in full_book['asks']
                    ],
                    'bidprice': full_book['bidprice'],
                    'askprice': full_book['askprice'],
                    'bidqty': full_book['bidqty'],
                    'askqty': full_book['askqty'],
                    'bidordn': full_book['bidordn'],
                    'askordn': full_book['askordn'],
                    'imbalance_10': calculate_order_book_imbalance(
                        full_book['bids'], full_book['asks'], 10),
                    'imbalance_20': calculate_order_book_imbalance(
                        full_book['bids'], full_book['asks'], 20),
                    'imbalance_50': calculate_order_book_imbalance(
                        full_book['bids'], full_book['asks'], 50)
                }
                market_data[ticker] = frontend_data

        return market_data if len(market_data) > 0 else None
    except Exception as e:
        print(f"[FYERS WS] Error processing market depth: {e}")
        return None


# ── WebSocket Client ──────────────────────────────────────────────────

_ws_connection = None
_last_ping_time = 0


async def subscribe_symbols(ws):
    """Subscribe to market depth data for configured symbol."""
    try:
        subscribe_msg = {
            "type": 1,
            "data": {
                "subs": 1,
                "symbols": [SYMBOL],
                "mode": "depth",
                "channel": "1"
            }
        }
        await ws.send(json.dumps(subscribe_msg))
        print(f"[FYERS WS] Subscribed to {SYMBOL}")

        resume_msg = {
            "type": 2,
            "data": {
                "resumeChannels": ["1"],
                "pauseChannels": []
            }
        }
        await ws.send(json.dumps(resume_msg))
        print("[FYERS WS] Channel resumed")
    except Exception as e:
        print(f"[FYERS WS] Error subscribing: {e}")


async def websocket_client():
    """
    Main WebSocket client loop with SEBI compliance.
    Only connects when user is authenticated with a valid-today token.
    """
    global _ws_connection, _last_ping_time, websocket_enabled

    print("[FYERS WS] WebSocket client started (waiting for login)")

    while True:
        try:
            if not websocket_enabled:
                await asyncio.sleep(5)
                continue

            # Get active auth from database
            active_auth = db_session.query(FyersAuth).filter_by(
                is_revoked=False, broker='fyers'
            ).first()

            if not active_auth:
                print("[FYERS WS] No active auth found, waiting...")
                websocket_enabled = False
                await asyncio.sleep(10)
                continue

            if not is_fyers_token_valid_for_today(active_auth.name):
                print(f"[FYERS WS] Token for {active_auth.name} not valid today")
                websocket_enabled = False
                await asyncio.sleep(10)
                continue

            auth_data = get_fyers_auth_data(active_auth.name)
            if not auth_data or not auth_data['auth_token'] or not auth_data['api_key']:
                print("[FYERS WS] No valid auth data, waiting...")
                websocket_enabled = False
                await asyncio.sleep(10)
                continue

            print(f"[FYERS WS] Connecting to {WEBSOCKET_URL}...")
            auth_header = f"{auth_data['api_key']}:{auth_data['auth_token']}"

            async with websockets.connect(
                WEBSOCKET_URL,
                extra_headers={"Authorization": auth_header}
            ) as ws:
                _ws_connection = ws
                print("[FYERS WS] Connected!")

                await subscribe_symbols(ws)
                _last_ping_time = time.time()

                while True:
                    if not websocket_enabled:
                        print("[FYERS WS] Disabled, closing connection...")
                        break

                    try:
                        current_time = time.time()
                        if current_time - _last_ping_time >= 30:
                            await ws.send("ping")
                            _last_ping_time = current_time

                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)

                        if isinstance(message, bytes):
                            market_data = process_market_depth(message)
                            if market_data and _socketio:
                                _socketio.emit('fyers_market_depth', market_data)
                        else:
                            print(f"[FYERS WS] Text message: {message}")

                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        print("[FYERS WS] Connection closed")
                        break
                    except Exception as e:
                        print(f"[FYERS WS] Error: {e}")

        except Exception as e:
            print(f"[FYERS WS] Connection error: {e}")

        if websocket_enabled:
            print("[FYERS WS] Retrying in 5 seconds...")
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(5)


def run_fyers_websocket():
    """Run the Fyers WebSocket client in an asyncio event loop (for threading)."""
    asyncio.run(websocket_client())


def start_fyers_websocket_thread():
    """Start the Fyers WebSocket in a background daemon thread."""
    ws_thread = threading.Thread(target=run_fyers_websocket, daemon=True, name="fyers-websocket")
    ws_thread.start()
    print("[FYERS WS] Background thread started")
    return ws_thread
