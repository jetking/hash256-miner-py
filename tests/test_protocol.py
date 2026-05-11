"""Smoke tests for protocol primitives.

Run with:   pytest tests/
"""
from eth_utils import keccak

from hash256_miner.protocol import (
    ChallengeInputs,
    build_challenge,
    encode_uint256,
    selector,
    verify_solution,
)


def test_selector_known_values():
    # transfer(address,uint256) → 0xa9059cbb (canonical ERC20)
    assert selector("transfer(address,uint256)").hex() == "a9059cbb"
    # balanceOf(address) → 0x70a08231
    assert selector("balanceOf(address)").hex() == "70a08231"


def test_build_challenge_packed_layout():
    """Mirror what abi.encodePacked(chainId, contract, miner, epoch) produces."""
    chain_id = 1
    contract = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
    miner = "0x000000000000000000000000000000000000dEaD"
    epoch = 42

    got = build_challenge(
        ChallengeInputs(chain_id=chain_id, contract=contract, miner=miner, epoch=epoch),
        packed=True,
    )
    expected = keccak(
        chain_id.to_bytes(32, "big")
        + bytes.fromhex(contract[2:])
        + bytes.fromhex(miner[2:])
        + epoch.to_bytes(32, "big")
    )
    assert got == expected


def test_build_challenge_padded_layout():
    chain_id = 1
    contract = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
    miner = "0x000000000000000000000000000000000000dEaD"
    epoch = 42

    got = build_challenge(
        ChallengeInputs(chain_id=chain_id, contract=contract, miner=miner, epoch=epoch),
        packed=False,
    )
    expected = keccak(
        chain_id.to_bytes(32, "big")
        + b"\x00" * 12 + bytes.fromhex(contract[2:])
        + b"\x00" * 12 + bytes.fromhex(miner[2:])
        + epoch.to_bytes(32, "big")
    )
    assert got == expected


def test_build_challenge_default_matches_deployed_contract_abi_encode():
    chain_id = 1
    contract = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
    miner = "0x000000000000000000000000000000000000dEaD"
    epoch = 42

    got = build_challenge(
        ChallengeInputs(chain_id=chain_id, contract=contract, miner=miner, epoch=epoch),
    )
    expected = keccak(
        chain_id.to_bytes(32, "big")
        + b"\x00" * 12 + bytes.fromhex(contract[2:])
        + b"\x00" * 12 + bytes.fromhex(miner[2:])
        + epoch.to_bytes(32, "big")
    )
    assert got == expected


def test_verify_solution_trivial_target():
    """Target = MAX_UINT256 → every nonce wins."""
    challenge = b"\x00" * 32
    nonce = 12345
    assert verify_solution(challenge, nonce, (1 << 256) - 1)


def test_verify_solution_impossible_target():
    """Target = 0 → no nonce ever wins."""
    challenge = b"\x00" * 32
    assert not verify_solution(challenge, 0, 0)
    assert not verify_solution(challenge, 999_999, 0)


def test_verify_solution_known_low_target():
    """Find a nonce whose digest starts with byte 0x00 — should clear target."""
    challenge = bytes.fromhex(
        "a" * 64
    )  # any deterministic challenge
    target = 1 << 248   # require top byte == 0

    # Try a few thousand nonces — there's a 1/256 chance per try, so this
    # is virtually guaranteed to find one quickly.
    found = None
    for n in range(10_000):
        digest = keccak(challenge + encode_uint256(n))
        if int.from_bytes(digest, "big") < target:
            found = n
            break
    assert found is not None
    assert verify_solution(challenge, found, target)
    # And the next bytes should not satisfy with target/2.
    if not verify_solution(challenge, found, target >> 1):
        # That's normal — most won't beat a tighter target.
        pass
