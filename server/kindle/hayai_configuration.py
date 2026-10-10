# Vendored from JustANormalTinkerer/hayai-ocr-v2 at
# 4a4ce477c9a8841f208b94e1d9ed5c0938965e05 (Apache-2.0).

from transformers import PretrainedConfig, Siglip2VisionConfig


def pinned_siglip2_vision_config() -> Siglip2VisionConfig:
    """Effective config at google SigLIP2 revision b53b807d."""
    return Siglip2VisionConfig(
        attention_dropout=0.0,
        hidden_act="gelu_pytorch_tanh",
        hidden_size=768,
        intermediate_size=3072,
        layer_norm_eps=1e-6,
        num_attention_heads=12,
        num_channels=3,
        num_hidden_layers=12,
        num_patches=256,
        patch_size=16,
    )

class HayaiConfig(PretrainedConfig):
    model_type = "hayai"
    def __init__(self, vocab_size=0, d_model=512, d_vision=768, d_ffn=2048, n_layers=12, **kwargs):
        self.vocab_size = vocab_size
        self.d_model = d_model
        self.d_vision = d_vision
        self.d_ffn = d_ffn
        self.n_layers = n_layers
        super().__init__(**kwargs)
