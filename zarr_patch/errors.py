"""Exception types for the patch engine."""


class PatchValidationError(ValueError):
    """Raised when an operation or the source array fails pre-flight checks.

    The whole batch is rejected before any modified chunk is written, so no
    partial output is ever published.
    """
