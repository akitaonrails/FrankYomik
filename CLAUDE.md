# Frank Manga - Technical Reference

## What This Is

Manga and webtoon translation system with three components:
1. **Manga pipeline** (`pipeline/`): Detect speech bubbles, OCR Japanese text, add furigana or translate to English
2. **Webtoon pipeline** (`webtoon/`): Detect Korean text, translate to English, render with color-aware typography
3. **Web service** (`cmd/server/` + `worker/`): Go API server + Python workers connected via Redis streams for async processing

The CLI tools (`process_manga.py`, `process_webtoon.py`) process local files. The web service accepts images via HTTP, queues them through Redis, and returns translated pages — designed for browser extensions on read.amazon.co.jp and webtoon.com.

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

## Architecture

### CLI Pipeline

```
Image → [Bubble Detection] → [OCR] → [Validation] → ┬→ [Furigana] → [Vertical Render]
                                                      └→ [Translate] → [English Render]
```

### Web Service

```
Browser Extension
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
```

### Test coverage

- `tests/unit/test_bubble_detector.py` — detection counts, known bubbles, FP rejection
- `tests/unit/test_ocr_validation.py` — Japanese text accept/reject
- `tests/unit/test_text_renderer.py` — SFX detection, word wrap, hyphenation
- `tests/unit/test_bytes_io.py` — encode/decode bytes, render_page_to_bytes, load_page_from_memory
- `tests/unit/test_worker_*.py` — job routing, consumer priority logic, health checks
- `cmd/server/handlers_test.go` — all REST endpoints, auth middleware, subscribe/notify

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
