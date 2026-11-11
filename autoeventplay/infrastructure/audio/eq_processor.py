"""Real-time EQ processing for one channel (stateful second-order sections)."""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("aep.eq")

try:
    from scipy.signal import sosfilt  # type: ignore
except Exception:  # pragma: no cover - depends on the machine
    sosfilt = None
    log.warning("scipy not available – equalizers are bypassed")


class EqState:
    """Holds coefficients and filter memory; coefficient updates keep the memory (no clicks)."""

    def __init__(self) -> None:
        self.sos: np.ndarray | None = None
        self.zi: np.ndarray | None = None

    def update(self, sos: np.ndarray | None) -> None:
        if sos is None:
            self.sos, self.zi = None, None
            return
        if self.zi is None or self.zi.shape[0] != sos.shape[0]:
            self.zi = np.zeros((sos.shape[0], 2))
        self.sos = sos

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.sos is None or sosfilt is None:
            return x
        y, self.zi = sosfilt(self.sos, x, zi=self.zi)
        return y.astype(np.float32, copy=False)
