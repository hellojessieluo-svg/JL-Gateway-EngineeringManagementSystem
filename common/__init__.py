"""A shared toolbox for both gates: errors, naming, validation, addresses, config, constants,
the ledger's tables and its vocabulary.

Moved out of outbound_gate\\ on 2026-09-03. No gate's business logic lives here --
the intake gate and the issue gate both import it, and it imports neither gate.
"""

__all__ = [
    "constants", "config", "errors", "ledger_schema", "naming", "places", "validators", "vocab",
]
