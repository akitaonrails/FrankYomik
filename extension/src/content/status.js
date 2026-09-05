(function frankStatusModule() {
  'use strict';

  function runtimeAlive() {
    try {
      return Boolean(chrome.runtime?.id);
    } catch {
      return false;
    }
  }

  if (window.FrankStatus) {
    if (window.FrankStatus.alive?.()) return;
    window.FrankStatus.destroy?.();
  }

  // Deliberately small and quiet: the page belongs to the reader, and knowing
  // whether this one is peekable yet should not cost more than a glance.
  const SIZE_PX = 9;
  const MARGIN_PX = 14;
  const READY_FADE_MS = 2200;

  const COLORS = {
    working: 'rgba(255, 183, 77, 0.85)',   // amber: captured, being translated
    ready: 'rgba(129, 199, 132, 0.85)',    // green: hold to peek
    failed: 'rgba(229, 115, 115, 0.85)',   // red: the server refused it
  };

  let dot = null;
  let fadeTimer = null;
  let current = 'idle';

  function element() {
    if (dot?.isConnected) return dot;
    dot = document.createElement('div');
    dot.id = '__frankStatusDot';
    dot.style.cssText = [
      'position:fixed',
      `right:${MARGIN_PX}px`,
      `bottom:${MARGIN_PX}px`,
      `width:${SIZE_PX}px`,
      `height:${SIZE_PX}px`,
      'border-radius:50%',
      'z-index:2147483645',
      'pointer-events:none',
      'opacity:0',
      'transition:opacity 400ms ease, background-color 250ms ease',
      'box-shadow:0 0 0 1px rgba(0,0,0,0.25)',
    ].join(';');
    (document.body || document.documentElement).appendChild(dot);
    return dot;
  }

  /// Where this page is: 'idle', 'working', 'ready' or 'failed'.
  ///
  /// A page being translated pulses; one that is ready settles to a faint dot
  /// and stays there, because "can I peek yet" is a question the reader asks
  /// repeatedly.
  function set(next) {
    if (next === current) return;
    current = next;
    window.clearTimeout(fadeTimer);

    if (next === 'idle' || !COLORS[next]) {
      if (dot) dot.style.opacity = '0';
      return;
    }

    const el = element();
    el.style.backgroundColor = COLORS[next];
    el.style.animation = next === 'working' ? 'frankStatusPulse 1.4s ease-in-out infinite' : 'none';
    el.style.opacity = next === 'working' ? '0.75' : '0.9';

    if (next === 'ready' || next === 'failed') {
      // Settle rather than disappear: still legible, no longer attention-seeking.
      fadeTimer = window.setTimeout(() => {
        if (dot && current === next) dot.style.opacity = '0.35';
      }, READY_FADE_MS);
    }
  }

  const style = document.createElement('style');
  style.id = '__frankStatusStyle';
  style.textContent =
    '@keyframes frankStatusPulse{0%,100%{opacity:0.35}50%{opacity:0.85}}';
  (document.head || document.documentElement)?.appendChild(style);

  window.FrankStatus = {
    alive: runtimeAlive,
    set,
    state: () => current,
    destroy() {
      window.clearTimeout(fadeTimer);
      dot?.remove?.();
      style?.remove?.();
      dot = null;
      current = 'idle';
      delete window.FrankStatus;
    },
  };
})();
