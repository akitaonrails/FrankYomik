# Evaluation assets

This directory is deliberately isolated from application fixtures and ignored by Git.

- `pages/`: full manga or webtoon pages used for detection.
- `ocr/`: ground-truth crops used to compare recognizers without detector effects.
- `inpainting/`: source crops and binary masks used to compare cleanup models.

Run `python scripts/benchmark.py prepare --seed-docs` to copy the existing repository manga samples into `pages/`. Add private Kindle and Naver samples with `prepare --add-pages DIR --kind manga|webtoon`.

Do not commit purchased manga, authenticated reader captures, generated model output, or masks derived from those pages.
