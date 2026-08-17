"""Prodeo Command Center Agent Node — the per-machine daemon (ADR-0020).

Answers only the Command Center whose installer set it up: the parent hub's
certificate is this node's entire TLS trust store, required as a client
certificate on every connection (ADR-0025).
"""

__version__ = "0.1.0"
