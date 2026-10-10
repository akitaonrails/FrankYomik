"""Backend-neutral semantic detection results and deterministic post-processing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Literal

from .document import BoundingBox, Polygon, Provenance

DetectionKind = Literal["text", "sfx", "balloon", "panel"]
DETECTION_KINDS = frozenset({"text", "sfx", "balloon", "panel"})


@dataclass(frozen=True, slots=True)
class DetectionThresholds:
    """Minimum scores and same-class suppression used by detector adapters."""

    text: float = 0.35
    sfx: float = 0.60
    balloon: float = 0.35
    panel: float = 0.50
    duplicate_overlap: float = 0.50

    def for_kind(self, kind: DetectionKind) -> float:
        return getattr(self, kind)


@dataclass(frozen=True, slots=True)
class SemanticDetection:
    """One detector observation before OCR or rendering.

    ``id`` and ``parent_id`` are stable for identical detector output. Masks may
    be backend-native in memory; PageDocument stores a reproducible reference
    and polygon rather than serializing the pixel array into JSON.
    """

    id: str
    kind: DetectionKind
    bbox: BoundingBox
    confidence: float
    raw_label: str
    provenance: Provenance
    polygon: Polygon | None = None
    mask: object | None = None
    parent_id: str | None = None
    reading_order: int = 0

    def __post_init__(self) -> None:
        if self.kind not in DETECTION_KINDS:
            raise ValueError(f"unsupported detection kind: {self.kind}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("detection confidence must be between 0 and 1")
        if not self.id or not self.raw_label:
            raise ValueError("detection id and raw label must not be empty")
        if self.parent_id == self.id:
            raise ValueError("a detection cannot be its own parent")
        if self.reading_order < 0:
            raise ValueError("reading order must be non-negative")


RTDETR_LABEL_KIND: dict[str, DetectionKind] = {
    "bubble": "balloon",
    "text_bubble": "text",
    "text_free": "sfx",
    "panel": "panel",
    "frame": "panel",
}


def adapt_rtdetr_detections(
    detections: list[dict[str, object]],
    provenance: Provenance,
    *,
    thresholds: DetectionThresholds | None = None,
    reading_direction: Literal["manga", "vertical"] = "manga",
) -> list[SemanticDetection]:
    """Normalize RT-DETR output without collapsing text into balloons.

    Suppression is deliberately class-aware. A ``bubble`` and its
    ``text_bubble`` may overlap almost completely and still carry distinct
    geometry and a useful parent relationship.
    """
    thresholds = thresholds or DetectionThresholds()
    candidates: list[tuple[DetectionKind, BoundingBox, float, str]] = []
    for raw in detections:
        raw_label = str(raw.get("label") or raw.get("raw_label") or "")
        kind = RTDETR_LABEL_KIND.get(raw_label)
        if kind is None:
            continue
        confidence = float(raw.get("score", 0.0))
        if confidence < thresholds.for_kind(kind):
            continue
        box = BoundingBox.from_value(raw["bbox"])  # type: ignore[arg-type]
        candidates.append((kind, box, confidence, raw_label))

    # Assign collision suffixes only after a total semantic sort. Model output
    # tensor order is not an identity and may vary across runtime backends.
    candidates.sort(key=lambda item: (
        item[0], *item[1].as_list(), item[3], -item[2],
    ))
    normalized: list[SemanticDetection] = []
    collisions: dict[str, int] = {}
    for kind, box, confidence, raw_label in candidates:
        base = _detection_id(kind, box, raw_label)
        occurrence = collisions.get(base, 0) + 1
        collisions[base] = occurrence
        detection_id = base if occurrence == 1 else f"{base}-{occurrence}"
        normalized.append(SemanticDetection(
            id=detection_id,
            kind=kind,
            bbox=box,
            confidence=confidence,
            raw_label=raw_label,
            provenance=provenance,
        ))

    return finalize_detections(
        normalized,
        duplicate_overlap=thresholds.duplicate_overlap,
        reading_direction=reading_direction,
    )


def finalize_detections(
    detections: list[SemanticDetection],
    *,
    duplicate_overlap: float = 0.50,
    reading_direction: Literal["manga", "vertical"] = "manga",
) -> list[SemanticDetection]:
    """Deduplicate, relink, and order page-space semantic detections."""
    unlinked = [replace(item, parent_id=None) for item in detections]
    normalized = _deduplicate_by_class(unlinked, duplicate_overlap)
    normalized = link_text_to_balloons(normalized)
    ordered = (
        _order_manga_detections(normalized)
        if reading_direction == "manga"
        else sorted(normalized, key=_vertical_reading_key)
    )
    return [replace(item, reading_order=index) for index, item in enumerate(ordered)]


def validate_detection_collection(detections: list[SemanticDetection]) -> None:
    """Reject ambiguous identities and relationships at the stage boundary."""
    ids = {item.id for item in detections}
    if len(ids) != len(detections):
        raise ValueError("semantic detection ids must be unique")
    orders = {item.reading_order for item in detections}
    if len(orders) != len(detections):
        raise ValueError("semantic detection reading orders must be unique")
    by_id = {item.id: item for item in detections}
    for item in detections:
        if item.parent_id is None:
            continue
        parent = by_id.get(item.parent_id)
        if parent is None:
            raise ValueError(f"semantic detection {item.id!r} has an unknown parent")
        if item.kind != "text" or parent.kind != "balloon":
            raise ValueError("only text detections may have balloon parents")


def link_text_to_balloons(
    detections: list[SemanticDetection],
) -> list[SemanticDetection]:
    """Attach each text region to the best containing/overlapping balloon."""
    balloons = [item for item in detections if item.kind == "balloon"]
    linked: list[SemanticDetection] = []
    for item in detections:
        if item.kind != "text":
            linked.append(item)
            continue
        candidates: list[tuple[float, int, str, SemanticDetection]] = []
        for balloon in balloons:
            overlap = _overlap_fraction(item.bbox, balloon.bbox)
            center_inside = _contains_center(balloon.bbox, item.bbox)
            if not center_inside and overlap < 0.50:
                continue
            # Prefer full containment, then overlap, then the smallest balloon.
            area = balloon.bbox.width * balloon.bbox.height
            score = overlap + (1.0 if center_inside else 0.0)
            candidates.append((-score, area, balloon.id, balloon))
        if candidates:
            parent = min(candidates)[3]
            linked.append(replace(item, parent_id=parent.id))
        else:
            linked.append(item)
    return linked


def legacy_processing_regions(
    detections: list[SemanticDetection],
) -> list[dict[str, object]]:
    """Project semantic results onto the current OCR/rendering input shape."""
    children_by_parent = {
        item.parent_id for item in detections
        if item.kind == "text" and item.parent_id is not None
    }
    result: list[dict[str, object]] = []
    for item in detections:
        if item.kind == "panel" or item.kind == "text" and item.parent_id is not None:
            continue
        if item.kind == "balloon" or item.kind == "text":
            legacy_type = "speech_bubble"
            is_artwork = False
        else:
            legacy_type = "artwork_text"
            is_artwork = True
        # A balloon is retained even with a linked child: its boundary is the
        # safe cleanup/rendering region used by Frank's existing pipeline.
        result.append({
            "bbox": tuple(item.bbox.as_list()),
            "type": legacy_type,
            "score": item.confidence,
            "is_artwork": is_artwork,
            "raw_label": item.raw_label,
            "semantic_id": item.id,
            "text_child_ids": sorted(
                child.id for child in detections if child.parent_id == item.id
            ) if item.id in children_by_parent else [],
        })
    return result


def _detection_id(kind: str, bbox: BoundingBox, raw_label: str) -> str:
    material = f"{kind}:{','.join(map(str, bbox.as_list()))}:{raw_label}".encode()
    return f"d_{hashlib.sha256(material).hexdigest()[:16]}"


def _deduplicate_by_class(
    detections: list[SemanticDetection], threshold: float,
) -> list[SemanticDetection]:
    kept: list[SemanticDetection] = []
    for item in sorted(
        detections,
        key=lambda value: (-value.confidence, value.id),
    ):
        if any(
            existing.kind == item.kind
            and max(
                _overlap_fraction(item.bbox, existing.bbox),
                _overlap_fraction(existing.bbox, item.bbox),
            ) > threshold
            for existing in kept
        ):
            continue
        kept.append(item)
    return kept


def _overlap_fraction(a: BoundingBox, b: BoundingBox) -> float:
    width = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
    height = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    return width * height / (a.width * a.height)


def _contains_center(container: BoundingBox, item: BoundingBox) -> bool:
    center_x = (item.x1 + item.x2) / 2
    center_y = (item.y1 + item.y2) / 2
    return (
        container.x1 <= center_x < container.x2
        and container.y1 <= center_y < container.y2
    )


def _vertical_reading_key(item: SemanticDetection) -> tuple[object, ...]:
    kind_order = {"panel": 0, "balloon": 1, "text": 2, "sfx": 3}
    return (item.bbox.y1, item.bbox.x1, kind_order[item.kind], item.id)


def _order_manga_detections(
    detections: list[SemanticDetection],
) -> list[SemanticDetection]:
    """Order panel groups and visual rows top-to-bottom, right-to-left."""
    panels = [item for item in detections if item.kind == "panel"]
    content = [item for item in detections if item.kind != "panel"]
    if not panels:
        return _order_visual_rows(content)

    ordered: list[SemanticDetection] = []
    assigned: set[str] = set()
    for panel in _order_visual_rows(panels):
        ordered.append(panel)
        members = [
            item for item in content
            if item.id not in assigned and _contains_center(panel.bbox, item.bbox)
        ]
        # Overlapping panels choose the smallest enclosing panel, then stable ID.
        members = [
            item for item in members
            if panel.id == min(
                (
                    candidate for candidate in panels
                    if _contains_center(candidate.bbox, item.bbox)
                ),
                key=lambda candidate: (
                    candidate.bbox.width * candidate.bbox.height,
                    candidate.id,
                ),
            ).id
        ]
        ordered.extend(_order_visual_rows(members))
        assigned.update(item.id for item in members)
    ordered.extend(_order_visual_rows([
        item for item in content if item.id not in assigned
    ]))
    return ordered


def _order_visual_rows(
    detections: list[SemanticDetection],
) -> list[SemanticDetection]:
    """Build input-order-independent rows from overlapping or close intervals."""
    if len(detections) <= 1:
        return list(detections)
    remaining = {item.id: item for item in detections}
    rows: list[list[SemanticDetection]] = []
    while remaining:
        seed_id = min(
            remaining,
            key=lambda item_id: (
                remaining[item_id].bbox.y1,
                -remaining[item_id].bbox.x2,
                item_id,
            ),
        )
        row_ids = {seed_id}
        changed = True
        while changed:
            changed = False
            for item_id, item in remaining.items():
                if item_id in row_ids:
                    continue
                if any(_same_visual_row(item, remaining[member]) for member in row_ids):
                    row_ids.add(item_id)
                    changed = True
        rows.append([remaining.pop(item_id) for item_id in sorted(row_ids)])

    rows.sort(key=lambda row: (
        min(item.bbox.y1 for item in row),
        min(item.bbox.y2 for item in row),
        min(item.id for item in row),
    ))
    ordered: list[SemanticDetection] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda item: (
            -item.bbox.x2, item.bbox.y1, item.kind, item.id,
        )))
    return ordered


def _same_visual_row(left: SemanticDetection, right: SemanticDetection) -> bool:
    overlap = min(left.bbox.y2, right.bbox.y2) - max(left.bbox.y1, right.bbox.y1)
    if overlap > 0:
        return True
    gap = max(left.bbox.y1, right.bbox.y1) - min(left.bbox.y2, right.bbox.y2)
    stagger_tolerance = max(4, min(left.bbox.height, right.bbox.height) // 4)
    return gap <= stagger_tolerance
