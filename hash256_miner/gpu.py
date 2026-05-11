"""GPU miner backend using OpenCL.

Drives the keccak256_miner.cl kernel: streams batches of nonces, polls the
result buffer, and yields solutions back to the orchestrator.

Why OpenCL and not CUDA: OpenCL runs on NVIDIA, AMD, Intel and Apple
silicon out of the box, which matches the project's "no special hardware
required" ethos. If a user has a CUDA-only setup they can install
pocl or the NVIDIA OpenCL runtime that ships with the driver — no code
change here.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import pyopencl as cl

log = logging.getLogger(__name__)


KERNEL_PATH = Path(__file__).resolve().parent.parent / "kernels" / "keccak256_miner.cl"


@dataclass
class GpuStats:
    hashes: int = 0
    started_at: float = field(default_factory=time.time)
    last_batch_hashes: int = 0
    last_batch_seconds: float = 0.0

    def hashrate(self) -> float:
        elapsed = max(time.time() - self.started_at, 1e-6)
        return self.hashes / elapsed

    def last_hashrate(self) -> float:
        if self.last_batch_seconds <= 0:
            return 0.0
        return self.last_batch_hashes / self.last_batch_seconds


@dataclass
class Solution:
    nonce: int          # full 256-bit nonce as Python int
    challenge: bytes    # 32 bytes — the challenge the solution was found against
    digest: bytes       # 32 bytes — keccak256(challenge ‖ nonce)
    target: int         # the target the solution beat


def list_devices() -> list[tuple[int, int, str, str]]:
    """Enumerate (platform_idx, device_idx, platform_name, device_name)."""
    out: list[tuple[int, int, str, str]] = []
    for p_idx, platform in enumerate(cl.get_platforms()):
        for d_idx, device in enumerate(platform.get_devices()):
            out.append((p_idx, d_idx, platform.name.strip(), device.name.strip()))
    return out


def pick_device(platform_idx: Optional[int], device_idx: Optional[int]) -> cl.Device:
    platforms = cl.get_platforms()
    if not platforms:
        raise RuntimeError("No OpenCL platforms detected — install GPU drivers.")

    # Prefer GPUs over CPUs when auto-selecting.
    if platform_idx is None and device_idx is None:
        for platform in platforms:
            gpus = platform.get_devices(device_type=cl.device_type.GPU)
            if gpus:
                return gpus[0]
        # Fall back to whatever's there.
        return platforms[0].get_devices()[0]

    p_idx = platform_idx if platform_idx is not None else 0
    d_idx = device_idx if device_idx is not None else 0
    return platforms[p_idx].get_devices()[d_idx]


class GpuMiner:
    """One OpenCL context tied to one device. Reusable across jobs."""

    def __init__(
        self,
        device: cl.Device,
        *,
        local_size: int = 256,
        global_size: int = 1 << 22,      # 4M work-items per kernel launch
    ):
        self.device = device
        self.context = cl.Context([device])
        self.queue = cl.CommandQueue(self.context)
        self.local_size = local_size
        self.global_size = global_size
        self.stats = GpuStats()

        kernel_src = KERNEL_PATH.read_text()
        log.debug("Building OpenCL program for %s ...", device.name.strip())
        self.program = cl.Program(self.context, kernel_src).build()
        self.kernel = self.program.mine

        # Allocate device buffers once. The challenge / target / result buffers
        # are tiny; we just refresh their contents per job.
        mf = cl.mem_flags
        self.buf_challenge = cl.Buffer(self.context, mf.READ_ONLY, 32)
        self.buf_target = cl.Buffer(self.context, mf.READ_ONLY, 32)
        # Result: [low64, mid64, high64, found_flag, top64]  -> 5 * 8 = 40 bytes
        self.buf_result = cl.Buffer(self.context, mf.READ_WRITE, 5 * 8)
        self._result_host = np.zeros(5, dtype=np.uint64)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _challenge_to_le_words(challenge: bytes) -> np.ndarray:
        """Pack 32 challenge bytes into 4 little-endian uint64s.

        Keccak absorbs bytes in little-endian word order, so the first 8
        bytes of the challenge become the low 8 bytes of the first uint64.
        """
        if len(challenge) != 32:
            raise ValueError("challenge must be 32 bytes")
        return np.frombuffer(challenge, dtype=np.uint64).astype(np.uint64).copy()

    @staticmethod
    def _target_to_be_words(target: int) -> np.ndarray:
        """Pack a uint256 target into 4 big-endian uint64s, MSB first."""
        if not (0 <= target < 1 << 256):
            raise ValueError("target out of uint256 range")
        be = target.to_bytes(32, "big")
        return np.array([
            int.from_bytes(be[0:8],   "big"),
            int.from_bytes(be[8:16],  "big"),
            int.from_bytes(be[16:24], "big"),
            int.from_bytes(be[24:32], "big"),
        ], dtype=np.uint64)

    @staticmethod
    def _split_nonce_to_be_words(nonce: int) -> tuple[int, int, int, int]:
        """Split a 256-bit nonce into 4 big-endian uint64 words.

        Word 0 = most-significant 64 bits, word 3 = least-significant.
        """
        be = nonce.to_bytes(32, "big")
        return (
            int.from_bytes(be[0:8],   "big"),
            int.from_bytes(be[8:16],  "big"),
            int.from_bytes(be[16:24], "big"),
            int.from_bytes(be[24:32], "big"),
        )

    # ------------------------------------------------------------------
    # Main mining loop
    # ------------------------------------------------------------------

    def mine(
        self,
        challenge: bytes,
        target: int,
        *,
        max_seconds: Optional[float] = None,
        poll_interval_batches: int = 1,
        start_nonce: Optional[int] = None,
    ) -> Iterator[Solution]:
        """Stream solutions for (challenge, target).

        Yields a `Solution` whenever the GPU finds a winning nonce. Stops
        when `max_seconds` elapses (if set) or the caller breaks out.

        The kernel varies the low 32 bits of the nonce per work item. The
        host bumps `start_nonce` by `global_size` between launches, so
        the next kernel call covers the next contiguous slice of nonce
        space. We pick a random 224-bit start_nonce upfront so two miners
        on the same address don't collide on tested ranges.
        """
        if start_nonce is None:
            # 192 random bits, leaving bottom 64 bits for batch advancement.
            # Each kernel launch uses the bottom 32 bits as `gid` and the
            # next 32 bits as the batch counter, giving us 2^32 batches per
            # random base before we ever wrap.
            start_nonce = secrets.randbits(192) << 64

        challenge_words = self._challenge_to_le_words(challenge)
        target_words = self._target_to_be_words(target)

        cl.enqueue_copy(self.queue, self.buf_challenge, challenge_words)
        cl.enqueue_copy(self.queue, self.buf_target, target_words)

        deadline = (time.time() + max_seconds) if max_seconds else None
        batch_index = 0   # increments per kernel launch; placed in bits 32..63

        while True:
            if deadline is not None and time.time() >= deadline:
                return

            # Each batch uses:
            #   bits 0..31    : gid (kernel-supplied, varies per work item)
            #   bits 32..63   : batch_index (host-supplied, varies per launch)
            #   bits 64..255  : random base from start_nonce
            current_nonce = (start_nonce | ((batch_index & 0xFFFFFFFF) << 32)) & ((1 << 256) - 1)

            # Reset result buffer.
            self._result_host[:] = 0
            cl.enqueue_copy(self.queue, self.buf_result, self._result_host)

            w0, w1, w2, w3 = self._split_nonce_to_be_words(current_nonce)
            # The kernel ORs `gid` (a uint32) into the bottom 32 bits of w3.
            # We pre-zero those bits so the OR is unambiguous.
            w3_base = w3 & 0xFFFFFFFF_00000000

            batch_start = time.time()
            self.kernel(
                self.queue,
                (self.global_size,),
                (self.local_size,),
                self.buf_challenge,
                self.buf_target,
                np.uint64(w0),
                np.uint64(w1),
                np.uint64(w2),
                np.uint64(w3_base),
                self.buf_result,
            )
            cl.enqueue_copy(self.queue, self._result_host, self.buf_result)
            self.queue.finish()
            batch_seconds = time.time() - batch_start

            self.stats.hashes += self.global_size
            self.stats.last_batch_hashes = self.global_size
            self.stats.last_batch_seconds = batch_seconds

            found = int(self._result_host[3])
            if found:
                # Reconstruct full 256-bit nonce from the result buffer.
                tw0 = int(self._result_host[4])   # top 64 bits
                tw1 = int(self._result_host[2])
                tw2 = int(self._result_host[1])
                tw3 = int(self._result_host[0])   # low 64 bits (incl. gid)
                nonce_bytes = (
                    tw0.to_bytes(8, "big")
                    + tw1.to_bytes(8, "big")
                    + tw2.to_bytes(8, "big")
                    + tw3.to_bytes(8, "big")
                )
                nonce_int = int.from_bytes(nonce_bytes, "big")

                # CPU-verify before yielding — protects us from a buggy kernel
                # accidentally claiming victory on a non-solution.
                from eth_utils import keccak  # local import to avoid cycle
                digest = keccak(challenge + nonce_int.to_bytes(32, "big"))
                if int.from_bytes(digest, "big") < target:
                    yield Solution(
                        nonce=nonce_int,
                        challenge=challenge,
                        digest=digest,
                        target=target,
                    )
                else:
                    log.warning(
                        "GPU reported a false positive nonce — verify failed. "
                        "Continuing."
                    )

            # Advance to the next batch. After 2^32 batches we wrap and
            # implicitly start chewing through a new random base — vanishingly
            # unlikely to matter in practice.
            batch_index = (batch_index + 1) & 0xFFFFFFFF
            if batch_index == 0:
                start_nonce = secrets.randbits(192) << 64
