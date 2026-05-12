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


class OpenClDeviceResetError(RuntimeError):
    """Raised when the OpenCL device/context is no longer usable."""


def list_devices() -> list[tuple[int, int, str, str]]:
    """Enumerate (platform_idx, device_idx, platform_name, device_name)."""
    out: list[tuple[int, int, str, str]] = []
    for p_idx, platform in enumerate(cl.get_platforms()):
        for d_idx, device in enumerate(_platform_devices(platform)):
            out.append((p_idx, d_idx, platform.name.strip(), device.name.strip()))
    return out


def _platform_devices(
    platform: cl.Platform,
    *,
    device_type: Optional[int] = None,
) -> list[cl.Device]:
    try:
        if device_type is None:
            return list(platform.get_devices())
        return list(platform.get_devices(device_type=device_type))
    except cl.LogicError as e:
        log.debug("OpenCL device enumeration failed for %s: %s", platform.name.strip(), e)
        return []


DEFAULT_GLOBAL_FLOOR = 1 << 22  # 4M work items — keep as floor so small GPUs don't regress
DEFAULT_OVER_SUBSCRIBE = 256
DEFAULT_LOCAL_CEILING = 256
DEFAULT_NONCES_PER_ITEM = 64
MAX_NONCES_PER_ITEM = 256


def _resolve_nonces_per_item() -> int:
    """Read HASH256_NONCES_PER_ITEM; default 64. Must be a power of two in [1, 256].

    Each work-item scans this many consecutive nonces in an inner loop, sharing
    challenge / nonce-prefix registers across iterations.
    """
    raw = os.environ.get("HASH256_NONCES_PER_ITEM")
    if raw is None or raw == "":
        return DEFAULT_NONCES_PER_ITEM
    try:
        n = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"HASH256_NONCES_PER_ITEM must be an integer, got {raw!r}"
        ) from exc
    if n < 1 or n > MAX_NONCES_PER_ITEM:
        raise ValueError(
            f"HASH256_NONCES_PER_ITEM must be in [1, {MAX_NONCES_PER_ITEM}], got {n}"
        )
    if n & (n - 1):
        raise ValueError(f"HASH256_NONCES_PER_ITEM must be a power of two, got {n}")
    return n


def auto_work_size(
    device: "cl.Device",
    local_override: Optional[int] = None,
    global_override: Optional[int] = None,
) -> tuple[int, int]:
    """Pick (local_size, global_size) for a device.

    local: ``min(DEFAULT_LOCAL_CEILING, device.max_work_group_size)`` unless
    the caller overrides. global: ``max(DEFAULT_GLOBAL_FLOOR, cu * local *
    over_subscribe)`` — the floor preserves the legacy 4M default so that
    setups already tuned around it don't regress; the adaptive term lets
    larger GPUs scale up automatically. ``HASH256_OVER_SUBSCRIBE`` env var
    tunes the multiplier (default 256).

    Always returns a global that is a multiple of local.
    """
    mwg = int(getattr(device, "max_work_group_size", DEFAULT_LOCAL_CEILING) or DEFAULT_LOCAL_CEILING)
    cu = int(getattr(device, "max_compute_units", 1) or 1)

    if local_override is not None:
        local = int(local_override)
        if local < 1:
            raise ValueError("local_size must be >= 1")
        if local > mwg:
            raise ValueError(
                f"local_size {local} exceeds device max_work_group_size {mwg}"
            )
    else:
        local = min(DEFAULT_LOCAL_CEILING, mwg)

    if global_override is not None:
        global_size = int(global_override)
        if global_size < local:
            raise ValueError(
                f"global_size {global_size} must be >= local_size {local}"
            )
    else:
        try:
            over = int(os.environ.get("HASH256_OVER_SUBSCRIBE", DEFAULT_OVER_SUBSCRIBE))
        except ValueError:
            over = DEFAULT_OVER_SUBSCRIBE
        if over < 1:
            over = DEFAULT_OVER_SUBSCRIBE
        adaptive = cu * local * over
        global_size = max(DEFAULT_GLOBAL_FLOOR, adaptive)

    # OpenCL requires global to be a multiple of local.
    global_size = (global_size // local) * local
    if global_size < local:
        global_size = local
    return local, global_size


def pick_device(platform_idx: Optional[int], device_idx: Optional[int]) -> cl.Device:
    platforms = cl.get_platforms()
    if not platforms:
        raise RuntimeError("No OpenCL platforms detected — install GPU drivers.")

    # Prefer GPUs over CPUs when auto-selecting.
    if platform_idx is None and device_idx is None:
        for platform in platforms:
            gpus = _platform_devices(platform, device_type=cl.device_type.GPU)
            if gpus:
                return gpus[0]
        # Fall back to whatever's there.
        for platform in platforms:
            devices = _platform_devices(platform)
            if devices:
                return devices[0]
        raise RuntimeError("No OpenCL devices detected — install GPU drivers / OpenCL ICDs.")

    p_idx = platform_idx if platform_idx is not None else 0
    d_idx = device_idx if device_idx is not None else 0
    devices = _platform_devices(platforms[p_idx])
    if not devices:
        raise RuntimeError(f"No OpenCL devices detected on platform {p_idx}.")
    return devices[d_idx]


class GpuMiner:
    """One OpenCL context tied to one device. Reusable across jobs."""

    def __init__(
        self,
        device: cl.Device,
        *,
        local_size: Optional[int] = None,
        global_size: Optional[int] = None,
        batch_cooldown_seconds: float = 0.0,
    ):
        self.device = device
        self.context = cl.Context([device])
        self.queue = cl.CommandQueue(self.context)
        resolved_local, resolved_global = auto_work_size(
            device,
            local_override=local_size,
            global_override=global_size,
        )
        self.local_size = resolved_local
        self.global_size = resolved_global
        self.batch_cooldown_seconds = max(batch_cooldown_seconds, 0.0)
        self.stats = GpuStats()

        # Nonce bit layout (kernel ORs gid*NONCES_PER_ITEM + i into low bits of word3):
        #   bits [0, gid_bits)   : gid_base * NONCES_PER_ITEM + i (kernel-supplied)
        #   bits [gid_bits, 64)  : batch_index (host-supplied, per launch)
        #   bits [64, 256)       : random start_nonce (host-supplied, persistent)
        self.nonces_per_item = _resolve_nonces_per_item()
        log2_n = self.nonces_per_item.bit_length() - 1
        self._gid_bits = 32 + log2_n
        self._batch_bits = 64 - self._gid_bits
        self._batch_mask = (1 << self._batch_bits) - 1
        self._w3_keep_mask = ((1 << 64) - 1) ^ ((1 << self._gid_bits) - 1)

        kernel_src = KERNEL_PATH.read_text(encoding="utf-8")
        build_options = _opencl_build_options(device, nonces_per_item=self.nonces_per_item)
        log.debug(
            "Building OpenCL program for %s with options: %s",
            device.name.strip(),
            build_options or "<none>",
        )
        self.program = cl.Program(self.context, kernel_src).build(options=build_options)
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

        The kernel varies the bottom `gid_bits = 32 + log2(NONCES_PER_ITEM)`
        bits of the nonce per work item (each item scans NONCES_PER_ITEM
        consecutive nonces). The host bumps `batch_index` between launches
        so the next kernel call covers the next contiguous slice of nonce
        space. We pick a random 192-bit start_nonce upfront so two miners
        on the same address don't collide on tested ranges.
        """
        if start_nonce is None:
            # 192 random bits in the upper portion; the bottom 64 bits are
            # carved up between the per-launch batch_index and the kernel's
            # per-work-item gid (controlled by gid_bits/batch_bits).
            start_nonce = secrets.randbits(192) << 64

        challenge_words = self._challenge_to_le_words(challenge)
        target_words = self._target_to_be_words(target)

        try:
            cl.enqueue_copy(self.queue, self.buf_challenge, challenge_words)
            cl.enqueue_copy(self.queue, self.buf_target, target_words)
        except cl.Error as exc:
            raise _opencl_runtime_error(exc) from exc

        deadline = (time.time() + max_seconds) if max_seconds else None
        batch_index = 0   # increments per kernel launch; placed in bits 32..63

        while True:
            if deadline is not None and time.time() >= deadline:
                return

            # Each batch uses:
            #   bits [0, gid_bits)  : gid_base * NONCES_PER_ITEM + i (kernel)
            #   bits [gid_bits, 64) : batch_index (host)
            #   bits [64, 256)      : random base from start_nonce
            current_nonce = (
                start_nonce
                | ((batch_index & self._batch_mask) << self._gid_bits)
            ) & ((1 << 256) - 1)

            # Reset result buffer.
            self._result_host[:] = 0
            try:
                cl.enqueue_copy(self.queue, self.buf_result, self._result_host)
            except cl.Error as exc:
                raise _opencl_runtime_error(exc) from exc

            w0, w1, w2, w3 = self._split_nonce_to_be_words(current_nonce)
            # The kernel ORs (gid_base * NONCES_PER_ITEM + i) into the bottom
            # gid_bits bits of w3. Pre-zero those bits so the OR is unambiguous.
            w3_base = w3 & self._w3_keep_mask

            batch_start = time.time()
            try:
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
            except cl.Error as exc:
                raise _opencl_runtime_error(exc) from exc
            batch_seconds = time.time() - batch_start

            hashes_this_batch = self.global_size * self.nonces_per_item
            self.stats.hashes += hashes_this_batch
            self.stats.last_batch_hashes = hashes_this_batch
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

            # Advance to the next batch. After 2^batch_bits batches we wrap
            # and reseed from a fresh random base — vanishingly unlikely to
            # matter in practice.
            batch_index = (batch_index + 1) & self._batch_mask
            if batch_index == 0:
                start_nonce = secrets.randbits(192) << 64
            if self.batch_cooldown_seconds > 0:
                if deadline is None:
                    time.sleep(self.batch_cooldown_seconds)
                else:
                    remaining = deadline - time.time()
                    if remaining > 0:
                        time.sleep(min(self.batch_cooldown_seconds, remaining))


def _opencl_runtime_error(exc: cl.Error) -> RuntimeError:
    message = str(exc)
    upper = message.upper()
    reset_markers = (
        "INVALID_COMMAND_QUEUE",
        "INVALID_CONTEXT",
        "DEVICE_NOT_AVAILABLE",
        "OUT_OF_RESOURCES",
        "LAUNCH_TIMEOUT",
    )
    if any(marker in upper for marker in reset_markers):
        return OpenClDeviceResetError(
            "OpenCL device/context became unusable. On Windows this usually means "
            "the GPU driver reset the device after a long or overheated kernel run. "
            "Reduce --global-size, try a smaller --local-size, add "
            "--batch-cooldown-seconds, and check GPU temperature/power limits. "
            f"Original OpenCL error: {message}"
        )
    return RuntimeError(f"OpenCL mining failed: {message}")


def _opencl_build_options(device: cl.Device, *, nonces_per_item: int) -> list[str]:
    """Return vendor-specific compiler switches for the mining kernel."""
    vendor = (getattr(device, "vendor", "") or "").lower()
    name = (getattr(device, "name", "") or "").lower()
    options: list[str] = [f"-DNONCES_PER_ITEM={nonces_per_item}"]

    disable_vendor_options = os.environ.get("HASH256_DISABLE_VENDOR_OPTIONS", "").lower()
    if (
        disable_vendor_options not in {"1", "true", "yes", "on"}
        and ("nvidia" in vendor or "nvidia" in name)
    ):
        options.extend([
            "-DHASH256_NVIDIA=1",
            "-DHASH256_UNROLL_KECCAK=1",
        ])

    extra = os.environ.get("HASH256_OPENCL_BUILD_OPTIONS", "").strip()
    if extra:
        options.extend(extra.split())

    return options
