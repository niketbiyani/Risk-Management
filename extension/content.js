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

  let doc = document;
  let container = null;

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
          container = activeWidget._container || (activeWidget._id ? doc.getElementById(activeWidget._id) : null);
          break;
        }
      }
    } catch(e){}
  }

  const searchRoot = container || doc;
  const legendSelectors = ['.js-button-text','.noWrapWrapper-l31H9iuA','.pane-legend-line','[class*="legend-"]','[class*="title-"]'];

  for (const selector of legendSelectors) {
    const elements = searchRoot.querySelectorAll(selector);
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
  return null;
}

function attachIframeListeners() {
  const iframes = document.querySelectorAll('iframe');
  iframes.forEach(iframe => {
    try {
      if (iframe.contentDocument && !iframe.dataset.rmExtListenerAttached) {
        iframe.dataset.rmExtListenerAttached = "true";

        iframe.contentDocument.addEventListener('mousedown', function(e) {
          const rowPane = e.target.closest('tr, .chart-container, [class*="widget-"], td.chart-markup-table');
          if (rowPane) activePane = rowPane;
        }, true);

        iframe.contentDocument.addEventListener('click', function(e) {
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
