# Annotation rules

Annotations establish the ground truth used to decide whether a model is better. Do not derive them by selecting whichever system output looks more plausible.

## Detection

- Use page pixel coordinates `[left, top, right, bottom]`.
- Draw separate masks for overlapping instances.
- Label visible text strokes as `text` or `onomatopoeia`; label the surrounding semantic balloon separately as `bubble`.
- Use `onomatopoeia` for sound effects integrated into artwork.
- Use `ignore: true` when the class or visible boundary cannot be resolved confidently.
- Have a second person verify the held-out evaluation subset.

## OCR

- Preserve the characters and punctuation a fluent reader sees.
- Do not preserve visual line breaks unless they are semantically meaningful.
- Mark an entry verified only after uncertain characters have been resolved.
- Keep ordinary dialogue and difficult typography represented in the dataset IDs or case notes.

## Inpainting

- Masks describe source lettering to remove, not the entire balloon.
- Keep balloon borders and source artwork outside the mask.
- A clean reference must be the actual underlying image or a controlled synthetic source, never another model's output.
- Real-page output is judged blind because the hidden original artwork is unknown.

