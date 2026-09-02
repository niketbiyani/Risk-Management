(function() {
  'use strict';

  let activePane = null;
  let armedCaptureMode = null;

  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'ARM_CAPTURE_MODE_MAIN') {
      armedCaptureMode = e.data.mode;
    }
  });

  function getActiveChartWidget() {
    let win = window;
    for (let i = 0; i < 3; i++) {
      try {
        if (win && win.chartWidgetCollection) {
          const coll = win.chartWidgetCollection;
          const activeWidget = typeof coll.activeChartWidget.value === "function"
            ? coll.activeChartWidget.value()
            : coll.activeChartWidget._value;
          if (activeWidget) return activeWidget;
        }
      } catch (e) {}
      if (win && win.parent && win.parent !== win) win = win.parent;
      else break;
    }

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

  function scanChartFromClick(clickEvent) {
    if (!clickEvent || !clickEvent.target) return null;
    let curr = clickEvent.target;
    let chartBox = null;
    while (curr && curr !== document && curr !== document.body) {
      if (curr.classList && (curr.classList.contains('chart-container') || curr.classList.contains('widget-container') || curr.tagName === 'TD' || curr.tagName === 'TR')) {
        chartBox = curr;
        break;
      }
      curr = curr.parentElement;
    }
    const root = chartBox || document;
    const legendSelectors = ['.js-button-text','.noWrapWrapper-l31H9iuA','.pane-legend-line','[class*="legend-"]','[class*="title-"]'];

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
    return null;
  }

  function scanActiveSymbol() {
    const activeWidget = getActiveChartWidget();
    if (!activeWidget) return null;

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

    const searchRoots = activeContainer ? [activeContainer] : [doc, document];
    const legendSelectors = ['.js-button-text','.noWrapWrapper-l31H9iuA','.pane-legend-line','[class*="legend-"]','[class*="title-"]'];

    for (const root of searchRoots) {
      if (!root) continue;
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

  function calculateClickedPrice(clickEvent) {
    try {
      const activeWidget = getActiveChartWidget();
      if (!activeWidget) return null;

      const model = activeWidget.model();
      if (!model) return null;

      const mModel = model.m_model || model;
      if (!mModel) return null;

      const mainSeries = mModel.mainSeries();
      const firstVal = mainSeries.firstValue();
      const priceScale = mainSeries.priceScale();
      if (!priceScale || !firstVal) return null;

      const targetPane = activePane || clickEvent.target.closest('tr, td.chart-markup-table, .chart-container');
      if (!targetPane) return null;

      const rect = targetPane.getBoundingClientRect();
      const y = clickEvent.clientY - rect.top;

      return priceScale.coordinateToPrice(y, firstVal);
    } catch (e) {
      return null;
    }
  }

  function attachListeners() {
    document.addEventListener('mousedown', function(e) {
      const rowPane = e.target.closest('tr, .chart-container, [class*="widget-"], td.chart-markup-table');
      if (rowPane) activePane = rowPane;

      const clickedSym = scanChartFromClick(e);
      if (clickedSym) {
        window.postMessage({ type: 'ACTIVE_SYMBOL_DETECTED', symbol: clickedSym }, '*');
      } else {
        setTimeout(() => {
          const sym = scanActiveSymbol();
          if (sym) window.postMessage({ type: 'ACTIVE_SYMBOL_DETECTED', symbol: sym }, '*');
        }, 50);
      }
    }, true);

    document.addEventListener('click', function(e) {
      if (armedCaptureMode) {
        const computedPrice = calculateClickedPrice(e);
        if (computedPrice) {
          window.postMessage({ type: 'CHART_PRICE_CLICKED', price: computedPrice }, '*');
        }
        armedCaptureMode = null;
      }
    }, true);
  }

  setInterval(() => {
    const sym = scanActiveSymbol();
    if (sym) {
      window.postMessage({ type: 'ACTIVE_SYMBOL_DETECTED', symbol: sym }, '*');
    }
  }, 1500);

  attachListeners();
})();
