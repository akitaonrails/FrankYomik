# Frank Yomik Chromium extension

Manifest V3 extension for using a self-hosted Frank Yomik server from desktop Chrome/Chromium.

The extension intentionally keeps Kindle and Naver pages close to vanilla: it does not add in-page buttons, panels, HUDs, or settings overlays. Controls live in the extension popup/options page. Content scripts only detect and capture page images, and present the translated result.

## Reading modes

By default the page is left in its original language and the translation is revealed through a magnifier lens: **press and hold for 200ms** anywhere on a translated page and a circular lens follows the pointer until you let go. It is meant for reading in Japanese or Korean and peeking only at the balloon you cannot parse.

- A quick tap is untouched, so Kindle still turns pages and webtoons still scroll.
- The magnification (1.5x / 2x / 3x) and the mode are set in the popup and apply immediately, without reloading the page.
- **Full page** mode is the older behavior: the translated render replaces the page image.

The lens is the only element the extension puts on the page, it appears only while you hold, and it disappears on release.

![Frank Yomik extension popup on Amazon Manga](docs/chromium-extension-popup.png)

The popup configures the self-hosted API URL, bearer token, enabled sites, Kindle pipeline, target language, reading mode, lens magnification, and webtoon prefetch behavior.

**Kindle pipeline** picks what a page is sent through: *Translate* and *Furigana* for manga, and *Furigana — text book* for reflowable novels, whose pages Kindle also delivers as images but which are typeset prose rather than artwork. The server refuses a page that does not look like prose, so a manga page sent to the book pipeline fails with an explanation rather than annotating the artwork.

That setting is the **default**. Under *Current page*, **Pipeline for this book** overrides it for the volume open in the active tab, so a novel and a manga can each keep their own mode without changing anything when you switch between them. Changes apply to the open reader immediately, without a reload. Settings autosave when you leave a field or change a checkbox/select; **Save now** is retained as a fallback and to trigger browser permission prompts when needed.

![Kindle page translated by the Chromium extension](docs/kindle-extension-translation.png)

## Current-page debug controls

The popup has two manual controls for bug reports and cache misses:

- **Force reprocess current page**: resubmits the visible Kindle/Naver image with `force=true`, bypassing both the extension image cache and the server cache.
- **Send debug pages to server**: uploads the current original and translated page images to the server for later comparison. Recent uploads can be listed with `GET /api/v1/debug/pages`.

An optional command named **Force reprocess the current Frank Yomik page** is registered for `chrome://extensions/shortcuts`, but it has no default key binding. This avoids clashes with Chrome, Google apps, and site shortcuts.

## Supported sites

- Kindle Japan reader:
  - `https://read.amazon.co.jp/*`
  - `https://read.kindle.co.jp/*`
- Naver Webtoon:
  - `https://comic.naver.com/*`
  - `https://m.comic.naver.com/*`

## Install from a GitHub release

Use this path when you only want to install the extension, not develop it.

1. Open the [latest Frank Yomik release](https://github.com/akitaonrails/FrankYomik/releases/latest).
2. Download the extension zip asset named `frank-yomik-extension-*.zip`.
3. Unzip it into a permanent folder, for example:
   - Linux: `~/Applications/frank-yomik-extension/`
   - macOS: `~/Applications/Frank Yomik Extension/`
   - Windows: `C:\Users\<you>\Apps\frank-yomik-extension\`
4. Open your browser's extensions page:
   - Chrome/Chromium/Brave: `chrome://extensions`
   - Edge: `edge://extensions`
5. Enable **Developer mode**.
6. Click **Load unpacked**.
7. Select the extracted folder that contains `manifest.json`.
8. Pin/open the **Frank Yomik** extension action.
9. Set:
   - API base URL, for example `https://frank.example.net` or a trusted-LAN URL.
   - auth token matching server `AUTH_TOKEN`.
   - enabled sites, manga pipeline, target language, reading mode, lens magnification, and webtoon prefetch settings.
10. Leave each field or click **Save now** to save, then allow the exact API-origin permission when Chromium asks.
11. Click **Check server**.
12. Reload any Kindle/Naver tabs that were already open before installing or updating the extension.

To update later, download the newer release zip, replace the extracted folder contents, then click the reload button on the extension card. Removing and re-adding the extension can clear Chromium extension storage, so use **Export settings** first if you want a backup of the API URL/token.

## Load unpacked from source

Use this path while developing from a local clone.

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select this `extension/` directory.
5. Pin/open the **Frank Yomik** extension action.
6. Set:
   - API base URL, for example `https://frank.example.net` or a trusted-LAN URL.
   - auth token matching server `AUTH_TOKEN`.
   - manga pipeline and target language.
7. Leave a field or click **Save now** to save, then allow the exact API-origin permission when Chromium asks.
8. Click **Check server**.
9. Reload any Kindle/Naver tabs that were already open before installing or updating the extension.

For normal development updates, use the reload button on the existing extension card.

## Packaged zip

Create a distributable zip from this directory:

```bash
cd extension
npm run package
```

The output lands in `extension/dist/frank-yomik-extension-<version>.zip`. Unzip it into a dedicated directory, then load that directory from `chrome://extensions` with Developer mode enabled. `extension/dist/` is ignored by git.

## Development checks

```bash
cd extension
npm test
```

This validates the manifest, security guardrails, JavaScript syntax, and unit-tested pure helper modules.

## Runtime model

- `src/background/service_worker.js`
  - owns the bearer token
  - submits `/api/v1/jobs`
  - polls job status
  - downloads same-origin result images
  - stores a bounded IndexedDB result cache
- `src/content/kindle.js`
  - detects visible Kindle blob-backed page images
  - captures the exact page image through canvas
  - handles single pages and spreads
- `src/content/webtoon.js`
  - detects Naver Webtoon page images
  - captures image bytes through page fetch first, then a strict pstatic-host background fallback
  - limits concurrent submissions
- `src/content/overlay.js`
  - only replaces image `src` after a completed translation is available

The popup contains a small diagnostics section. If a page is not translating, open the extension popup on that tab and check whether it reports strategy startup, page detection, queued jobs, or errors.

The popup also has **Export settings** and **Import settings** actions. The export file contains the auth token, so keep it private.

## Security notes

- The bearer token is never sent to content scripts.
- The service worker validates sender tab URLs for capture/fetch requests.
- Content scripts run only on the four supported reader hosts.
- The API host permission is requested only for the configured API origin.
- Result image downloads are rejected unless they resolve to the configured API origin.
- Webtoon background image fetching is limited to exact pstatic image hosts and never sends the bearer token.
- Prefer HTTPS for the server. Plain HTTP on a trusted LAN can work, but exposes page images and the token to network observers.
- Extension icons are generated from the Android launcher artwork under `client/android/app/src/main/res/`.

## Current limitations

- The extension uses polling, not WebSocket, for job completion. This avoids long-lived MV3 service-worker assumptions.
- Chrome for Android is not a target; use the existing Android app there.
- If Kindle or Naver changes their DOM/image loading behavior, the content strategies may need updates.
- Existing reader tabs may need a reload after extension install/update because static content scripts only inject on page load.
