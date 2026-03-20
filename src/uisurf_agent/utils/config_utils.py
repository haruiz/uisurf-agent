from __future__ import annotations

"""Helpers for resolving runtime configuration from args and environment."""

import os


def resolve_bool_config(
    value: bool | None,
    env_var: str,
    *,
    default: bool = False,
) -> bool:
    """Resolve a boolean config value from an explicit value or environment."""
    if value is not None:
        return value

    raw_value = os.environ.get(env_var)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(
        f"{env_var} must be one of: true, false, 1, 0, yes, no, on, off."
    )


def resolve_int_config(
    value: int | None,
    env_var: str,
    *,
    default: int,
    minimum: int | None = None,
) -> int:
    """Resolve an integer config value from an explicit value or environment."""
    resolved = value
    source = "argument"
    if resolved is None:
        raw_value = os.environ.get(env_var)
        source = env_var
        if raw_value is None:
            resolved = default
            source = "default"
        else:
            try:
                resolved = int(raw_value)
            except ValueError as exc:
                raise ValueError(f"{env_var} must be an integer.") from exc

    if minimum is not None and resolved < minimum:
        if source == "argument":
            raise ValueError(f"value must be greater than or equal to {minimum}.")
        raise ValueError(f"{env_var} must be greater than or equal to {minimum}.")

    return resolved
