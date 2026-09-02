let activePane = null;
let armedCaptureMode = null;

// Message listener from sidepanel.js
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'QUERY_ACTIVE_SYMBOL') {
    const sym = scanActiveSymbol();
    sendResponse({ symbol: sym });
  } else if (msg.type === 'ARM_CAPTURE_MODE') {
    armedCaptureMode = msg.mode;
  }
});

// Continuously scan active chart symbol across frames and broadcast to sidepanel
setInterval(() => {
  const sym = scanActiveSymbol();
  if (sym) {
    chrome.runtime.sendMessage({ type: 'ACTIVE_SYMBOL_DETECTED', symbol: sym }).catch(() => {});
  }
}, 1500);

function getActiveChartWidget() {
  const iframes = document.querySelectorAll('iframe');
  for (const iframe of iframes) {
    try {
      if (iframe.contentWindow && iframe.contentWindow.chartWidgetCollection) {
        const coll = iframe.contentWindow.chartWidgetCollection;
        const activeWidget = typeof coll.activeChartWidget.value === "function"
          ? coll.activeChartWidget.value()
          : coll.activeChartWidget._value;
        if (activeWidget) return activeWidget;
      }
    } catch (e) {}
  }
  return null;
}

function scanActiveSymbol() {
  const activeWidget = getActiveChartWidget();
  if (!activeWidget) return null;

  // 1. Try reading symbol directly from TradingView's active mainSeries model
  try {
    if (activeWidget.model && activeWidget.model()) {
      const mModel = activeWidget.model().m_model || activeWidget.model();
      if (mModel && mModel.mainSeries) {
        const mainSeries = mModel.mainSeries();
        const rawSym = mainSeries ? (typeof mainSeries.symbol === 'function' ? mainSeries.symbol() : mainSeries._symbol) : null;
        if (rawSym && typeof rawSym === 'string') {
          const match = rawSym.match(/(NIFTY|SENSEX).*?(\d{3,})\s*[-\s]*\s*(CE|PE|CALL|PUT|C|P)/i);
          if (match) {
            const underlying = match[1].toUpperCase();
            const strike = match[2];
            let type = match[3].toUpperCase();
            if (type === "CALL" || type === "C") type = "CE";
            if (type === "PUT" || type === "P") type = "PE";
            return `${underlying} ${strike} ${type}`;
          }
        }
      }
    }
  } catch(e) {}

  // 2. Locate the specific DOM container for ONLY the active chart widget
  let doc = document;
  let activeContainer = activeWidget._container || null;

  const iframes = document.querySelectorAll('iframe');
  for (const iframe of iframes) {
    try {
      if (iframe.contentWindow && iframe.contentWindow.chartWidgetCollection) {
        const coll = iframe.contentWindow.chartWidgetCollection;
        const widget = typeof coll.activeChartWidget.value === "function"
          ? coll.activeChartWidget.value()
          : coll.activeChartWidget._value;
        if (widget === activeWidget) {
          doc = iframe.contentDocument;
          if (!activeContainer && activeWidget._id) {
            activeContainer = doc.getElementById(activeWidget._id);
          }
          break;
        }
      }
    } catch(e){}
  }

  // Strictly search ONLY within the active chart's container (do NOT fall back to entire doc)
  const searchRoots = activeContainer ? [activeContainer] : [];
  const legendSelectors = ['.js-button-text','.noWrapWrapper-l31H9iuA','.pane-legend-line','[class*="legend-"]','[class*="title-"]'];

  for (const root of searchRoots) {
    for (const selector of legendSelectors) {
      const elements = root.querySelectorAll(selector);
      for (const el of elements) {
        if (el.textContent) {
          const text = el.textContent.trim();
          const match = text.match(/(NIFTY|SENSEX).*?(\d{3,})\s*[-\s]*\s*(CE|PE|CALL|PUT|C|P)/i);
          if (match) {
            const underlying = match[1].toUpperCase();
            const strike = match[2];
            let type = match[3].toUpperCase();
            if (type === "CALL" || type === "C") type = "CE";
            if (type === "PUT" || type === "P") type = "PE";
            return `${underlying} ${strike} ${type}`;
          }
        }
      }
    }
  }
  return null;
}

function attachIframeListeners() {
  const iframes = document.querySelectorAll('iframe');
  iframes.forEach(iframe => {
    try {
      if (iframe.contentDocument && !iframe.dataset.rmExtListenerAttached) {
        iframe.dataset.rmExtListenerAttached = "true";

        // Listen for pane focus click in multi-chart layouts
        const handlePaneClick = (e) => {
          const rowPane = e.target.closest('tr, .chart-container, [class*="widget-"], td.chart-markup-table');
          if (rowPane) activePane = rowPane;

          // Broadcast focused chart symbol on click
          setTimeout(() => {
            const sym = scanActiveSymbol();
            if (sym) {
              chrome.runtime.sendMessage({ type: 'ACTIVE_SYMBOL_DETECTED', symbol: sym }).catch(() => {});
            }
          }, 50);
        };

        iframe.contentDocument.addEventListener('mousedown', handlePaneClick, true);
        iframe.contentDocument.addEventListener('click', function(e) {
          handlePaneClick(e);
          if (armedCaptureMode) {
            const computedPrice = calculateClickedPrice(iframe.contentWindow, e);
            if (computedPrice) {
              chrome.runtime.sendMessage({ type: 'CHART_PRICE_CLICKED', price: computedPrice });
            }
            armedCaptureMode = null;
          }
        }, true);
      }
    } catch (e) {}
  });
}

function calculateClickedPrice(iframeWin, clickEvent) {
  try {
    const coll = iframeWin.chartWidgetCollection;
    if (!coll) return null;

    const activeWidget = typeof coll.activeChartWidget.value === "function"
      ? coll.activeChartWidget.value()
      : coll.activeChartWidget._value;

    if (!activeWidget) return null;

    const model = activeWidget.model();
    if (!model) return null;

    const mModel = model.m_model;
    if (!mModel) return null;

    const mainSeries = mModel.mainSeries();
    const firstVal = mainSeries.firstValue();
    const priceScale = mainSeries.priceScale();
    if (!priceScale || !firstVal) return null;

    const targetPane = activePane || clickEvent.target.closest('tr, td.chart-markup-table');
    if (!targetPane) return null;

    const rect = targetPane.getBoundingClientRect();
    const y = clickEvent.clientY - rect.top;

    return priceScale.coordinateToPrice(y, firstVal);
  } catch (e) {
    return null;
  }
}

setInterval(attachIframeListeners, 2000);
