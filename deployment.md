# Production Deployment: RunPod + Kamal

Deploy Frank Manga to a RunPod GPU pod using Kamal for container orchestration.

## Architecture

Single RunPod GPU pod running all services:

```
Internet → Traefik (Kamal proxy) → Go API (:8080)
                                       ↕ Redis (:6379)
                                       ↕ Python Worker (GPU)
                                       ↕ Ollama (GPU, :11434)
```

All persistent data lives on a RunPod **Network Volume** that survives pod restarts.

## Prerequisites

- [Kamal 2](https://kamal-deploy.org/) installed locally (`gem install kamal`)
- Docker Hub account (or other registry) for pushing images
- RunPod account with a GPU pod (RTX 4090 / A6000 recommended, 24GB+ VRAM)
- RunPod Network Volume attached to the pod

## 1. Network Volume Layout

Attach a network volume to your RunPod pod at `/runpod-volume/`. Create the directory structure on first setup:

```bash
ssh root@<RUNPOD_IP>
mkdir -p /runpod-volume/{models/mangaocr,models/easyocr,models/huggingface,ollama,redis,cache}
```

Layout:
```
/runpod-volume/
├── models/
│   ├── mangaocr/       # manga-ocr transformer weights
│   ├── easyocr/        # EasyOCR models (.pth)
│   └── huggingface/    # HuggingFace hub cache
├── ollama/             # Ollama model blobs
├── redis/              # Redis AOF/RDB persistence
└── cache/              # Translated image filesystem cache
```

## 2. Build & Push Images

From the project root:

```bash
# Tag with your registry
export REGISTRY=docker.io/yourusername

docker build -f Dockerfile.api -t $REGISTRY/frank-api:latest .
docker build -f Dockerfile.worker -t $REGISTRY/frank-worker:latest .

docker push $REGISTRY/frank-api:latest
docker push $REGISTRY/frank-worker:latest
```

## 3. Kamal Configuration

Create `config/deploy.yml`:

```yaml
service: frank-manga
image: yourusername/frank-api

servers:
  web:
    hosts:
      - <RUNPOD_IP>
    labels:
      traefik.http.routers.frank-manga.rule: PathPrefix(`/`)

registry:
  username: yourusername
  password:
    - KAMAL_REGISTRY_PASSWORD

env:
  clear:
    REDIS_URL: redis://frank-manga-redis:6379
    CACHE_DIR: /data/cache
  secret:
    - AUTH_TOKEN

volumes:
  - /runpod-volume/cache:/data/cache:ro

accessories:
  redis:
    image: redis:7-alpine
    host: <RUNPOD_IP>
    port: 6379
    volumes:
      - /runpod-volume/redis:/data
    cmd: redis-server --appendonly yes
    options:
      health-cmd: "redis-cli ping"
      health-interval: 5s

  ollama:
    image: ollama/ollama
    host: <RUNPOD_IP>
    port: 11434
    volumes:
      - /runpod-volume/ollama:/root/.ollama
    options:
      gpus: all

  worker:
    image: yourusername/frank-worker
    host: <RUNPOD_IP>
    env:
      clear:
        OLLAMA_BASE_URL: http://frank-manga-ollama:11434
    cmd: --pipeline both --redis-url redis://frank-manga-redis:6379
    volumes:
      - /runpod-volume/cache:/app/cache
      - /runpod-volume/models/mangaocr:/home/worker/.cache/manga_ocr
      - /runpod-volume/models/huggingface:/home/worker/.cache/huggingface
      - /runpod-volume/models/easyocr:/home/worker/.EasyOCR
    options:
      gpus: all
```

## 4. Secrets

Create `.kamal/secrets` (git-ignored):

```bash
AUTH_TOKEN=your-secret-token-here
KAMAL_REGISTRY_PASSWORD=your-docker-hub-password
```

## 5. Pre-Seed Ollama Model

After the first deploy (or manually before), pull the translation model:

```bash
# SSH into pod
ssh root@<RUNPOD_IP>

# Pull model into the network volume
docker exec frank-manga-ollama ollama pull qwen3:14b
```

This downloads ~9GB to `/runpod-volume/ollama/` and persists across restarts.

## 6. First Deploy

```bash
kamal setup
```

This will:
1. Install Docker on the host (if needed)
2. Start Traefik proxy
3. Start accessories (Redis, Ollama, Worker)
4. Deploy the Go API behind Traefik

## 7. Subsequent Deploys

After code changes, rebuild and deploy:

```bash
docker build -f Dockerfile.api -t $REGISTRY/frank-api:latest .
docker push $REGISTRY/frank-api:latest
kamal deploy

# If worker code changed:
docker build -f Dockerfile.worker -t $REGISTRY/frank-worker:latest .
docker push $REGISTRY/frank-worker:latest
kamal accessory reboot worker
```

## 8. Monitoring

```bash
# Service status
kamal details

# API logs
kamal app logs

# Worker logs
kamal accessory logs worker

# Health check
curl -s https://<RUNPOD_IP>/api/v1/health | jq .

# Redis queue lengths
kamal accessory exec redis "redis-cli XLEN frank:jobs:high"
kamal accessory exec redis "redis-cli XLEN frank:jobs:low"
```

## 9. Troubleshooting

### Worker can't connect to Ollama
Ollama takes 20-30s to start. The worker will retry on connection failure. Check:
```bash
kamal accessory logs ollama
curl http://localhost:11434/api/tags  # from inside the pod
```

### Out of VRAM
The worker loads manga-ocr (~350MB), EasyOCR (~600MB), and LaMa (~200MB) into VRAM. Ollama loads qwen3:14b (~9GB). Total ~10GB. If using a 24GB GPU, there's headroom. For 16GB GPUs, set `ocr.device: cpu` in `config.yaml`.

### Model cache not persisting
Verify volume mounts: `docker inspect frank-manga-worker | jq '.[0].Mounts'`. The HuggingFace cache must map to `/home/worker/.cache/huggingface` (not `/root/`).

### Stale results after redeploy
Redis data persists. To flush: `kamal accessory exec redis "redis-cli FLUSHDB"`.

### Pod restarted, models gone
If using a RunPod Network Volume, models survive restarts. Without a network volume, you'll need to re-pull `ollama pull qwen3:14b` and the worker will re-download OCR models on first job.
