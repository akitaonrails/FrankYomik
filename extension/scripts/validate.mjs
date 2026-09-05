import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const manifestPath = path.join(root, 'manifest.json');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

const errors = [];
const expectedMatches = new Set([
  'https://read.amazon.co.jp/*',
  'https://read.kindle.co.jp/*',
  'https://comic.naver.com/*',
  'https://m.comic.naver.com/*',
]);
const expectedIconPaths = [
  'assets/icons/icon-16.png',
  'assets/icons/icon-32.png',
  'assets/icons/icon-48.png',
  'assets/icons/icon-128.png',
];

function fail(message) {
  errors.push(message);
}

if (manifest.manifest_version !== 3) fail('manifest_version must be 3');
if (!manifest.background?.service_worker) fail('background service_worker is required');
if (manifest.background?.type !== 'module') fail('background service_worker should be a module');
if (manifest.commands?.['force-reprocess-current']?.description !== 'Force reprocess the current Frank Yomik page') {
  fail('force-reprocess-current command with description is required');
}
if (manifest.commands?.['force-reprocess-current']?.suggested_key) {
  fail('force-reprocess-current must not define a default key binding');
}
if (!Array.isArray(manifest.content_scripts) || manifest.content_scripts.length !== 2) {
  fail('two scoped content script declarations are expected: Kindle and Naver Webtoon');
}

const contentScripts = manifest.content_scripts ?? [];
const kindleScript = contentScripts.find((script) => script.matches?.includes('https://read.amazon.co.jp/*'));
const webtoonScript = contentScripts.find((script) => script.matches?.includes('https://comic.naver.com/*'));
if (!kindleScript) fail('missing Kindle content script declaration');
if (!webtoonScript) fail('missing Naver Webtoon content script declaration');

for (const contentScript of contentScripts) {
  for (const match of expectedMatches) {
    // Each expected match should be covered by one declaration, not all declarations.
    if (!contentScripts.some((script) => script.matches?.includes(match))) fail(`missing content script match: ${match}`);
  }
  for (const match of contentScript.matches ?? []) {
    if (!expectedMatches.has(match)) fail(`unexpected content script match: ${match}`);
  }
}
if (kindleScript && kindleScript.all_frames !== true) fail('Kindle content script must run in all frames for reader iframes');
if (webtoonScript && webtoonScript.all_frames === true) fail('Webtoon content script should not run in all frames');
if (kindleScript && !kindleScript.js?.includes('src/content/kindle.js')) fail('Kindle declaration must include kindle.js');
if (kindleScript && kindleScript.js?.includes('src/content/webtoon.js')) fail('Kindle declaration must not include webtoon.js');
if (webtoonScript && !webtoonScript.js?.includes('src/content/webtoon.js')) fail('Webtoon declaration must include webtoon.js');
if (webtoonScript && webtoonScript.js?.includes('src/content/kindle.js')) fail('Webtoon declaration must not include kindle.js');

const allowedPermissions = new Set(['activeTab', 'alarms', 'scripting', 'storage']);
const forbiddenPermissions = new Set(['tabs', 'webRequest', 'webRequestBlocking', '<all_urls>']);
const allowedHostPermissions = new Set([
  'https://image-comic.pstatic.net/*',
  'https://webtoon-phinf.pstatic.net/*',
  'https://swebtoon-phinf.pstatic.net/*',
]);
for (const permission of manifest.permissions ?? []) {
  if (forbiddenPermissions.has(permission)) fail(`forbidden permission: ${permission}`);
  if (!allowedPermissions.has(permission)) fail(`unexpected permission: ${permission}`);
}
for (const origin of manifest.host_permissions ?? []) {
  if (!allowedHostPermissions.has(origin)) fail(`unexpected static host permission: ${origin}`);
}
for (const resource of manifest.web_accessible_resources ?? []) {
  if (resource.resources?.includes('*')) fail('web_accessible_resources must not expose *');
}

const requiredFiles = [
  manifest.background.service_worker,
  manifest.options_page,
  ...new Set(contentScripts.flatMap((script) => script.js ?? [])),
  ...expectedIconPaths,
];
for (const rel of requiredFiles) {
  if (!fs.existsSync(path.join(root, rel))) fail(`referenced file does not exist: ${rel}`);
}

const serviceWorker = fs.readFileSync(path.join(root, manifest.background.service_worker), 'utf8');
if (!serviceWorker.includes('async function handleMessage(message, sender)')) {
  fail('service worker handleMessage must accept sender');
}
if (serviceWorker.includes('chrome.permissions.request')) {
  fail('service worker must not request permissions outside an options-page user gesture');
}

for (const rel of new Set(contentScripts.flatMap((script) => script.js ?? []))) {
  const source = fs.readFileSync(path.join(root, rel), 'utf8');
  if (source.includes('authToken')) fail(`content script must not reference authToken: ${rel}`);
  // The lens and a 9px status dot are the only marks on a page, and both are
  // passive: no buttons, panels or settings belong in a content script.
  if (source.includes('frank-yomik-hud') || /addEventListener\(\s*['"]click['"][^)]*button/i.test(source)) {
    fail(`content script must not create visible Frank controls: ${rel}`);
  }
  if (/document\.createElement\(\s*['"](button|input|select|textarea)['"]/i.test(source)) {
    fail(`content script must not add interactive controls to the page: ${rel}`);
  }
  if (source.includes("endsWith('.pstatic.net')")) {
    fail(`content script must use exact webtoon image host allowlists: ${rel}`);
  }
}

if (errors.length) {
  console.error(errors.map((error) => `- ${error}`).join('\n'));
  process.exit(1);
}

console.log('Extension manifest validation passed.');
