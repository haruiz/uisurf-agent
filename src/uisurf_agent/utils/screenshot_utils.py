from __future__ import annotations

"""Helpers for scaling screenshots before they are sent to the model."""

import io
import os

from PIL import Image


DEFAULT_OBSERVATION_SCALE = 1.0
GLOBAL_OBSERVATION_SCALE_ENV = "OBSERVATION_SCALE"
_LANCZOS = (
    Image.Resampling.LANCZOS
    if hasattr(Image, "Resampling")
    else Image.LANCZOS
)


def validate_observation_scale(scale: float) -> float:
    """Validate the screenshot scale factor used for model observations."""
    if not 0 < scale <= 1:
        raise ValueError(
            "observation_scale must be greater than 0 and less than or equal to 1."
        )
    return scale


def resolve_observation_scale(value: float | None, agent_env_var: str) -> float:
    """Resolve screenshot scale from an explicit value or environment variables."""
    if value is not None:
        return validate_observation_scale(value)

    raw_value = os.environ.get(agent_env_var)
    source = agent_env_var
    if raw_value is None:
        raw_value = os.environ.get(GLOBAL_OBSERVATION_SCALE_ENV)
        source = GLOBAL_OBSERVATION_SCALE_ENV

    if raw_value is None:
        return DEFAULT_OBSERVATION_SCALE

    try:
        return validate_observation_scale(float(raw_value))
    except ValueError as exc:
        raise ValueError(
            f"{source} must be a float greater than 0 and less than or equal to 1."
        ) from exc


def scale_image(image: Image.Image, observation_scale: float) -> Image.Image:
    """Resize an image while preserving aspect ratio."""
    observation_scale = validate_observation_scale(observation_scale)
    if observation_scale == DEFAULT_OBSERVATION_SCALE:
        return image

    target_size = (
        max(1, int(round(image.width * observation_scale))),
        max(1, int(round(image.height * observation_scale))),
    )
    return image.resize(target_size, _LANCZOS)


def resize_png_bytes(png_bytes: bytes, observation_scale: float) -> bytes:
    """Resize PNG bytes while keeping PNG encoding for model input."""
    observation_scale = validate_observation_scale(observation_scale)
    if observation_scale == DEFAULT_OBSERVATION_SCALE:
        return png_bytes

    with Image.open(io.BytesIO(png_bytes)) as image:
        resized_image = scale_image(image, observation_scale)
        buffer = io.BytesIO()
        resized_image.save(buffer, format="PNG")
        return buffer.getvalue()
