# Frank Manga - Technical Reference

## What This Is

Manga and webtoon translation system with two main components:
1. **Server** (`server/`): Go API, Python workers, manga pipeline (`kindle/`), webtoon pipeline (`webtoon/`), CLI tools, tests, fonts, config
2. **Flutter client** (`client/`): Cross-platform app (Android + Linux) — WebView-based reader for Kindle/Webtoon with inline translation overlay

The CLI tools (`process_manga.py`, `process_webtoon.py`) process local files. The web service accepts images via HTTP, queues them through Redis, and returns translated pages. The Flutter client wraps Kindle (read.amazon.co.jp) and Webtoon (webtoons.com) in a WebView, captures pages, submits them to the API, and overlays translated images.

## Quick Start

### CLI (local processing)

```bash
cd server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Requires Ollama running locally
python process_manga.py furigana          # adult*.png → output/furigana/
python process_manga.py translate         # shounen*.png → output/translate/
python process_manga.py all --debug       # both + debug bounding box images

python process_webtoon.py pipeline URL    # download + translate webtoon
```

### Web Service

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: Go API
cd server && AUTH_TOKEN=secret go run .

# Terminal 3: Python worker (from server/)
cd server && python -m worker --pipeline both

# Submit a job
curl -X POST -H "Authorization: Bearer secret" \
  -F "image=@docs/shounen.png" -F "pipeline=manga_translate" \
  http://localhost:8080/api/v1/jobs
```

### Flutter Client

```bash
cd client
flutter pub get
flutter run -d linux       # Desktop
flutter run -d <device>    # Android (connected device or emulator)
```

Requires the web service running (Go API + Redis + worker).

## Architecture

### CLI Pipeline

```
Image → [Bubble Detection] → [OCR] → [Validation] → ┬→ [Furigana] → [Vertical Render]
                                                      └→ [Translate] → [English Render]
```

### Web Service + Flutter Client

```
Flutter Client (client/)
    ├── WebView: read.amazon.co.jp / webtoons.com
    ├── JS Bridge: page detection + image capture
    ├── Local SQLite cache (SHA256-keyed)
    ↕ HTTPS + WebSocket (Bearer token)
Go API Server (server/)
    ↕ Redis Streams (priority queues) + Pub/Sub (notifications)
Python Workers (server/worker/)
    ├── manga pipeline (server/kindle/)
    └── webtoon pipeline (server/webtoon/)
    ↕ HTTP
Ollama (qwen3:14b)
```

## Directory Structure

```
frank_manga/
  server/                       # All backend code (Python root)
    config.yaml                 # All configuration (Ollama, fonts, OCR, worker)
    requirements.txt            # Python dependencies
    pyproject.toml              # Pytest config
    process_manga.py            # CLI — manga furigana/translation
    process_webtoon.py          # CLI — webtoon translation
    go.mod, go.sum              # Go module
    main.go                     # Entry point, env config, graceful shutdown
    handlers.go                 # REST + WebSocket handlers
    middleware.go               # Bearer token auth (header + query param)
    queue.go                    # Redis stream producer, SHA256 dedup
    results.go                  # Result storage/retrieval
    websocket.go                # WebSocket upgrade, subscribe/notify
    types.go                    # Job, Result, Health types
    cache.go                    # Disk cache layer
    fonts/                      # Font files (.ttf/.otf)
    kindle/                     # Manga processing modules
      config.py                 # Constants loaded from config.yaml
      bubble_detector.py        # RT-DETR-v2 object detection + bubble mask extraction
      ocr.py                    # manga-ocr + Japanese validation
      furigana.py               # pykakasi kanji → hiragana
      translator.py             # Ollama qwen3:14b, Google Translate fallback
      text_renderer.py          # Vertical JP, horizontal EN, SFX rendering
      text_detector.py          # EasyOCR + stroke clustering + small bubble recovery
      image_utils.py            # OpenCV/Pillow I/O, bytes encode/decode
      processor.py              # Pipeline stages, PageResult dataclass
      inpainter.py              # LaMa/diffusion-based text removal
    webtoon/                    # Korean webtoon modules
      processor.py              # Pipeline stages, WebtoonPageResult dataclass
      ocr.py                    # EasyOCR Korean, CLAHE enhancement
      bubble_detector.py        # Text-first clustering, boundary detection
      translator.py             # Korean-specific Ollama prompts
      image_utils.py            # Tall image splitting, detection stitching
      inpainter.py              # LaMa inpainting for webtoon panels
      scraper.py                # Naver Webtoon downloader (nodriver)
      config.py                 # Webtoon-specific font/path config
    worker/                     # Python Redis stream consumer
      main.py                   # Entry: python -m worker [--pipeline manga|webtoon|both]
      consumer.py               # Two-stream priority consumer (high 100ms / low 1s)
      job.py                    # ProcessingJob/ProcessingResult, pipeline routing, shared metadata builders
      health.py                 # Worker health check (queue lengths, heartbeats)
      page_cache.py             # Disk page cache
    tests/
      unit/                     # Pure logic tests (no external deps)
      integration/              # Tests using real images
      conftest.py               # Shared fixtures

  client/                       # Flutter client app (Android + Linux)
    lib/
      main.dart                 # Entry point, ProviderScope
      app.dart                  # MaterialApp, dark Material 3 theme
      models/                   # server_settings, page_job, site_config
      services/                 # api_service, websocket_service, cache_service, image_capture_service
      providers/                # settings, connection, jobs providers
      screens/                  # home, reader, settings, jobs, inspector screens
      webview/                  # js_bridge, overlay_controller, dom_inspector, strategies/
      widgets/                  # connection_banner, progress_indicator, page_status_badge
    test/
      widget_test.dart          # Model + strategy unit tests
    android/                    # Android platform (com.frankmanga.frank_client)
    linux/                      # Linux platform (GTK, Wayland/X11 aware)

  .cloudflared/                  # Cloudflare Tunnel credentials (gitignored)
    config.yml                  # Tunnel config (hostname → service mapping)
    <UUID>.json                 # Tunnel credentials

  docs/                         # Documentation + test images
```

## Critical Technical Decisions

### Bubble Detection (bubble_detector.py)

Uses **RT-DETR-v2** (real-time detection transformer) trained to detect speech bubbles and artwork text in a single pass. Previous approaches tried and rejected:
- Pure OpenCV heuristic (contour + 7-layer FP filtering) — replaced because RT-DETR-v2 is more accurate
- Qwen2.5-VL (VLM-based detection) — produced unreliable coordinates

**Detection flow**: RT-DETR-v2 forward pass → NMS → classify as `bubble` or `artwork_text` → extract bubble mask via OpenCV flood-fill for shape-aware text clearing.

Model detections are trusted — no user-facing false-positive marking UI. Users can only edit translations via the `manual_translation` field in region metadata.

### Translation (translator.py)

- Model: `qwen3:14b` (9GB VRAM, 0.2s/text, 18x faster than qwen2.5vl:32b)
- Uses `/api/chat` endpoint with `think: false` and `num_predict: 1024`
- Strips `<think>...</think>` tags and XML artifacts
- Fallback: `deep-translator` (Google Translate) on Ollama failure
- A/B tested: qwen3:8b leaks Japanese, translategemma:12b flattens tone, qwen3:30b not worth VRAM

### Flutter Client (client/)

**State management**: Riverpod v2 — `settingsProvider`, `connectionProvider`, `jobsProvider`, `cacheServiceProvider`, `apiServiceProvider`, `wsServiceProvider`.

**Site strategy pattern**: `SiteStrategy` base class with `KindleStrategy` and `WebtoonStrategy`. Each defines URL regex, JS detection script, capture script, and URL metadata parsing. Strategy selected at runtime by matching current WebView URL.

**Image capture**: Kindle uses Flutter `takeScreenshot()` (bypasses DRM canvas restrictions). Webtoon uses JS `fetch()` + `FileReader` to extract `<img>` elements as base64.

**Dual-path job tracking**: Before submitting, checks local SQLite cache by SHA256 hash and by metadata (title/chapter/page). On server response, subscribes to WebSocket for real-time updates with 3s polling fallback.

**Overlay**: Webtoon replaces `<img>` src with blob URL of translated image, click toggles original/translated. Kindle overlay is TODO (needs coordinate tracking from JS).

**Translation editing**: Edit mode shows detection boxes on the overlay. Single-click or right-click on a region opens the translation edit dialog. Edits are staged locally until the user explicitly saves, which PATCHes the server metadata and triggers a rerender. The `user` dict on each region contains only `manual_translation` — no false-positive or undetected flags (RT-DETR-v2 detections are trusted).

**Cache**: SQLite3 FFI with filesystem storage at app support directory. Keyed by image hash + pipeline. No TTL (never expires).

### Web Service Design

- **Go API**: Accepts images (default 20 MiB max, configurable via `MAX_IMAGE_SIZE_MB`), deduplicates via SHA256, enqueues to Redis streams, serves results. Never blocks on GPU.
- **Redis Streams**: Two priority levels (`frank:jobs:high` for current page, `frank:jobs:low` for prefetch). MAXLEN trimmed.
- **Python Workers**: Long-running processes, models loaded once at startup. Consumer reads high stream first (100ms block), then low (1s block).
- **WebSocket**: Real-time result push via Redis Pub/Sub → Go subscriber → per-connection channels.
- **Dedup**: SHA256 hash of image bytes → if already queued/completed, returns existing job_id.

### Cloudflare Tunnel

Remote access is provided via Cloudflare Tunnel (`cloudflared`), configured in `docker-compose.yml`:

- **`init-cloudflared`**: Busybox init container that copies `.cloudflared/` credentials into a Docker named volume (`cloudflared-config`). This avoids permission issues with NFS or restrictive host mounts.
- **`cloudflared`**: Runs the tunnel using config from the volume, routes `localhost:8080` → `http://api:8080`.
- **Setup**: Requires `cloudflared tunnel login`, `cloudflared tunnel create yomik`, and DNS routing. Credentials (tunnel UUID JSON + `config.yml`) go in `.cloudflared/` (gitignored).
- **Client default**: Flutter client defaults to `https://localhost:8080` with auth token `mysecrettoken`.

### Text Rendering (text_renderer.py)

- SFX (repeated chars, exclamation words) → vertical stacked letters
- Dialogue → horizontal with word-wrap and hyphenation for narrow bubbles
- Font sizing: binary search for largest font that fits
- Vertical Japanese: character-by-character (Pillow `direction="ttb"` has bugs)

## API Endpoints

```
POST   /api/v1/jobs              Upload image → {job_id, status: "queued"}
GET    /api/v1/jobs/:id          Poll job status + result metadata
GET    /api/v1/jobs/:id/image    Download processed image bytes
DELETE /api/v1/jobs/:id          Cancel/delete job
GET    /api/v1/health            Server + worker + queue status
WS     /api/v1/ws                Real-time result push (subscribe to job_ids)
```

All endpoints except `/health` require `Authorization: Bearer <token>`.

## Redis Layout

| Key | Purpose | TTL |
|-----|---------|-----|
| `frank:jobs:high` / `frank:jobs:low` | Priority job streams | MAXLEN 500/1000 |
| `frank:images:{sha256}` | Uploaded image bytes | 1 hour |
| `frank:results:{job_id}` | Result metadata (JSON) | 1 hour |
| `frank:results:img:{job_id}` | Processed image bytes | 1 hour |
| `frank:dedup` | SHA256 → job_id mapping | 1 hour |
| `frank:notify:{job_id}` | Pub/Sub notification channel | N/A |
| `frank:worker:{name}:heartbeat` | Worker liveness | 60s |

## External Dependencies

- **Ollama** running locally with `qwen3:14b` (~9GB VRAM)
- **Redis** for job queue and result storage
- **manga-ocr** — Japanese OCR (configurable CPU/GPU via `config.yaml`)
- **EasyOCR** — Korean OCR + Japanese text detection (GPU)
- **Go 1.21+** with `go-redis/v9` and `gorilla/websocket`
- **Flutter 3.11+** with `flutter_inappwebview`, `flutter_riverpod`, `web_socket_channel`, `sqflite_common_ffi`

## Configuration

All settings in `server/config.yaml`:
- `ollama:` — model, URL, temperature, think mode
- `fonts:` — JP, EN, SFX font paths (relative to server/)
- `ocr:` — device (cpu/cuda)
- `text_detection:` — confidence, GPU
- `manga_inpainting:` — LaMa/diffusion settings
- `webtoon:` — scraper, OCR, bubble detection, inpainting
- `worker:` — redis_url, consumer_group, heartbeat, timeout

## Regression Testing

**Always run tests after modifying detection thresholds, filters, or rendering logic.**

```bash
cd server && .venv/bin/pytest tests/unit/ -v       # Fast — pure logic (345 tests)
cd server && .venv/bin/pytest tests/integration/ -v # Slower — uses real images
cd server && go test -v .                           # Go API + middleware + cache + subscribe/notify
cd client && flutter test                           # Flutter model + strategy tests (71 tests)
```

### Test coverage

- `tests/unit/test_bubble_detector.py` — detection counts, known bubbles, RT-DETR-v2 integration
- `tests/unit/test_ocr_validation.py` — Japanese text accept/reject
- `tests/unit/test_text_renderer.py` — SFX detection, word wrap, hyphenation
- `tests/unit/test_bytes_io.py` — encode/decode bytes, render_page_to_bytes, load_page_from_memory
- `tests/unit/test_worker_*.py` — job routing, consumer priority logic, health checks, rerender (manual_translation override, legacy metadata tolerance, metadata payload structure)
- `handlers_test.go` — all REST endpoints, auth middleware, cache hit/miss, PATCH + rerender queueing, 409 conflict, subscribe/notify
- `client/test/widget_test.dart` — ServerSettings, PageJob states, SiteConfig, strategy URL parsing, overlay patterns, feedback toolbar + edit_translation dispatch

**Workflow for adjustments**:
1. Run existing tests before making changes
2. Make threshold/filter adjustments
3. Run tests again — fix any regressions before proceeding
4. Add regression tests for new behaviors

## Test Data

- `docs/adult*.png` (5 pages): Seinen manga — furigana pipeline
- `docs/shounen*.png` (10 pages): Various styles — translation pipeline
- Output: `output/furigana/` and `output/translate/` subdirectories
- Debug mode (`--debug`): Saves `*-debug.png` with red bounding boxes

## License

- `server/` — [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0)
- `client/` — [GNU General Public License v3.0](client/LICENSE) (GPL-3.0)
