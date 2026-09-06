// A real Chromium, driven over the DevTools protocol with no dependencies.
//
// Some of this extension's behaviour cannot be judged outside a browser. The
// clearest case: reducing a page image to a 16-pixel signature. A DOM stub
// cannot model how Chromium samples during a large downscale, and getting that
// wrong made a correct render look like a different page for hours.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';

const CHROMIUM_CANDIDATES = [
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
];

export function chromiumPath() {
  return CHROMIUM_CANDIDATES.find(existsSync) ?? null;
}

/// Run one function inside a real browser page and return its result.
///
/// The function is serialised, so it must be self-contained: no closure over
/// anything in this file. Anything it needs is passed through `args`.
export async function inBrowser(fn, args = {}, { timeoutMs = 30_000 } = {}) {
  const binary = chromiumPath();
  if (!binary) throw new Error('no chromium available');

  const port = 9200 + Math.floor(Math.random() * 500);
  const chrome = spawn(binary, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--mute-audio',
    '--disable-dev-shm-usage', `--remote-debugging-port=${port}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });

  try {
    const browserUrl = await endpoint(chrome, timeoutMs);
    const pageUrl = await firstPageTarget(port);
    return await evaluate(pageUrl ?? browserUrl, fn, args, timeoutMs);
  } finally {
    chrome.kill('SIGKILL');
  }
}

function endpoint(chrome, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('chromium did not start')), timeoutMs);
    chrome.stderr.on('data', (chunk) => {
      const match = /ws:\/\/[^\s]+/.exec(String(chunk));
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    chrome.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`chromium exited with ${code}`));
    });
  });
}

async function firstPageTarget(port) {
  for (let attempt = 0; attempt < 20; attempt++) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const page = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      // the port is not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return null;
}

/// Run a function in the page, then send real input, then read a result.
///
/// Synthetic events dispatched from script are not the same thing: they carry
/// isTrusted false, skip the browser's own hit testing, and let a test
/// accidentally prove something the reader would never experience. Gestures
/// are therefore driven through the browser's input pipeline.
export async function withRealInput({ setup, actions, read }, args = {}, { timeoutMs = 30_000 } = {}) {
  const binary = chromiumPath();
  if (!binary) throw new Error('no chromium available');
  const port = 9200 + Math.floor(Math.random() * 500);
  const chrome = spawn(binary, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--mute-audio',
    '--disable-dev-shm-usage', '--window-size=1000,800',
    `--remote-debugging-port=${port}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });

  try {
    await endpoint(chrome, timeoutMs);
    const pageUrl = await firstPageTarget(port);
    const session = await connect(pageUrl, timeoutMs);
    try {
      // The endpoint can answer before the page can run anything, and a test
      // that starts pressing into a page that is not ready fails for reasons
      // that have nothing to do with what it is testing.
      await ready(session, timeoutMs);
      await session.send('Runtime.evaluate', {
        expression: `(${setup.toString()})(${JSON.stringify(args)})`,
        awaitPromise: true, returnByValue: true,
      });
      for (const action of actions) {
        await session.send('Input.dispatchMouseEvent', action);
        await new Promise((resolve) => setTimeout(resolve, action.pause ?? 30));
      }
      // Let the page act on the last input before reading the result.
      await new Promise((resolve) => setTimeout(resolve, 150));
      const result = await session.send('Runtime.evaluate', {
        expression: `(${read.toString()})()`,
        awaitPromise: true, returnByValue: true,
      });
      return result?.result?.result?.value ?? result?.result?.value;
    } finally {
      session.close();
    }
  } finally {
    chrome.kill('SIGKILL');
  }
}

/// Wait until the page can actually evaluate.
async function ready(session, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const probe = await session.send('Runtime.evaluate', {
        expression: 'document.readyState', returnByValue: true,
      });
      if (probe?.result?.result?.value) return;
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error('the page never became ready');
}

function connect(wsUrl, timeoutMs) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(wsUrl);
    const pending = new Map();
    let nextId = 1;
    const timer = setTimeout(() => reject(new Error('devtools did not connect')), timeoutMs);
    socket.addEventListener('open', () => {
      clearTimeout(timer);
      resolve({
        send(method, params) {
          const id = nextId++;
          socket.send(JSON.stringify({ id, method, params }));
          return new Promise((res, rej) => {
            pending.set(id, { res, rej });
            setTimeout(() => {
              if (pending.delete(id)) rej(new Error(`${method} timed out`));
            }, timeoutMs);
          });
        },
        close: () => socket.close(),
      });
    });
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      const waiter = pending.get(message.id);
      if (!waiter) return;
      pending.delete(message.id);
      if (message.error) waiter.rej(new Error(message.error.message));
      else waiter.res(message);
    });
    socket.addEventListener('error', () => reject(new Error('devtools socket failed')));
  });
}

function evaluate(wsUrl, fn, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(wsUrl);
    const timer = setTimeout(() => {
      socket.close();
      reject(new Error('evaluation timed out'));
    }, timeoutMs);

    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({
        id: 1,
        method: 'Runtime.evaluate',
        params: {
          expression: `(${fn.toString()})(${JSON.stringify(args)})`,
          awaitPromise: true,
          returnByValue: true,
        },
      }));
    });

    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== 1) return;
      clearTimeout(timer);
      socket.close();
      const thrown = message.result?.exceptionDetails;
      if (thrown) {
        reject(new Error(thrown.exception?.description ?? 'evaluation threw'));
        return;
      }
      resolve(message.result?.result?.value);
    });

    socket.addEventListener('error', (error) => {
      clearTimeout(timer);
      reject(new Error(`devtools socket failed: ${error?.message ?? 'unknown'}`));
    });
  });
}
