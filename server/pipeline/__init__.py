"""Backend-neutral contracts for the image processing pipeline.

The package intentionally depends only on the Python standard library.  Model
backends and image libraries belong in adapters, which keeps document and
contract tests runnable without downloading model dependencies.
"""

from .document import (
    PAGE_DOCUMENT_SCHEMA_VERSION,
    BoundingBox,
    CleanupMetadata,
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
    "LEGACY_PIPELINE_REVISION",
    "PAGE_DOCUMENT_SCHEMA_VERSION",
    "PIPELINE_REVISION_PATTERN",
    "BoundingBox",
    "CleanupMetadata",
    "MaskReference",
    "OcrAlternative",
    "OcrResult",
    "PageDocument",
    "PageRegion",
    "PipelineRuntime",
    "Polygon",
    "Provenance",
    "RenderMetadata",
    "TransformResult",
    "TranslationResult",
    "UserEdit",
    "canonical_json",
    "canonical_json_bytes",
    "get_pipeline_revision",
    "make_region_id",
    "validate_pipeline_revision",
]
