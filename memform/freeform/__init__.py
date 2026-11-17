"""Free-form conversion and cascaded adjudication."""

from memform.freeform.evaluator import (
    evaluate_freeform_response,
    is_freeform_case,
    validate_freeform_case_for_evaluation,
)

__all__ = [
    "evaluate_freeform_response",
    "is_freeform_case",
    "validate_freeform_case_for_evaluation",
]


def __getattr__(name: str):
    """Load construction helpers only in distributions that include the builder."""
    if name in {"build_freeform_case", "validate_freeform_case"}:
        from importlib import import_module

        return getattr(import_module("memform.freeform.build"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
