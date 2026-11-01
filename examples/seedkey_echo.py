"""Example Seed&Key plugin — key equals seed (for open/sim ECUs only).

Point the UI «Seed&Key» path at this file, enable Auto unlock, then Connect.

Real ECUs need a vendor algorithm (or a matching SeedNKey.dll).
"""


def compute_key(resource: int, seed: bytes) -> bytes:
    _ = resource
    return bytes(seed)
