"""GPU kernel correctness test: run the OpenCL miner against a relaxed target
and verify every solution it returns matches the CPU keccak256.

Skipped if no OpenCL platform is available.
"""

import pytest

pytest.importorskip("pyopencl")

import secrets
from eth_utils import keccak

from hash256_miner.gpu import GpuMiner, list_devices, pick_device
from hash256_miner.protocol import encode_uint256


@pytest.fixture(scope="module")
def gpu():
    if not list_devices():
        pytest.skip("No OpenCL devices available")
    device = pick_device(None, None)
    # Small global_size to keep the test fast on CI runners.
    return GpuMiner(device, local_size=64, global_size=1 << 16)


def test_gpu_finds_easy_solutions(gpu):
    """With a target of 1<<240 (top 2 bytes zero) we should find ~256 hits
    per million attempts. Use a 2-second budget so the test is fast."""
    challenge = secrets.token_bytes(32)
    target = 1 << 240

    found_any = False
    for solution in gpu.mine(challenge, target, max_seconds=2.0):
        found_any = True
        # Re-verify on CPU.
        digest = keccak(challenge + encode_uint256(solution.nonce))
        assert digest == solution.digest
        assert int.from_bytes(digest, "big") < target
        break

    assert found_any, "GPU didn't find any solution in 2s with 1<<240 target"


def test_gpu_respects_impossible_target(gpu):
    """target = 0 → no nonce can win."""
    challenge = secrets.token_bytes(32)
    found = list(gpu.mine(challenge, 0, max_seconds=0.5))
    assert found == []
