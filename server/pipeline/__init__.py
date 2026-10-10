"""Backend-neutral contracts for the image processing pipeline.

The package intentionally depends only on the Python standard library.  Model
backends and image libraries belong in adapters, which keeps document and
contract tests runnable without downloading model dependencies.
"""

from .detection import (
    DETECTION_KINDS,
    DetectionKind,
    DetectionThresholds,
    SemanticDetection,
    adapt_rtdetr_detections,
    finalize_detections,
    legacy_processing_regions,
    link_text_to_balloons,
    validate_detection_collection,
)
from .document import (
    PAGE_DOCUMENT_SCHEMA_VERSION,
    BoundingBox,
    CleanupMetadata,
    ImageReference,
    MaskReference,
    OcrAlternative,
    OcrResult,
    PageDocument,
    PageRegion,
    Polygon,
    Provenance,
    RenderMetadata,
    TransformResult,
    TranslationResult,
    UserEdit,
    canonical_json,
    canonical_json_bytes,
    make_region_id,
)
from .protocols import (
    PipelineRuntime,
)
from .revision import (
    DEFAULT_PIPELINE_REVISION,
    LEGACY_PIPELINE_REVISION,
    PIPELINE_REVISION_PATTERN,
    get_pipeline_revision,
    validate_pipeline_revision,
)

__all__ = [
    "DEFAULT_PIPELINE_REVISION",
    "DETECTION_KINDS",
    "LEGACY_PIPELINE_REVISION",
    "PAGE_DOCUMENT_SCHEMA_VERSION",
    "PIPELINE_REVISION_PATTERN",
    "BoundingBox",
    "CleanupMetadata",
    "DetectionKind",
    "DetectionThresholds",
    "ImageReference",
    "MaskReference",
    "OcrAlternative",
    "OcrResult",
    "PageDocument",
    "PageRegion",
    "PipelineRuntime",
    "Polygon",
    "Provenance",
    "RenderMetadata",
    "SemanticDetection",
    "TransformResult",
    "TranslationResult",
    "UserEdit",
    "adapt_rtdetr_detections",
    "canonical_json",
    "canonical_json_bytes",
    "finalize_detections",
    "get_pipeline_revision",
    "legacy_processing_regions",
    "link_text_to_balloons",
    "make_region_id",
    "validate_detection_collection",
    "validate_pipeline_revision",
]
