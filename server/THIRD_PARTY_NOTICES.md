# Third-party notices

## Hayai OCR v2.1 model implementation

Frank Yomik contains a reviewed local adaptation of `configuration_hayai.py`
and `modeling_hayai.py` from
`JustANormalTinkerer/hayai-ocr-v2`, revision
`4a4ce477c9a8841f208b94e1d9ed5c0938965e05`.

Copyright belongs to the Hayai OCR contributors. The upstream code and model
repository declare the Apache License 2.0. A copy is included at
[`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt).

The local adaptation removes runtime remote-code loading, supplies the pinned
SigLIP2 NaFlex configuration locally, and requires SHA-256 verification of all
model artifacts before parsing. Hayai model weights are not distributed or
automatically enabled because their training-image provenance is not yet
sufficient for Frank's release policy.

## SigLIP2 configuration

The Hayai adapter contains the effective vision and preprocessing configuration
from `google/siglip2-base-patch16-naflex`, revision
`b53b807d3a2d5e2b3911292f2d69e5341cdc064c`. Google declares that repository
under the Apache License 2.0.
