# Frank Manga

Automatic manga and webtoon translation. Detects speech bubbles with RT-DETR-v2, extracts text via OCR, translates with a local LLM, and renders the result back onto the page.

## Components

| Directory | Language | Description |
|-----------|----------|-------------|
| `server/` | Go + Python | API server, processing pipelines (manga + webtoon), Redis worker |
| `client/` | Dart/Flutter | Android + Linux reader app with WebView overlay |
| `docs/` | — | Test images, deployment notes |

## How It Works

**Manga pipeline** (Japanese → English or furigana):
```
Image → RT-DETR-v2 bubble detection → manga-ocr → Ollama translation → English render
                                                 → pykakasi furigana → Vertical JP render
```

**Webtoon pipeline** (Korean → English):
```
Image → EasyOCR text detection → cluster into bubbles → Ollama translation → color-aware render
```

**Web service**: Go API accepts images over HTTP, deduplicates via SHA256, queues through Redis Streams with priority ordering. Python workers process jobs and push results via Redis Pub/Sub + WebSocket.

## Requirements

- Python 3.12+
- [Ollama](https://ollama.ai) with `qwen3:14b` (~9 GB VRAM)
- Go 1.21+
- Redis
- Flutter 3.11+ (for the client app)

## Setup

```bash
git clone https://github.com/akitaonrails/frank_manga.git
cd frank_manga/server

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

ollama pull qwen3:14b
```

## CLI Usage

Run from `server/`:

```bash
# Manga: add furigana readings
python process_manga.py furigana

# Manga: translate to English
python process_manga.py translate

# Both + debug bounding boxes
python process_manga.py all --debug

# Webtoon: download and translate a Naver Webtoon chapter
python process_webtoon.py pipeline <URL>
```

Input: `docs/adult*.png` (furigana), `docs/shounen*.png` (translation).
Output: `output/furigana/`, `output/translate/`.

## Web Service

### Local

```bash
# Terminal 1
redis-server

# Terminal 2
cd server && AUTH_TOKEN=secret go run .

# Terminal 3
cd server && python -m worker --pipeline both
```

### Docker Compose

```bash
# Set auth token
echo "AUTH_TOKEN=mysecret" > .env

# Optionally set UID/GID to match your host user (default 1026)
echo "APP_UID=$(id -u)" >> .env
echo "APP_GID=$(id -g)" >> .env

# Build and start
docker compose up -d

# Check status
docker compose logs -f worker
curl -H "Authorization: Bearer mysecret" http://localhost:8080/api/v1/health
```

### Submit a Job

```bash
curl -X POST -H "Authorization: Bearer secret" \
  -F "image=@docs/shounen.png" \
  -F "pipeline=manga_translate" \
  http://localhost:8080/api/v1/jobs

# Poll status
curl -H "Authorization: Bearer secret" http://localhost:8080/api/v1/jobs/<job_id>

# Download result
curl -H "Authorization: Bearer secret" http://localhost:8080/api/v1/jobs/<job_id>/image -o result.png
```

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/jobs` | Upload image, returns `job_id` |
| GET | `/api/v1/jobs/:id` | Poll job status and metadata |
| GET | `/api/v1/jobs/:id/image` | Download processed image |
| DELETE | `/api/v1/jobs/:id` | Cancel/delete a job |
| GET | `/api/v1/health` | Server + worker + queue status |
| WS | `/api/v1/ws` | Real-time result push |

All endpoints except `/health` require `Authorization: Bearer <token>`.

Pipelines: `manga_translate`, `manga_furigana`, `webtoon`. Priority: `high` (default) or `low` (prefetch).

## Flutter Client

```bash
cd client
flutter pub get
flutter run -d linux       # Desktop
flutter run -d <device>    # Android
```

## Testing

All tests run from `server/`:

```bash
cd server

# Python unit tests (345 tests, ~8s)
.venv/bin/pytest tests/unit/ -v

# Python integration tests (34 tests, ~16s, needs test images in docs/)
.venv/bin/pytest tests/integration/ -v

# All Python tests
.venv/bin/pytest tests/ -v

# Go API tests (needs Redis for full coverage, skips gracefully without it)
go test -v .

# Flutter tests
cd ../client && flutter test
```

## Configuration

All settings in `server/config.yaml`:

| Section | Controls |
|---------|----------|
| `ollama` | Model, URL, temperature, think mode |
| `fonts` | Japanese, English, SFX font paths |
| `ocr` | Device (cpu/cuda) for manga-ocr |
| `text_detection` | EasyOCR confidence and GPU for artwork text |
| `manga_inpainting` | LaMa text removal (off by default) |
| `webtoon` | Scraper, OCR, bubble detection, inpainting |
| `worker` | Redis, consumer group, heartbeat, timeout |

## License

- `server/` — [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0)
- `client/` — [GNU General Public License v3.0](client/LICENSE) (GPL-3.0)
