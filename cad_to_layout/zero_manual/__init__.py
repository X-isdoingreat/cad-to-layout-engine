"""Zero-manual DWG reading: equipment list and warehouse geometry for any drawing.

One flow for every drawing (acceptance target
``docs/acceptance_evidence/read_generalization_warehouse_geometry_20260929``):
no code branch, rule file, configuration or block-name mapping written for a
particular drawing.  Which block or label is which equipment type is decided by
an automatic mapper (offline or LLM); every decision carries evidence that points
at concrete source objects and is re-derived by a checker that never calls an
LLM.  Anything uncertain is listed as unrecognized instead of guessed.

This package must not import the per-drawing legacy modules; the guardrail test
``tests/test_zero_manual_guardrails.py`` enforces that.
"""
