# Strix Halo execution assessment

Assessment date: October 9, 2026

Host: `192.168.0.90` (`marvin-server`)

## Decision

The comparison can run on this host. Its Ryzen AI Max+ 395 exposes the Radeon 8060S as ROCm target `gfx1151`, and Koharu's pinned runtime source explicitly supports `gfx1151` on Linux.

The compatibility question is resolved. The pinned Koharu detector, three crop OCR engines, LaMa, and AOT all initialized Koharu's ROCm 10.0 user-space runtime and completed GPU inference successfully. This ran alongside Frank's PyTorch 2.9.1 / ROCm 7.2.1 workers without changing their images or caches.

## Observed capacity

| Resource | Observed value |
| --- | ---: |
| CPU | Ryzen AI Max+ 395, 16 cores / 32 threads |
| System memory | 61.9 GiB |
| Available memory during inspection | 49 GiB |
| GPU/unified memory reported by KFD/PyTorch | 64 GiB |
| GPU target | `gfx1151` |
| Free storage under `/var` | 2.6 TiB |
| Docker | 29.7.2 |
| Existing Frank worker runtime | PyTorch 2.9.1, ROCm 7.2.1 |

GPU memory is unified with system memory. A model allocation reduces memory available to the rest of the host even though tools describe it as 64 GiB of VRAM.

## Executed smoke results

The smoke run used Koharu commit `2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3` with the evaluation-only mask serialization patch. The CLI binaries were compiled in an isolated local worktree and copied to the server, so the host did not need a Rust or LLVM toolchain.

Koharu reported one `AMD Radeon 8060S Graphics` device, target `gfx1151`, with 65,536 MiB VRAM. Its resolved runtime was ROCm 10.0.0 with Torch 2.13.0.7 HIP.

| Check | Result |
| --- | --- |
| Detector compatibility case | Passed; 46 detections with boxes and instance masks |
| Prepared detector corpus | 19/19 pages passed |
| Corpus detector output | 786 detections and 786 serialized masks |
| Cached detector latency | 2.26–2.40 seconds/page; 2.30 second mean |
| Manga OCR | Passed; 1.57 seconds cached |
| Baberu OCR | Passed; 1.41 seconds cached |
| Hayai OCR | Passed; 1.14 seconds cached |
| LaMa | Passed; 1.61 seconds cached |
| AOT | Passed; 1.22 seconds cached |
| RORem Mixed | Passed; about 16.4 seconds per cached one-shot case |
| FLUX.2 Klein | Passed; about 7.6 seconds per cached one-shot case |

The three OCR engines were given the same crop. Manga OCR and Baberu produced identical text; Hayai differed only in the ellipsis representation. This is a runtime check, not an accuracy result, because the crop has not yet been added to the verified OCR annotations.

LaMa and AOT returned PNGs at the original 157 by 246 pixel dimensions. Pixel comparison confirmed that both changed only the supplied mask area. Visual inspection confirmed that both produced valid image content. This synthetic mask is also a runtime check rather than an inpainting quality judgment.

The cached detector run raised total observed VRAM use by about 5.5 GiB. The OCR runs peaked below 1.1 GiB total observed VRAM. The readings include concurrent Frank activity, so they are capacity evidence rather than isolated model measurements.

The isolated Koharu cache occupied 8.4 GiB after installing the runtime, detector, three OCR engines, LaMa, and AOT. It grew to 17 GiB after the later RORem and FLUX.2 Klein comparison. The server retained roughly 49 GiB available system memory during the initial detector setup. Frank remained healthy with both workers registered before and after the runs.

The runtime emitted a warning that Torch lacks memory-efficient attention, and AOT emitted MIOpen workspace-selection warnings. Neither warning caused an inference failure or malformed output. They may affect performance and should remain visible in longer benchmark logs.

These results establish runtime compatibility and adequate capacity. They do not establish that Koharu's output quality exceeds Frank's; that conclusion still requires verified annotations and blind inpainting reviews.

## Weight downloads for the focused comparison

Sizes below are the exact pinned files selected by the reviewed Koharu commit. They exclude native runtimes, tokenizers, configuration files, activations, and temporary build data.

| Stage | Model assets | Download size |
| --- | --- | ---: |
| Detection | [Koharu Layout RF-DETR Seg 2XL](https://huggingface.co/mayocream/koharu-layout-rfdetr-seg-2xl-1152) | 154 MiB |
| Japanese OCR | [Manga OCR](https://huggingface.co/mayocream/manga-ocr) | 442 MiB |
| Manga OCR alternative | [Baberu OCR](https://huggingface.co/genshiai-daichi/baberu-ocr) | 430 MiB |
| Multilingual crop OCR | [Hayai OCR v2](https://huggingface.co/JustANormalTinkerer/hayai-ocr-v2) | 594 MiB |
| General vision OCR | [PaddleOCR VL 1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) | 1.79 GiB |
| Direct inpainting | [LaMa Manga](https://huggingface.co/mayocream/lama-manga) | 195 MiB |
| Direct inpainting | [AOT](https://huggingface.co/mayocream/aot-inpainting) | 22 MiB |
| Generative inpainting | [RORem Mixed](https://huggingface.co/mayocream/RORem-mixed-GGUF) plus pinned SDXL VAE and encoders | 3.56 GiB |
| Generative inpainting | [FLUX.2 Klein](https://huggingface.co/unsloth/FLUX.2-klein-4B-GGUF) plus Qwen3 4B encoder and VAE | 4.98 GiB |

The detector, crop OCR, LaMa, and AOT models are small for this machine. RORem and FLUX fit, but their peak memory will exceed their file sizes.

## Existing Frank deployment

The host already runs:

- Two `frank-yomik-worker-rocm` containers with `/dev/kfd` and `/dev/dri` access.
- PyTorch reporting one AMD GPU and 64 GiB of device memory.
- Shared Manga OCR, EasyOCR, and Hugging Face caches.
- Ollama on port 11434.
- llama-swap on port 11435.

The existing worker containers each have an 8 GiB memory limit and should not host the Koharu comparison. They also process the production queue. During inspection the API reported 501 high-priority and 51 low-priority queued jobs.

The host has no Rust, Cargo, Clang, CMake, or Ninja toolchain. Koharu must be compiled in a dedicated build container or elsewhere and copied to the evaluation container.

## Recommended execution layout

Use a separate, disposable container rather than modifying the production workers.

Required container properties:

```yaml
devices:
  - /dev/kfd:/dev/kfd
  - /dev/dri:/dev/dri
security_opt:
  - seccomp:unconfined
group_add:
  - "485"
  - "488"
environment:
  HSA_OVERRIDE_GFX_VERSION: "11.5.1"
  PYTORCH_ROCM_ARCH: "gfx1151"
mem_limit: 32g
memswap_limit: 40g
shm_size: 8g
```

Keep source, Cargo output, model caches, private evaluation assets, and results under `/var/opt/docker/frank/yomik-eval/`. The root filesystem has only about 11 GiB free, while `/var` has ample capacity.

Do not mount the production result cache read-write. The benchmark may reuse model download caches only when ownership and file locking are compatible; a separate Koharu cache is safer for the first run.

## Run order

1. Wait until the production queue is drained or explicitly pause the production workers.
2. Confirm llama-swap and Ollama have no large model resident.
3. Build the patched detector CLI in the dedicated build container.
4. Run one detector case and confirm ROCm initialization, mask JSON, and output dimensions.
5. Run the labeled detection corpus.
6. Run Manga OCR, Baberu, Hayai, LaMa, and AOT.
7. Run PaddleOCR VL only if the smaller OCR engines leave meaningful uncertainty.
8. Run RORem and FLUX only if direct inpainting does not meet the blind-review threshold.

This order answers the important questions with the smallest GPU and download cost first.

## Operational gate

Before starting the evaluation container, require:

- No large llama-swap or Ollama model loaded.
- At least 32 GiB system memory available.
- Production queue drained, or production workers intentionally paused.
- At least 30 GiB free under `/var` for source, compilation, runtimes, models, and outputs.

The reusable benchmark workspace is `/var/opt/docker/frank/yomik-eval/`. It contains the eight pinned CLI binaries, private test inputs, generated outputs, and the isolated 17 GiB Koharu cache. No production containers, service configuration, queues, or production model caches were changed.
