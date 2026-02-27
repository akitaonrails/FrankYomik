# Frank Manga - Technical Reference

## What This Is

Manga and webtoon translation system with four components:
1. **Manga pipeline** (`pipeline/`): Detect speech bubbles, OCR Japanese text, add furigana or translate to English
2. **Webtoon pipeline** (`webtoon/`): Detect Korean text, translate to English, render with color-aware typography
3. **Web service** (`cmd/server/` + `worker/`): Go API server + Python workers connected via Redis streams for async processing
4. **Flutter client** (`frank_client/`): Cross-platform app (Android + Linux) — WebView-based reader for Kindle/Webtoon with inline translation overlay

The CLI tools (`process_manga.py`, `process_webtoon.py`) process local files. The web service accepts images via HTTP, queues them through Redis, and returns translated pages. The Flutter client wraps Kindle (read.amazon.co.jp) and Webtoon (webtoons.com) in a WebView, captures pages, submits them to the API, and overlays translated images.

## Quick Start

### CLI (local processing)

```bash
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
cd cmd/server && AUTH_TOKEN=secret go run .

# Terminal 3: Python worker
python -m worker --pipeline both

# Submit a job
curl -X POST -H "Authorization: Bearer secret" \
  -F "image=@docs/shounen.png" -F "pipeline=manga_translate" \
  http://localhost:8080/api/v1/jobs
```

### Flutter Client

```bash
cd frank_client
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
Flutter Client (frank_client/)
    ├── WebView: read.amazon.co.jp / webtoons.com
    ├── JS Bridge: page detection + image capture
    ├── Local SQLite cache (SHA256-keyed)
    ↕ HTTPS + WebSocket (Bearer token)
Go API Server (cmd/server/)
    ↕ Redis Streams (priority queues) + Pub/Sub (notifications)
Python Workers (worker/)
    ├── manga pipeline (pipeline/)
    └── webtoon pipeline (webtoon/)
    ↕ HTTP
Ollama (qwen3:14b)
```

## Directory Structure

```
frank_manga/
  process_manga.py              # CLI — manga furigana/translation
  process_webtoon.py            # CLI — webtoon translation
  config.yaml                   # All configuration (Ollama, fonts, OCR, worker)

  pipeline/                     # Manga processing modules
    config.py                   # Constants loaded from config.yaml
    bubble_detector.py          # OpenCV contour analysis, 7-layer FP filtering
    ocr.py                      # manga-ocr + Japanese validation
    furigana.py                 # pykakasi kanji → hiragana
    translator.py               # Ollama qwen3:14b, Google Translate fallback
    text_renderer.py            # Vertical JP, horizontal EN, SFX rendering
    text_detector.py            # EasyOCR + stroke clustering + small bubble recovery
    image_utils.py              # OpenCV/Pillow I/O, bytes encode/decode
    processor.py                # Pipeline stages, PageResult dataclass
    inpainter.py                # LaMa/diffusion-based text removal

  webtoon/                      # Korean webtoon modules
    processor.py                # Pipeline stages, WebtoonPageResult dataclass
    ocr.py                      # EasyOCR Korean, CLAHE enhancement
    bubble_detector.py          # Text-first clustering, boundary detection
    translator.py               # Korean-specific Ollama prompts
    image_utils.py              # Tall image splitting, detection stitching
    inpainter.py                # LaMa inpainting for webtoon panels
    scraper.py                  # Naver Webtoon downloader (nodriver)
    config.py                   # Webtoon-specific font/path config

  worker/                       # Python Redis stream consumer
    main.py                     # Entry: python -m worker [--pipeline manga|webtoon|both]
    consumer.py                 # Two-stream priority consumer (high 100ms / low 1s)
    job.py                      # ProcessingJob/ProcessingResult, pipeline routing
    health.py                   # Worker health check (queue lengths, heartbeats)

  cmd/server/                   # Go API server
    main.go                     # Entry point, env config, graceful shutdown
    handlers.go                 # REST + WebSocket handlers
    middleware.go               # Bearer token auth (header + query param)
    queue.go                    # Redis stream producer, SHA256 dedup
    results.go                  # Result storage/retrieval
    websocket.go                # WebSocket upgrade, subscribe/notify
    types.go                    # Job, Result, Health types

  frank_client/                 # Flutter client app (Android + Linux)
    lib/
      main.dart                 # Entry point, ProviderScope
      app.dart                  # MaterialApp, dark Material 3 theme
      models/
        server_settings.dart    # Server URL, auth token, pipeline selection
        page_job.dart           # Job lifecycle tracking (pending→completed)
        site_config.dart        # Kindle + Webtoon site definitions
      services/
        api_service.dart        # REST client (all /api/v1/* endpoints)
        websocket_service.dart  # WS client, auto-reconnect, subscribe/unsubscribe
        cache_service.dart      # SQLite FFI cache (hash + metadata lookup)
        image_capture_service.dart  # WebView screenshot + JS image extraction
      providers/
        settings_provider.dart  # SharedPreferences persistence
        connection_provider.dart # Health check + WS handshake state
        jobs_provider.dart      # Job submission, polling fallback, cache
      screens/
        home_screen.dart        # URL bar + quick-launch cards
        reader_screen.dart      # WebView + JS bridge + overlay
        settings_screen.dart    # Server config + pipeline selection
        jobs_screen.dart        # Job history + status badges
        inspector_screen.dart   # DOM debug view
      webview/
        js_bridge.dart          # Strategy manager, URL detection
        overlay_controller.dart # Image src swap (original ↔ translated)
        dom_inspector.dart      # JS element logger
        strategies/
          base_strategy.dart    # Abstract SiteStrategy interface
          kindle_strategy.dart  # Screenshot capture, ASIN extraction
          webtoon_strategy.dart # IntersectionObserver, JS fetch to base64
      widgets/                  # connection_banner, progress_indicator, page_status_badge
    test/
      widget_test.dart          # Model + strategy unit tests
    android/                    # Android platform (com.frankmanga.frank_client)
    linux/                      # Linux platform (GTK, Wayland/X11 aware)

  tests/
    unit/                       # Pure logic tests (no external deps)
    integration/                # Tests using real images
    conftest.py                 # Shared fixtures
```

## Critical Technical Decisions

### Bubble Detection (bubble_detector.py)

Uses pure OpenCV — VLM-based detection (Qwen2.5-VL) was tried first but produced unreliable coordinates.

**Detection flow**: Binary threshold (>200) → morphological cleanup → RETR_TREE contour hierarchy → filter by area, aspect ratio, convex hull solidity (>0.6), interior brightness (>200).

**False-positive filters** (reject faces, clothing, white backgrounds):

1. **Edge density < 0.12** — Bubbles have sparse edges; faces have many
2. **Bright pixel ratio** — Grayscale: >0.65 at 240, color: >0.50 at 220
3. **Very bright ratio > 0.20 (color only)** — Catches colored skin/faces
4. **Mid-tone ratio** — Grayscale: <0.15, color: <0.40
5. **Circularity > 0.15** — `4π × area / perimeter²`
6. **Border darkness < 160** — Bubbles have dark ink outlines
7. **Largest dark component < 0.08 × inner area** — Rejects face regions

**Why NOT std dev**: Text strokes create high variance even in real bubbles.

### Translation (translator.py)

- Model: `qwen3:14b` (9GB VRAM, 0.2s/text, 18x faster than qwen2.5vl:32b)
- Uses `/api/chat` endpoint with `think: false` and `num_predict: 1024`
- Strips `<think>...</think>` tags and XML artifacts
- Fallback: `deep-translator` (Google Translate) on Ollama failure
- A/B tested: qwen3:8b leaks Japanese, translategemma:12b flattens tone, qwen3:30b not worth VRAM

### Flutter Client (frank_client/)

**State management**: Riverpod v2 — `settingsProvider`, `connectionProvider`, `jobsProvider`, `cacheServiceProvider`, `apiServiceProvider`, `wsServiceProvider`.

**Site strategy pattern**: `SiteStrategy` base class with `KindleStrategy` and `WebtoonStrategy`. Each defines URL regex, JS detection script, capture script, and URL metadata parsing. Strategy selected at runtime by matching current WebView URL.

**Image capture**: Kindle uses Flutter `takeScreenshot()` (bypasses DRM canvas restrictions). Webtoon uses JS `fetch()` + `FileReader` to extract `<img>` elements as base64.

**Dual-path job tracking**: Before submitting, checks local SQLite cache by SHA256 hash and by metadata (title/chapter/page). On server response, subscribes to WebSocket for real-time updates with 3s polling fallback.

**Overlay**: Webtoon replaces `<img>` src with blob URL of translated image, click toggles original/translated. Kindle overlay is TODO (needs coordinate tracking from JS).

**Cache**: SQLite3 FFI with filesystem storage at app support directory. Keyed by image hash + pipeline. No TTL (never expires).

### Web Service Design

- **Go API**: Accepts images, deduplicates via SHA256, enqueues to Redis streams, serves results. Never blocks on GPU.
- **Redis Streams**: Two priority levels (`frank:jobs:high` for current page, `frank:jobs:low` for prefetch). MAXLEN trimmed.
- **Python Workers**: Long-running processes, models loaded once at startup. Consumer reads high stream first (100ms block), then low (1s block).
- **WebSocket**: Real-time result push via Redis Pub/Sub → Go subscriber → per-connection channels.
- **Dedup**: SHA256 hash of image bytes → if already queued/completed, returns existing job_id.

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

All settings in `config.yaml`:
- `ollama:` — model, URL, temperature, think mode
- `fonts:` — JP, EN, SFX font paths (relative to project root)
- `ocr:` — device (cpu/cuda)
- `text_detection:` — enable/disable, confidence, GPU
- `manga_inpainting:` — LaMa/diffusion settings
- `webtoon:` — scraper, OCR, bubble detection, inpainting
- `worker:` — redis_url, consumer_group, heartbeat, timeout

## Regression Testing

**Always run `pytest tests/` after modifying detection thresholds, filters, or rendering logic.**

```bash
pytest tests/unit/ -v              # Fast — pure logic (318 tests)
pytest tests/integration/ -v       # Slower — uses real images
go test ./cmd/server/ -v           # Go API + middleware + subscribe/notify
cd frank_client && flutter test    # Flutter model + strategy tests
```

### Test coverage

- `tests/unit/test_bubble_detector.py` — detection counts, known bubbles, FP rejection
- `tests/unit/test_ocr_validation.py` — Japanese text accept/reject
- `tests/unit/test_text_renderer.py` — SFX detection, word wrap, hyphenation
- `tests/unit/test_bytes_io.py` — encode/decode bytes, render_page_to_bytes, load_page_from_memory
- `tests/unit/test_worker_*.py` — job routing, consumer priority logic, health checks
- `cmd/server/handlers_test.go` — all REST endpoints, auth middleware, subscribe/notify
- `frank_client/test/widget_test.dart` — ServerSettings, PageJob states, SiteConfig, strategy URL parsing

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
