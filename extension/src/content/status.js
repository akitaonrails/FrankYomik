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

  // Quiet, but not invisible. The first version was a 9px dot at a third
  // opacity in a corner: correct, and never once noticed. A state change now
  // says what it is in words and then settles back to the dot.
  const SIZE_PX = 12;
  const MARGIN_PX = 16;
  const LABEL_MS = 2600;

  const STATES = {
    capturing:   { color: 'rgba(255, 213, 79, 0.95)',  label: 'capturing page' },
    queued:      { color: 'rgba(255, 183, 77, 0.95)',  label: 'translating…' },
    ready:       { color: 'rgba(129, 199, 132, 0.95)', label: 'ready — hold to peek' },
    failed:      { color: 'rgba(229, 115, 115, 0.95)', label: 'could not translate' },
  };
  // The older names, so a caller need not change to keep working.
  const ALIASES = { working: 'queued' };

  let dot = null;
  let label = null;
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
      'transition:opacity 300ms ease, background-color 200ms ease',
      'box-shadow:0 0 0 1px rgba(0,0,0,0.35), 0 0 10px rgba(0,0,0,0.5)',
    ].join(';');
    (document.body || document.documentElement).appendChild(dot);
    return dot;
  }

  /// A few words beside the dot when the state changes, then gone again.
  function labelElement() {
    if (label?.isConnected) return label;
    label = document.createElement('div');
    label.id = '__frankStatusLabel';
    label.style.cssText = [
      'position:fixed',
      `right:${MARGIN_PX + SIZE_PX + 8}px`,
      `bottom:${MARGIN_PX - 3}px`,
      'z-index:2147483645',
      'pointer-events:none',
      'opacity:0',
      'transition:opacity 300ms ease',
      'font:500 12px/1.5 system-ui, sans-serif',
      'color:rgba(255,255,255,0.92)',
      'background:rgba(20,20,20,0.82)',
      'padding:2px 8px',
      'border-radius:10px',
      'white-space:nowrap',
      'box-shadow:0 1px 6px rgba(0,0,0,0.45)',
    ].join(';');
    (document.body || document.documentElement).appendChild(label);
    return label;
  }

  /// Where this page is: 'capturing', 'queued', 'ready', 'failed' or 'idle'.
  ///
  /// The dot stays for as long as the state holds; the words appear on the
  /// change and fade, so the corner does not become a permanent caption.
  function set(next) {
    const name = ALIASES[next] ?? next;
    if (name === current) return;
    current = name;
    window.clearTimeout(fadeTimer);

    const state = STATES[name];
    if (!state) {
      // The pulse animates opacity, and an animation outranks an inline style
      // for the property it animates: without stopping it first, the dot
      // cannot be hidden at all.
      if (dot) {
        dot.style.animation = 'none';
        dot.style.opacity = '0';
      }
      if (label) label.style.opacity = '0';
      return;
    }

    const el = element();
    el.style.backgroundColor = state.color;
    el.style.animation = name === 'ready' || name === 'failed'
      ? 'none'
      : 'frankStatusPulse 1.4s ease-in-out infinite';
    el.style.opacity = '1';

    const text = labelElement();
    text.textContent = state.label;
    text.style.opacity = '1';

    fadeTimer = window.setTimeout(() => {
      if (current !== name) return;
      text.style.opacity = '0';
      // The dot stays, dimmer, so the state is still there to glance at.
      if (name === 'ready' || name === 'failed') el.style.opacity = '0.55';
    }, LABEL_MS);
  }

  const style = document.createElement('style');
  style.id = '__frankStatusStyle';
  style.textContent =
    '@keyframes frankStatusPulse{0%,100%{opacity:0.55}50%{opacity:1}}';
  (document.head || document.documentElement)?.appendChild(style);

  window.FrankStatus = {
    alive: runtimeAlive,
    set,
    state: () => current,
    destroy() {
      window.clearTimeout(fadeTimer);
      dot?.remove?.();
      label?.remove?.();
      style?.remove?.();
      dot = null;
      label = null;
      current = 'idle';
      delete window.FrankStatus;
    },
  };
})();
