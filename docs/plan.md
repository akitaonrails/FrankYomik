# Fix Webtoon Text Rendering and Detection

## Problem Summary

Three root causes across all reported panels:

**A. Text overflows bubble bbox** (002, 004, 012, 025, 028, 041, 042, 058)
English translations are longer than Korean originals. The renderer tries fonts down to size 10 but if text still doesn't fit, it renders beyond the bubble boundary. No clipping is enforced.

**B. Wrong bubble boundary on dark backgrounds** (053)
Canny edge contour detection was designed for dark outlines on light backgrounds. On dark backgrounds with light/gradient outlines, it finds wrong contours (smoke effects, gradient edges), producing oversized bboxes that cover character faces.

**C. Merged overlapping narrations** (059)
Two separate narration text blocks close vertically get clustered into one bubble. Translation renders as a single block between the two, which doesn't match either original position.

## Plan

### Fix 1: Clip text rendering to bubble bbox (files: `webtoon/processor.py`)

The biggest single improvement — prevents ALL overflow issues.

- Render text and background onto a **temporary RGBA canvas** (same size as image)
- After rendering, **composite only the bubble bbox region** onto the output image
- This guarantees no pixel outside the bubble bbox is ever modified by text rendering
- Also: lower minimum font from 10 to 8, reduce line spacing from 4px to 2px when text is tight
- The bg rect, font drawing — all clipped automatically since we only paste the bubble region

### Fix 2: Skip contour detection for dark backgrounds (files: `webtoon/bubble_detector.py`)

The user's insight: dark backgrounds need different treatment.

- In `find_bubble_boundary()`, compute bg luminance before attempting Level 3 contour detection
- When luminance < 0.5 (dark background), **skip contour detection entirely** — go straight to flood fill or padded bbox
- Contour detection with Canny edges is designed for dark-outline-on-light-bg; on dark backgrounds the edge patterns are inverted and unreliable
- This prevents 053-style wrong placements where smoke/gradient edges become the "bubble contour"

### Fix 3: Split overlapping narration clusters (files: `webtoon/bubble_detector.py`)

For panel 059 where two narration boxes overlap:

- After clustering, check if a cluster has a large **internal vertical gap** (> 60% of the gap between the topmost and bottommost detections)
- If so, split into sub-clusters at the gap point
- This handles the common webtoon pattern of two stacked narration boxes that are close but logically separate

### Out of scope (OCR misses: 030, 040, 051)

Partially improved by the earlier threshold tuning. Further improvement requires EasyOCR model changes or preprocessing that risks false positives. These are acceptable known limitations.
