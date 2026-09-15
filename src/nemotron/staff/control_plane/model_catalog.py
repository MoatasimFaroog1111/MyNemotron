from __future__ import annotations

from nemotron.staff.domain.model_routing import ModelProfile, ModelTier


NEMOTRON_35_LIGHTNING_MODEL_ID = "nvidia/nemotron-3.5-lightning-30b-a3b"


def nemotron_35_lightning_worker(*, arabic_validated: bool = False) -> ModelProfile:
    """Return the NVIDIA Nemotron 3.5 Lightning candidate profile.

    Arabic stays disabled until the project's own Arabic accounting benchmark passes.
    The 1M context value follows NVIDIA's published model card; runtime deployments
    may impose a smaller operational limit and should override the profile accordingly.
    """

    return ModelProfile(
        model_id=NEMOTRON_35_LIGHTNING_MODEL_ID,
        provider="nvidia",
        tier=ModelTier.WORKER,
        max_context_tokens=1_000_000,
        arabic_validated=arabic_validated,
    )
