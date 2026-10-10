# Cleanup and inpainting policy

The cleanup stage removes source lettering before rendering. Its cache and
provenance identity covers the shared planner, pipeline adapter, processor,
configuration, and pinned model identity.

## Decision flow

1. Build an OCR cleanup mask from retained region geometry. Expand it by the
   configured padding and dilation, then clip it to the eroded semantic mask
   when one exists. All coordinates are clipped to page bounds.
2. If the retained interior has at most 8 levels of channel standard deviation
   and 28 levels of channel range, fill the cleanup mask with its median color.
   This path does not load a model.
3. Otherwise, split connected mask components into deterministic top-to-bottom,
   left-to-right context tiles and run the pinned Frank LaMa adapter.
4. FLUX.2 Klein is reserved for difficult artwork. Its audited code and weights
   are pinned, but it remains disabled until the router is observably
   deterministic, the two-replica/OOM operational gate passes, and incomplete
   training-data disclosure is resolved. No released path downloads it.

The webtoon adapter estimates glyph pixels inside each OCR rectangle by color
distance from the detected background and falls back to the rectangle when the
estimate is empty. The shared API also accepts engine-supplied pixel masks.
Manga artwork currently uses its detector rectangle because that path has no
reliable background color or glyph mask.

`PageDocument.region.mask` continues to reference the retained semantic mask.
The removal mask is computed transiently from OCR geometry and is not yet stored
in the cache artifact store, so `cleanup.mask` is deliberately omitted rather
than pointing at the broader balloon mask.

## Resource budget and failure behavior

Production defaults allow at most 16 tiles, each at most 1024 by 1024 pixels,
at most 1,048,576 pixels per tile and 40,000,000 pixels per page mask, with 20
pixels of context and a 30 second page cleanup budget. Model work starts
only when at least 512 MiB of host memory and, when CUDA is active, 512 MiB of
GPU memory are available. A persistent supervised process owns the warmed LaMa
model. Large image and mask payloads cross the process boundary through bounded
temporary array artifacts. Only capped control datagrams use IPC, with
nonblocking parent reads and writes tied to the same deadline. The deadline
includes artifact staging, child startup/model load, inference-lock wait, IPC,
artifact reads, and inference; the parent terminates the process when it expires.
Tile seeds start at zero and increase by tile order.

An oversized plan, unavailable model, memory guard, timeout, OOM, output-size
mismatch, or expected backend error restores the last safe cleanup and resets
the supervised model process and accelerator cache. The adapter then
applies a mask-bounded fill using the detected background color. Cleanup
failure therefore does not fail the page. Pixels
outside the expanded cleanup mask are copied from the current target unchanged.

## Pipeline coverage

- Webtoon dialogue uses uniform fill or bounded LaMa tiles and persists the
  selected method, including a `:degraded` suffix when recovery was needed.
- Manga dialogue and furigana retain Frank's existing stroke clearing. Optional
  manga artwork inpainting uses the same bounded tile and resource policy; it
  remains disabled by default.
- Book pipelines retain their gutter rendering and do not load an inpainting
  model.

The controlled eight-case quality comparison and Strix Halo memory and latency
gate must still pass before a release enables any additional learned backend.
