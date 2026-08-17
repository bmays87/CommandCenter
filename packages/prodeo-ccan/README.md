# prodeo-ccan

The **Command Center Agent Node**: the per-machine daemon of the CCAN split
(ADR-0020). Installed from an installer downloaded from a Command Center
dashboard, it answers **only** that Command Center — the hub's certificate is
baked into the installer and the node's TLS listener requires it as a client
certificate on every connection (ADR-0025).

This package is currently the pairing surface (`/ccan/v1/pair`,
`/ccan/v1/health`). The machine-bound capabilities (adapters, agent launch,
browsing, host probes) migrate here in the next Phase 6 workstream.

Normally you never install this by hand: download the installer from the
Command Center dashboard (Add Machine → Download CCAN Installer), unpack it
on the target machine, and run `python install.py`.
