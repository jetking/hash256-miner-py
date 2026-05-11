"""Protocol primitives for hash256.org PoW.

The HASH whitepaper defines the puzzle as:

    challenge = keccak256(chainId ‖ contract ‖ miner ‖ epoch)
    valid iff keccak256(challenge ‖ nonce) < currentDifficulty

This module contains the pure-Python implementations of those operations,
plus helpers for converting between hex / bytes / uint256 and for computing
4-byte function selectors. Nothing here talks to the chain — `rpc.py` does.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_utils import keccak, to_bytes, to_checksum_address

from .constants import DEFAULT_CONTRACT, MAINNET_CHAIN_ID


# --- ABI / selector helpers ---------------------------------------------------

def selector(signature: str) -> bytes:
    """Return the first 4 bytes of keccak256(signature)."""
    return keccak(text=signature)[:4]


def encode_address(addr: str) -> bytes:
    """Encode an Ethereum address as a 32-byte left-padded word."""
    raw = to_bytes(hexstr=to_checksum_address(addr))
    if len(raw) != 20:
        raise ValueError(f"address must decode to 20 bytes, got {len(raw)}")
    return b"\x00" * 12 + raw


def encode_uint256(value: int) -> bytes:
    if value < 0 or value >= 1 << 256:
        raise ValueError("uint256 out of range")
    return value.to_bytes(32, "big")


# --- Challenge construction ---------------------------------------------------
#
# The whitepaper describes the challenge as
# `keccak256(chainId ‖ contract ‖ miner ‖ epoch)`. The deployed contract
# builds this via Solidity abi.encode, which pads all values to 32-byte ABI
# words:
#
#     chainId  : uint256 (32 bytes, big-endian)
#     contract : address (32 bytes, left-padded)
#     miner    : address (32 bytes, left-padded)
#     epoch    : uint256 (32 bytes)
#
# `packed=True` remains available for forks that use abi.encodePacked,
# which concatenates addresses at their natural 20-byte width:
#
#     chainId  : uint256 (32 bytes, big-endian)
#     contract : address (20 bytes)
#     miner    : address (20 bytes)
#     epoch    : uint256 (32 bytes)

@dataclass(frozen=True)
class ChallengeInputs:
    chain_id: int
    contract: str   # 0x-prefixed checksum address
    miner: str      # 0x-prefixed checksum address
    epoch: int      # current epoch number (whitepaper says it rotates every 100 blocks)


def build_challenge(inputs: ChallengeInputs, *, packed: bool = False) -> bytes:
    """Compute the 32-byte challenge for a (miner, epoch) pair.

    Args:
        inputs: chain id, contract address, miner address, epoch.
        packed: if True, use Solidity's abi.encodePacked layout
            (chainId u256 ‖ contract addr20 ‖ miner addr20 ‖ epoch u256),
            which totals 104 bytes. If False, use abi.encode (128 bytes,
            all values padded to 32).

    Returns:
        32-byte keccak256 digest.
    """
    contract_raw = to_bytes(hexstr=to_checksum_address(inputs.contract))
    miner_raw = to_bytes(hexstr=to_checksum_address(inputs.miner))

    if packed:
        preimage = (
            encode_uint256(inputs.chain_id)
            + contract_raw
            + miner_raw
            + encode_uint256(inputs.epoch)
        )
    else:
        preimage = (
            encode_uint256(inputs.chain_id)
            + encode_address(inputs.contract)
            + encode_address(inputs.miner)
            + encode_uint256(inputs.epoch)
        )
    return keccak(preimage)


# --- Verification -------------------------------------------------------------

def verify_solution(challenge: bytes, nonce: int, target: int) -> bool:
    """Check whether keccak256(challenge ‖ nonce_uint256_be) < target.

    The contract enforces the same inequality on-chain. We use it both as a
    sanity check before broadcasting a transaction and as the basis for the
    CPU verifier in tests.
    """
    if len(challenge) != 32:
        raise ValueError("challenge must be 32 bytes")
    digest = keccak(challenge + encode_uint256(nonce))
    return int.from_bytes(digest, "big") < target
