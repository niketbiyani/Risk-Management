import pytest
import time
import threading
from unittest.mock import MagicMock, patch
from monitor import PositionMonitor

def test_candle_close_trigger_queuing():
    """Verify that a candle close trigger is correctly queued with expected rollover time."""
    monitor = PositionMonitor()
    
    # Mock self._lock to avoid threading issues in test
    monitor._lock = threading.Lock()
    
    # Queue a trigger on a 15-second timeframe
    security_id = "12345"
    trig_id = monitor.queue_candle_close_trigger(
        security_id=security_id,
        direction="SELL",
        quantity=50,
        buffer=1.0,
        timeframe=15,
        product_type="MARGIN",
        exchange_segment="NSE_FNO",
        stop_loss=180.0
    )
    
    assert trig_id is not None
    assert trig_id in monitor._candle_close_triggers
    
    trig = monitor._candle_close_triggers[trig_id]
    assert trig["security_id"] == security_id
    assert trig["direction"] == "SELL"
    assert trig["quantity"] == 50
    assert trig["buffer"] == 1.0
    assert trig["timeframe"] == 15
    assert trig["stop_loss"] == 180.0
    
    # Rollover time should be aligned to a multiple of 15 seconds in the future
    now = time.time()
    assert trig["rollover_time"] > now
    assert trig["rollover_time"] % 15 == 0


@patch("monitor.BrokerAPI")
def test_execute_candle_close_order(mock_broker_class):
    """Verify order placement and automated Stop Loss queuing on execution."""
    monitor = PositionMonitor()
    
    # Mock out API client methods
    mock_api = monitor.api
    mock_api.get_ltp = MagicMock(return_value=150.0)
    
    # Mock out interceptor place_order method
    monitor.interceptor = MagicMock()
    monitor.interceptor.place_order.return_value = {
        "status": "success",
        "data": {"orderId": "ORD_TEST_99"}
    }
    
    trig_payload = {
        "security_id": "54321",
        "direction": "SELL",
        "quantity": 50,
        "buffer": 1.0,
        "product_type": "MARGIN",
        "exchange_segment": "NSE_FNO",
        "stop_loss": 180.0
    }
    
    # Run the execution method
    monitor._execute_candle_close_order(trig_payload)
    
    # Limit price should be Close (150.0) + Buffer (1.0) = 151.0
    monitor.interceptor.place_order.assert_called_once_with(
        security_id="54321",
        exchange_segment="NSE_FNO",
        transaction_type="SELL",
        quantity=50,
        order_type="LIMIT",
        product_type="MARGIN",
        price=151.0,
        trigger_price=0.0
    )
    
    # Stop loss should be queued inside self._pending_sl_orders for when the order fills
    assert "ORD_TEST_99" in monitor._pending_sl_orders
    sl_job = monitor._pending_sl_orders["ORD_TEST_99"]
    assert sl_job["stop_loss"] == 180.0
    assert sl_job["security_id"] == "54321"


@patch("monitor.BrokerAPI")
def test_automated_stop_loss_on_fill(mock_broker_class):
    """Verify that Stop Loss is instantly placed when the order status changes to TRADED."""
    monitor = PositionMonitor()
    mock_api = monitor.api
    mock_api.place_order = MagicMock(return_value={"orderId": "SL_ORD_100"})
    
    # Populate the pending SL orders cache
    monitor._pending_sl_orders["ORD_TEST_99"] = {
        "security_id": "54321",
        "exchange_segment": "NSE_FNO",
        "quantity": 50,
        "product_type": "MARGIN",
        "direction": "SELL",
        "stop_loss": 180.0
    }
    
    # Trigger an order update with "TRADED" status
    update_payload = {
        "orderId": "ORD_TEST_99",
        "orderStatus": "TRADED",
        "tradingSymbol": "NIFTY24CE",
        "tradedQuantity": 50
    }
    
    with patch("dashboard.socketio"):  # mock socketio emission
        monitor._on_order_update(update_payload)
        
    # Verify that the pending SL order has been executed and cleared
    assert "ORD_TEST_99" not in monitor._pending_sl_orders
    
    # The SL order should be a BUY order (covering the SELL entry position)
    # The SL limit price should be stop_loss (180.0) + buffer (approx 1.8) = 181.80
    mock_api.place_order.assert_called_once()
    args, kwargs = mock_api.place_order.call_args
    assert kwargs["security_id"] == "54321"
    assert kwargs["transaction_type"] == "BUY"
    assert kwargs["order_type"] == "STOP_LOSS_LIMIT"
    assert kwargs["trigger_price"] == 180.0
    assert kwargs["price"] > 180.0  # limit price with slippage buffer
