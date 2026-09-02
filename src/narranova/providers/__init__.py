"""TTS provider adapters and contracts."""

from narranova.providers.base import ProviderCapabilities, SynthesisRequest, TTSProvider
from narranova.providers.openmoss import OpenMossConfig, OpenMossProvider

__all__ = [
    "OpenMossConfig",
    "OpenMossProvider",
    "ProviderCapabilities",
    "SynthesisRequest",
    "TTSProvider",
]
