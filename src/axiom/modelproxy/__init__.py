"""The model proxy: the only holder of the Anthropic key (ADR-015).

A sandbox gets a run token that works only here, only for its model, only within its budget, and
only until it expires. The proxy swaps in the real key, and counts what was actually used.
"""
