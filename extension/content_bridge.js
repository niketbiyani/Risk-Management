(function() {
  'use strict';

  // Listen for window.postMessage from content_main.js and forward to extension sidepanel
  window.addEventListener('message', (e) => {
    if (!e.data || !e.data.type) return;
    if (e.data.type === 'ACTIVE_SYMBOL_DETECTED' || e.data.type === 'CHART_PRICE_CLICKED') {
      chrome.runtime.sendMessage(e.data).catch(() => {});
    }
  });

  // Listen for messages from sidepanel.js and forward to content_main.js
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'ARM_CAPTURE_MODE') {
      window.postMessage({ type: 'ARM_CAPTURE_MODE_MAIN', mode: msg.mode }, '*');
    }
  });
})();
