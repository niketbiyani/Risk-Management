const VPS_IP = "88.208.255.34";
const VPS_URL = `https://${VPS_IP}`;

let resolvedSecurityId = "";
let resolvedSegment = "NSE_FNO";
let currentDirection = "SELL";
let currentProductType = localStorage.getItem("rm_product_type") || "MARGIN";
let captureMode = null;

document.addEventListener('DOMContentLoaded', () => {
  initPanel();
});

function initPanel() {
  const settingsBtn = document.getElementById('rm-toggle-settings');
  const settingsDrawer = document.getElementById('rm-settings-drawer');
  settingsBtn.onclick = () => {
    settingsDrawer.style.display = settingsDrawer.style.display === 'none' ? 'block' : 'none';
  };

  const savedUser = localStorage.getItem('rm_vps_user');
  if (savedUser) document.getElementById('rm-vps-user').value = savedUser;
  const savedPass = localStorage.getItem('rm_vps_password');
  if (savedPass) document.getElementById('rm-vps-password').value = savedPass;

  document.getElementById('rm-vps-user').oninput = function() { localStorage.setItem('rm_vps_user', this.value); };
  document.getElementById('rm-vps-password').oninput = function() { localStorage.setItem('rm_vps_password', this.value); };
  document.getElementById('rm-contract-search').oninput = function() { resolveContract(this.value); };

  document.getElementById('rm-dir-buy').onclick = () => updateDirectionUI("BUY");
  document.getElementById('rm-dir-sell').onclick = () => updateDirectionUI("SELL");
  document.getElementById('rm-prod-margin').onclick = () => updateProductUI("MARGIN");
  document.getElementById('rm-prod-intraday').onclick = () => updateProductUI("INTRADAY");

  // SERVER-SIDE DIRECT BREAK-EVEN CLICK
  document.getElementById('rm-btn-sl-to-be').onclick = moveSLToBreakEven;

  document.getElementById('rm-btn-lmt').onclick = () => executeOrder('LIMIT');
  document.getElementById('rm-btn-mkt').onclick = () => executeOrder('MARKET');

  // INSTANT ZERO-DIALOG EMERGENCY EXIT ALL
  document.getElementById('rm-btn-exit').onclick = () => {
    showFlashToast("🚨 INSTANT EXIT: Liquidating ALL positions...", "#f85149");
    fetch(`${VPS_URL}/api/exit_all`, {
      method: "POST",
      headers: getAuthHeaders(),
    })
    .then(r => r.json())
    .then(res => showFlashToast("⚡ LIQUIDATED: All open positions closed!", "#f85149"))
    .catch(() => showFlashToast("Exit command failed.", "#f85149"));
  };

  // CAPTURE BUTTONS
  document.getElementById('rm-btn-capture-limit').onclick = () => setCaptureMode('LIMIT');
  document.getElementById('rm-btn-capture-sl').onclick = () => setCaptureMode('SL');
  document.getElementById('rm-btn-capture-tp').onclick = () => setCaptureMode('TP');

  // Listen for messages from content.js (active tab chart symbol & price clicks)
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'ACTIVE_SYMBOL_DETECTED') {
      handleSymbolUpdate(msg.symbol);
    } else if (msg.type === 'CHART_PRICE_CLICKED') {
      handlePriceClickUpdate(msg.price);
    }
  });

  // Query active tab symbol on load
  queryActiveTabSymbol();
  setInterval(queryActiveTabSymbol, 2000);
}

function setCaptureMode(mode) {
  // If clicking the same capture button again, cancel capture mode
  if (captureMode === mode && mode !== null) {
    mode = null;
  }
  
  captureMode = mode;
  if (captureTimeoutId) clearTimeout(captureTimeoutId);

  const limitBtn = document.getElementById('rm-btn-capture-limit');
  const slBtn = document.getElementById('rm-btn-capture-sl');
  const tpBtn = document.getElementById('rm-btn-capture-tp');

  limitBtn.textContent = mode === 'LIMIT' ? 'Click...' : '🎯 Lmt';
  limitBtn.style.background = mode === 'LIMIT' ? '#2ea043' : '#13233c';

  slBtn.textContent = mode === 'SL' ? 'Click...' : '🎯 SL';
  slBtn.style.background = mode === 'SL' ? '#d29922' : '#13233c';

  tpBtn.textContent = mode === 'TP' ? 'Click...' : '🎯 TP';
  tpBtn.style.background = mode === 'TP' ? '#a371f7' : '#13233c';

  if (mode !== null) {
    showFlashToast(`Click chart price to set ${mode}...`, "#1f6feb");
    // Auto-reset after 10 seconds if user doesn't click chart
    captureTimeoutId = setTimeout(() => {
      setCaptureMode(null);
      showFlashToast("Capture mode timed out.", "#30363d");
    }, 10000);
  }

  // Tell content script to capture next click
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0] && tabs[0].id) {
      chrome.tabs.sendMessage(tabs[0].id, { type: 'ARM_CAPTURE_MODE', mode: mode }, () => {
        if (chrome.runtime.lastError) {} // Silence connection error
      });
    }
  });
}

function queryActiveTabSymbol() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (!tabs[0]) return;
    
    // 1. Try parsing symbol directly from active tab title (instant & 100% reliable)
    if (tabs[0].title) {
      const match = tabs[0].title.match(/(NIFTY|SENSEX).*?(\d{3,})\s*[-\s]*\s*(CE|PE|CALL|PUT|C|P)/i);
      if (match) {
        const underlying = match[1].toUpperCase();
        const strike = match[2];
        let type = match[3].toUpperCase();
        if (type === "CALL" || type === "C") type = "CE";
        if (type === "PUT" || type === "P") type = "PE";
        handleSymbolUpdate(`${underlying} ${strike} ${type}`);
        return;
      }
    }

    // 2. Query content script via message passing fallback
    if (tabs[0].id) {
      chrome.tabs.sendMessage(tabs[0].id, { type: 'QUERY_ACTIVE_SYMBOL' }, (res) => {
        if (chrome.runtime.lastError) return; // Silently handle disconnected content script
        if (res && res.symbol) handleSymbolUpdate(res.symbol);
      });
    }
  });
}

function handleSymbolUpdate(symbol) {
  if (!symbol) return;
  const searchInput = document.getElementById('rm-contract-search');
  if (searchInput && searchInput.value !== symbol) {
    searchInput.value = symbol;
    resolveContract(symbol);
  }

  const qtyInput = document.getElementById('rm-qty');
  if (qtyInput) {
    const upperSym = symbol.toUpperCase();
    if (upperSym.includes("SENSEX")) {
      qtyInput.step = "20";
      qtyInput.value = "20";
    } else if (upperSym.includes("NIFTY")) {
      qtyInput.step = "65";
      qtyInput.value = "65";
    }
  }
}

function updateDirectionUI(dir) {
  currentDirection = dir;
  const buyBtn = document.getElementById('rm-dir-buy');
  const sellBtn = document.getElementById('rm-dir-sell');
  const lmtBtn = document.getElementById('rm-btn-lmt');
  const mktBtn = document.getElementById('rm-btn-mkt');

  if (dir === "BUY") {
    buyBtn.className = "btn-toggle active-buy";
    sellBtn.className = "btn-toggle";
    lmtBtn.textContent = "⚡ BUY TRIGGER LIMIT";
    lmtBtn.className = "btn-action btn-buy";
    mktBtn.textContent = "⚡ BUY MARKET";
    mktBtn.className = "btn-action btn-buy";
  } else {
    buyBtn.className = "btn-toggle";
    sellBtn.className = "btn-toggle active-sell";
    lmtBtn.textContent = "⚡ SELL TRIGGER LIMIT";
    lmtBtn.className = "btn-action btn-sell";
    mktBtn.textContent = "⚡ SELL MARKET";
    mktBtn.className = "btn-action btn-sell";
  }
}

function updateProductUI(type) {
  currentProductType = type;
  localStorage.setItem("rm_product_type", type);
  const marginBtn = document.getElementById('rm-prod-margin');
  const intradayBtn = document.getElementById('rm-prod-intraday');

  if (type === "MARGIN") {
    marginBtn.className = "btn-toggle active-margin";
    intradayBtn.className = "btn-toggle";
    showFlashToast("Product Mode: NORMAL (MARGIN)");
  } else {
    marginBtn.className = "btn-toggle";
    intradayBtn.className = "btn-toggle active-intraday";
    showFlashToast("Product Mode: INTRADAY (MIS)");
  }
}

function moveSLToBreakEven() {
  const offset = parseFloat(document.getElementById('rm-be-offset').value || 0.50) || 0.50;
  showFlashToast("Submitting Break-Even SL to VPS...", "#30363d");

  fetch(`${VPS_URL}/api/order/move_sl_to_be`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({
      security_id: resolvedSecurityId,
      be_offset: offset
    })
  })
  .then(r => r.json())
  .then(res => {
    if (res.status === 'success' || res.status === 'SUCCESS') {
      if (res.be_price) document.getElementById('rm-sl-price').value = res.be_price.toFixed(2);
      showFlashToast(`SUCCESS: Native SL to BE placed @ ₹${res.be_price.toFixed(2)} (Entry: ₹${res.entry_price.toFixed(2)})`, "#2ea043");
    } else {
      showFlashToast(`BE SL Result: ${res.message || "Rejected"}`, "#f85149");
    }
  })
  .catch(() => showFlashToast("Failed to reach VPS BE endpoint.", "#f85149"));
}


function handlePriceClickUpdate(price) {
  const roundedPrice = Math.round(parseFloat(price) / 0.05) * 0.05;
  if (isNaN(roundedPrice)) return;

  if (captureMode === 'LIMIT') {
    document.getElementById('rm-limit-price').value = roundedPrice.toFixed(2);
    showFlashToast(`Trigger Price set to: ₹${roundedPrice.toFixed(2)}`, "#3fb950");
  } else if (captureMode === 'SL') {
    document.getElementById('rm-sl-price').value = roundedPrice.toFixed(2);
    showFlashToast(`Stop Loss set to: ₹${roundedPrice.toFixed(2)}`, "#f85149");

    if (resolvedSecurityId) {
      fetch(`${VPS_URL}/api/order/submit_sl_1click`, {
        method: "POST",
        headers: getAuthHeaders(),
        body: JSON.stringify({
          security_id: resolvedSecurityId,
          exchange_segment: resolvedSegment,
          sl_price: roundedPrice,
          quantity: parseInt(document.getElementById('rm-qty').value) || 0
        })
      })
      .then(r => r.json())
      .then(res => {
        if (res.status === 'success' || res.status === 'SUCCESS') {
          showFlashToast(`SUCCESS: Native SL placed @ ₹${roundedPrice.toFixed(2)}`, "#2ea043");
        }
      });
    }
  } else if (captureMode === 'TP') {
    document.getElementById('rm-tp-price').value = roundedPrice.toFixed(2);
    showFlashToast(`Take Profit set to: ₹${roundedPrice.toFixed(2)}`, "#a371f7");

    if (resolvedSecurityId) {
      fetch(`${VPS_URL}/api/order/submit_tp_1click`, {
        method: "POST",
        headers: getAuthHeaders(),
        body: JSON.stringify({
          security_id: resolvedSecurityId,
          exchange_segment: resolvedSegment,
          tp_price: roundedPrice,
          quantity: parseInt(document.getElementById('rm-qty').value) || 0
        })
      })
      .then(r => r.json())
      .then(res => {
        if (res.status === 'success' || res.status === 'SUCCESS') {
          showFlashToast(`SUCCESS: Native TP placed @ ₹${roundedPrice.toFixed(2)}`, "#2ea043");
        }
      });
    }
  }
  setCaptureMode(null);
}

function resolveContract(query) {
  if (!query || query.length < 5) return;
  const statusEl = document.getElementById('rm-contract-status');
  statusEl.style.color = "#8b949e";
  statusEl.textContent = "Resolving...";

  fetch(`${VPS_URL}/api/instruments/search?q=${encodeURIComponent(query)}&limit=1`, {
    headers: getAuthHeaders()
  })
  .then(r => r.json())
  .then(results => {
    if (results && results.length > 0) {
      const inst = results[0];
      resolvedSecurityId = inst.security_id;
      resolvedSegment = inst.exchange_segment || "NSE_FNO";
      statusEl.style.color = "#3fb950";
      statusEl.textContent = `Resolved: ${inst.trading_symbol} (ID: ${inst.security_id})`;
    } else {
      resolvedSecurityId = "";
      statusEl.style.color = "#f85149";
      statusEl.textContent = "Contract not found.";
    }
  })
  .catch(() => {
    statusEl.style.color = "#f85149";
    statusEl.textContent = "VPS connection failed.";
  });
}

function executeOrder(type) {
  if (!resolvedSecurityId) {
    alert("No resolved contract.");
    return;
  }
  const qty = parseInt(document.getElementById('rm-qty').value);
  const triggerPrice = parseFloat(document.getElementById('rm-limit-price').value || 0);
  const slippage = parseFloat(document.getElementById('rm-slippage').value || 0);
  const slPrice = parseFloat(document.getElementById('rm-sl-price').value || 0);
  const tpPrice = parseFloat(document.getElementById('rm-tp-price').value || 0);

  let orderType = type;
  let limitPrice = triggerPrice;

  if (type === 'LIMIT' && triggerPrice > 0) {
    orderType = "STOP_LOSS_LIMIT";
    limitPrice = currentDirection === 'BUY' ? triggerPrice + slippage : Math.max(0.05, triggerPrice - slippage);
    limitPrice = Math.round(limitPrice / 0.05) * 0.05;
    showFlashToast(`Submitting TRIGGER ENTRY (${currentProductType}): Trigger @ ₹${triggerPrice.toFixed(2)}...`, "#30363d");
  } else {
    showFlashToast(`Submitting ${type} order (${currentProductType})...`, "#30363d");
  }

  fetch(`${VPS_URL}/api/order/place`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({
      security_id: resolvedSecurityId,
      exchange_segment: resolvedSegment,
      quantity: qty,
      transaction_type: currentDirection,
      order_type: orderType,
      product_type: currentProductType,
      price: limitPrice,
      trigger_price: triggerPrice,
      sl_price: slPrice,
      tp_price: tpPrice
    })
  })
  .then(r => r.json())
  .then(res => {
    if (res.status === 'success' || res.status === 'SUCCESS') {
      showFlashToast(`SUCCESS: ${orderType} Order placed! ID: ${res.data ? res.data.orderId : res.orderId}`, "#2ea043");
    } else {
      showFlashToast(`FAILED: ${res.reason || res.message || "Rejected"}`, "#f85149");
    }
  })
  .catch(() => showFlashToast("Could not reach VPS API.", "#f85149"));
}

function getAuthHeaders() {
  const user = document.getElementById('rm-vps-user').value || "trader";
  const pass = document.getElementById('rm-vps-password').value;
  const headers = { "Content-Type": "application/json" };
  if (pass) {
    headers["Authorization"] = "Basic " + btoa(user + ":" + pass);
  }
  return headers;
}

function showFlashToast(msg, bg="#21262d") {
  const toast = document.getElementById('rm-flash-toast');
  if (!toast) return;
  toast.style.background = bg;
  toast.style.display = 'block';
  toast.textContent = msg;
  setTimeout(() => { toast.style.display = 'none'; }, 3500);
}
