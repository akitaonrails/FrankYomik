(function frankBootstrap() {
  'use strict';

  if (window.__frankYomikBootstrapLoaded) return;
  window.__frankYomikBootstrapLoaded = true;

  const RETRY_MS = 5000;
  let retryTimer = null;

  const host = location.hostname.toLowerCase();
  const site = host === 'read.amazon.co.jp' || host === 'read.kindle.co.jp'
    ? 'kindle'
    : host === 'comic.naver.com' || host === 'm.comic.naver.com'
      ? 'webtoon'
      : null;

  if (!site) return;

  requestSettings();

  function requestSettings() {
    chrome.runtime.sendMessage({ type: 'GET_SETTINGS' }, (response) => {
      if (chrome.runtime.lastError || !response?.ok) {
        scheduleRetry();
        return;
      }

      handleSettings(response.settings || {});
    });
  }

  function handleSettings(settings) {
    // Reading mode applies even before a strategy starts, so a mode change
    // while the reader is open takes effect without a reload.
    applyReaderPreferences(settings);
    if (!settings.configured) {
      console.info('[Frank] extension is installed but not configured');
      scheduleRetry();
      return;
    }
    if (site === 'kindle' && settings.kindleEnabled === false) {
      scheduleRetry();
      return;
    }
    if (site === 'webtoon' && settings.webtoonEnabled === false) {
      scheduleRetry();
      return;
    }
    if (site === 'kindle' && window.FrankKindle) {
      window.FrankKindle.start(settings);
      report('info', 'Kindle strategy start requested');
      return;
    }
    if (site === 'webtoon' && window.FrankWebtoon) {
      window.FrankWebtoon.start(settings);
      report('info', 'Webtoon strategy start requested');
      return;
    }
    console.info(`[Frank] ${site} extension bootstrap ready; strategy not loaded yet`);
    scheduleRetry();
  }

  function applyReaderPreferences(settings) {
    window.FrankOverlay?.applyReaderPreferences({
      readerMode: settings.readerMode,
      lensZoom: settings.lensZoom,
    });
  }

  // Settings live in extension storage; watching it keeps the lens in step
  // with the options page without another message round trip.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'sync' && area !== 'local') return;
    if (!Object.prototype.hasOwnProperty.call(changes, 'frankSettings')) return;
    chrome.runtime.sendMessage({ type: 'GET_SETTINGS' }, (response) => {
      if (chrome.runtime.lastError || !response?.ok) return;
      applyReaderPreferences(response.settings || {});
    });
  });

  function scheduleRetry() {
    if (retryTimer) return;
    retryTimer = window.setTimeout(() => {
      retryTimer = null;
      requestSettings();
    }, RETRY_MS);
  }

  function report(level, message) {
    chrome.runtime.sendMessage({ type: 'REPORT_EVENT', site, level, message }).catch(() => {});
  }
})();
