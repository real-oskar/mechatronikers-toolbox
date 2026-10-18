"""Test bench: compares generated models with human-made models on test sets (docs/09).

Runs on the host, outside the sandboxes, because it holds ground truth. It is the only code
allowed to read reference STEP files.
"""

from mechatronikers.testbench.compare import CompareResult, compare

__all__ = ["CompareResult", "compare"]
