# Frank Manga

Translate manga and webtoon pages automatically. Detects speech bubbles, extracts text via OCR, and either adds furigana readings to kanji or translates to English — then renders the result back onto the page.

Works as a local CLI for batch processing, or as a web service for browser extensions.

## How It Works

```
Image → Bubble Detection (OpenCV) → OCR → ┬→ Furigana (pykakasi) → Vertical JP Render
                                           └→ Translation (Ollama) → English Render
```

**Manga pipeline**: Pure OpenCV contour analysis detects speech bubbles with 7-layer false positive filtering (rejects faces, clothing, backgrounds). manga-ocr extracts Japanese text. Ollama translates or pykakasi adds furigana readings.

**Webtoon pipeline**: EasyOCR detects Korean text, clusters it into bubbles, and uses boundary detection (contour → flood fill → padded bbox fallback). Translates to English with color-aware rendering that samples the original background.

**Web service**: Go API accepts images over HTTP, deduplicates via SHA256, and queues jobs through Redis Streams. Python workers consume jobs with priority ordering (current page before prefetched pages) and push results back via Redis Pub/Sub + WebSocket.

## Requirements

- Python 3.12+
- [Ollama](https://ollama.ai) with `qwen3:14b` pulled (~9GB VRAM)
- Go 1.21+ (for the web service)
- Redis (for the web service)

## Setup

```bash
git clone https://github.com/akitaonrails/frank_manga.git
cd frank_manga

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Pull the translation model
ollama pull qwen3:14b
```

## Usage

### CLI — Local Processing

```bash
# Manga: add furigana to adult manga pages
python process_manga.py furigana

# Manga: translate shounen manga to English
python process_manga.py translate

# Both pipelines + debug bounding box images
python process_manga.py all --debug

# Webtoon: download and translate a Naver Webtoon chapter
python process_webtoon.py pipeline <URL>
```

Input images go in `docs/` (adult*.png for furigana, shounen*.png for translation). Output appears in `output/furigana/` and `output/translate/`.

### Web Service

Start three processes:

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: Go API server
cd cmd/server && AUTH_TOKEN=mysecret go run .

# Terminal 3: Python worker(s)
python -m worker --pipeline both
```

Submit a job:

```bash
# Upload an image for translation
curl -X POST -H "Authorization: Bearer mysecret" \
  -F "image=@docs/shounen.png" \
  -F "pipeline=manga_translate" \
  http://localhost:8080/api/v1/jobs

# Poll for result
curl -H "Authorization: Bearer mysecret" \
  http://localhost:8080/api/v1/jobs/<job_id>

# Download translated image
curl -H "Authorization: Bearer mysecret" \
  http://localhost:8080/api/v1/jobs/<job_id>/image -o result.png
```

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/jobs` | Upload image, returns `job_id` immediately |
| GET | `/api/v1/jobs/:id` | Poll job status and metadata |
| GET | `/api/v1/jobs/:id/image` | Download processed image |
| DELETE | `/api/v1/jobs/:id` | Cancel/delete a job |
| GET | `/api/v1/health` | Server, worker, and queue status |
| WS | `/api/v1/ws` | Real-time result push via WebSocket |

All endpoints except `/health` require `Authorization: Bearer <token>`.

**Pipelines**: `manga_translate`, `manga_furigana`, `webtoon`

**Priority**: `high` (default, current page) or `low` (prefetch)

### WebSocket

Connect to `/api/v1/ws?token=<bearer>` and subscribe to job IDs for real-time notifications:

```json
{"type": "subscribe", "job_ids": ["job-id-1", "job-id-2"]}
```

The server pushes completion events:

```json
{"type": "job_complete", "job_id": "job-id-1", "image_url": "/api/v1/jobs/job-id-1/image"}
```

## Configuration

All settings live in `config.yaml`:

| Section | Controls |
|---------|----------|
| `ollama` | Model name, URL, temperature, think mode |
| `fonts` | Japanese, English, and SFX font paths |
| `ocr` | Device (cpu/cuda) for manga-ocr |
| `text_detection` | EasyOCR for artwork text detection |
| `manga_inpainting` | LaMa/diffusion text removal |
| `webtoon` | Scraper, OCR, bubble detection, inpainting |
| `worker` | Redis URL, consumer group, heartbeat, timeout |

## Project Structure

```
frank_manga/
  process_manga.py              # CLI entry point for manga
  process_webtoon.py            # CLI entry point for webtoons
  config.yaml                   # All configuration

  pipeline/                     # Manga processing modules
    bubble_detector.py          # OpenCV contour analysis + 7 FP filters
    ocr.py                      # manga-ocr with Japanese validation
    furigana.py                 # Kanji → hiragana via pykakasi
    translator.py               # Ollama + Google Translate fallback
    text_renderer.py            # Vertical JP, horizontal EN, SFX rendering
    text_detector.py            # EasyOCR + stroke clustering
    processor.py                # Pipeline orchestration

  webtoon/                      # Korean webtoon modules
    processor.py                # Webtoon pipeline orchestration
    ocr.py                      # EasyOCR Korean
    bubble_detector.py          # Text-first clustering
    translator.py               # Korean-specific translation
    scraper.py                  # Naver Webtoon downloader

  worker/                       # Python Redis stream consumer
    consumer.py                 # Two-priority-stream consumer loop
    job.py                      # Job processing and pipeline routing
    health.py                   # Health check utilities

  cmd/server/                   # Go API server
    handlers.go                 # REST + WebSocket route handlers
    middleware.go               # Bearer token authentication
    queue.go                    # Redis stream producer with dedup
    websocket.go                # WebSocket subscribe/notify

  tests/
    unit/                       # Fast tests, no external dependencies
    integration/                # Tests using real images
```

## Testing

```bash
# Python unit tests
pytest tests/unit/ -v

# Python integration tests (requires test images in docs/)
pytest tests/integration/ -v

# Go API tests
go test ./cmd/server/ -v
```

## License

This project is a proof of concept for personal use.
