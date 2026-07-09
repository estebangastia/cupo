"""Cupo — usage limits, feature gates, and metering for AI products.

This is a name-reservation release. The project is in design phase:
the full API specification is public and open for feedback at

    https://github.com/estebangastia/cupo

v0.1 (Python SDK, embedded mode, plans-as-code, FastAPI middleware,
token-aware Anthropic/OpenAI wrappers) is in development.
"""

__version__ = "0.0.1"


def _not_yet(*_args, **_kwargs):
    raise NotImplementedError(
        "Cupo is in design phase. Follow progress and leave feedback at "
        "https://github.com/estebangastia/cupo"
    )


class Cupo:
    """Placeholder for the Cupo client. See module docstring."""

    def __init__(self, *args, **kwargs):
        _not_yet()
