// Minimal DOM stub for exercising the content scripts that manipulate the
// reader's page. It implements only the shapes those scripts actually use:
// element rects, dataset/style bags, the handful of selectors they query, and
// pointer/mouse event dispatch.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const contentDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '../../src/content');

export const VIEWPORT = Object.freeze({ width: 1000, height: 800 });

export function makeElement(tag) {
  const classSet = new Set();
  return {
    tagName: String(tag).toUpperCase(),
    dataset: {},
    style: { cssText: '' },
    className: '',
    src: '',
    id: '',
    isConnected: true,
    children: [],
    classList: {
      add: (c) => classSet.add(c),
      remove: (c) => classSet.delete(c),
      contains: (c) => classSet.has(c),
    },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {},
    dispatched: [],
    dispatchEvent(event) {
      this.dispatched.push(event);
      // Capture phase starts at the window, so document-level listeners see
      // a dispatched event exactly as they see a real one.
      this._deliver?.(event.type, event);
      return true;
    },
    contains(other) { return other === this; },
    // Enough of a canvas for capture paths to bail out cleanly.
    getContext: () => null,
    getBoundingClientRect() {
      const r = this._rect || { left: 0, top: 0, width: 0, height: 0 };
      return { ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height };
    },
  };
}

export function makeImage(rect, { src = 'blob:page', className = '' } = {}) {
  const el = makeElement('img');
  el._rect = rect;
  el.src = src;
  el.className = className;
  el.naturalWidth = Math.round(rect.width);
  el.naturalHeight = Math.round(rect.height);
  el.decode = async () => {};
  return el;
}

function matchesSelector(el, selector) {
  // Comma-separated groups match if any part does, as in the real thing.
  if (selector.includes(',')) {
    return selector.split(',').some((part) => matchesSelector(el, part.trim()));
  }
  const classContains = /^\[class\*="(.+)"\]$/.exec(selector);
  if (classContains) return String(el.className || '').includes(classContains[1]);
  if (selector.startsWith('.')) return String(el.className || '').split(/\s+/).includes(selector.slice(1));
  if (selector === 'img') return el.tagName === 'IMG';
  if (selector === 'img.toon_image') return el.tagName === 'IMG' && el.className.includes('toon_image');
  if (selector === 'img[data-frank-lens-src]') return el.tagName === 'IMG' && !!el.dataset.frankLensSrc;
  if (selector === 'img[data-frank-translated="true"]') {
    return el.tagName === 'IMG' && el.dataset.frankTranslated === 'true';
  }
  return false;
}

/// Loads content-script modules into one sandboxed window, in manifest order.
export function loadContentScripts(scripts, images, options = {}) {
  const revoked = [];
  const listeners = new Map();
  let urlCounter = 0;

  const queryAll = (selector) => images.filter((el) => matchesSelector(el, selector));

  const body = makeElement('body');
  body.querySelectorAll = queryAll;

  const document = {
    body: options.bareDocument ? null : body,
    head: options.bareDocument ? null : makeElement('head'),
    documentElement: makeElement('html'),
    createElement: makeElement,
    querySelector: () => options.readerRoot ?? null,
    querySelectorAll: queryAll,
    elementFromPoint(x, y) {
      const hit = images.filter((el) => {
        const r = el.getBoundingClientRect();
        return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
      });
      return hit[hit.length - 1] || null;
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
  };

  const selection = {
    isCollapsed: false,
    cleared: 0,
    removeAllRanges() { this.cleared += 1; this.isCollapsed = true; },
  };

  const messageListeners = [];

  const window = {
    location: { href: options.href ?? 'https://read.amazon.co.jp/?asin=B0ABCDEFGH' },
    setInterval: () => 0,
    clearInterval: () => {},
    innerWidth: VIEWPORT.width,
    innerHeight: VIEWPORT.height,
    devicePixelRatio: 1,
    getSelection: () => selection,
    document,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    addEventListener: (type, fn) => document.addEventListener(type, fn),
    setTimeout,
    clearTimeout,
  };
  window.window = window;

  const sandbox = {
    window,
    location: window.location,
    document,
    // A clock the test can move, so time-based gates are deterministic.
    Date: options.clock
      ? Object.assign(Object.create(Date), { now: () => options.clock.now })
      : Date,
    console,
    setTimeout,
    clearTimeout,
    // Enough of an Image for the decode warm-up, including the load event the
    // lens uses to learn a render's shape.
    Image: class {
      constructor() {
        this._handlers = [];
        this.naturalWidth = options.renderNatural?.width ?? 400;
        this.naturalHeight = options.renderNatural?.height ?? 600;
      }
      addEventListener(type, fn) { if (type === 'load') this._handlers.push(fn); }
      set src(value) {
        this._src = value;
        for (const fn of this._handlers) fn();
      }
      get src() { return this._src; }
    },
    fetch: async () => ({ blob: async () => ({ type: 'image/png' }) }),
    URL: {
      createObjectURL: () => `blob:frank-${++urlCounter}`,
      revokeObjectURL: (url) => revoked.push(url),
    },
    chrome: {
      runtime: {
        lastError: null,
        sendMessage: () => Promise.resolve(),
        onMessage: { addListener: (fn) => messageListeners.push(fn) },
      },
    },
    MouseEvent: class {
      constructor(type, init = {}) {
        this.type = type;
        Object.assign(this, init);
      }
    },
    PointerEvent: class {
      constructor(type, init = {}) {
        this.type = type;
        Object.assign(this, init);
      }
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);

  for (const script of scripts) {
    vm.runInContext(fs.readFileSync(path.join(contentDir, script), 'utf8'), sandbox);
  }

  const fire = (type, event) => {
    for (const fn of listeners.get(type) || []) fn(event);
  };
  for (const el of [...images, body, document.documentElement]) el._deliver = fire;
  const pointer = (type, x, y, extra = {}) => ({
    type,
    clientX: x,
    clientY: y,
    pointerId: 1,
    pointerType: 'mouse',
    button: 0,
    cancelable: true,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
    stopImmediatePropagation() { this.immediatePropagationStopped = true; },
    ...extra,
  });

  /// Deliver a message the way the service worker would.
  const sendToContent = (message) => {
    for (const listener of messageListeners) listener(message, {}, () => {});
  };

  return { sandbox, window, document, fire, pointer, revoked, selection, sendToContent };
}

export const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export const lensElement = (document) => document.body.children.find((c) => c.id === '__frankLens');
export const DATA_URL = 'data:image/png;base64,iVBORw0KGgo=';
