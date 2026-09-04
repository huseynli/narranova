"""TTS provider adapters and contracts."""

from narranova.providers.base import (
    ProviderCapabilities,
    SynthesisCancelled,
    SynthesisRequest,
    TTSProvider,
)
from narranova.providers.openmoss import (
    OPENMOSS_DEFAULT_MAX_NEW_TOKENS,
    OPENMOSS_SAMPLING_FIELDS,
    OPENMOSS_STREAM_FRAME_OPTIONS,
    OpenMossConfig,
    OpenMossProvider,
    OpenMossSampling,
    normalize_openmoss_sampling,
    openmoss_performance_settings,
    openmoss_sampling_from_form,
)

__all__ = [
    "OPENMOSS_DEFAULT_MAX_NEW_TOKENS",
    "OPENMOSS_SAMPLING_FIELDS",
    "OPENMOSS_STREAM_FRAME_OPTIONS",
    "OpenMossConfig",
    "OpenMossProvider",
    "OpenMossSampling",
    "ProviderCapabilities",
    "SynthesisRequest",
    "SynthesisCancelled",
    "TTSProvider",
    "normalize_openmoss_sampling",
    "openmoss_performance_settings",
    "openmoss_sampling_from_form",
]
