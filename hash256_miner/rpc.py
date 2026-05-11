"""Talk to an Ethereum node to fetch mining state and submit solutions.

The HASH contract exposes some kind of view functions for `challenge()`,
`difficulty()` and `epoch()` — the whitepaper does not pin the exact ABI
strings since the source code has not yet been published at the time of
writing (the genesis mint is still at 0%). This module is written so that
the four candidate getter names below can be swapped to match the real
ABI without touching the miner core.

Override the selectors via the `--abi-overrides` CLI flag if the deployed
contract turns out to use different names — e.g. `getChallenge`,
`currentDifficulty`, `currentEpoch`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.types import TxParams

from .protocol import (
    ChallengeInputs,
    DEFAULT_CONTRACT,
    MAINNET_CHAIN_ID,
    build_challenge,
    encode_uint256,
    selector,
)

log = logging.getLogger(__name__)


# Best-guess function signatures for the HASH contract. The whitepaper
# specifies the puzzle math but not the ABI names. These match the common
# convention for ERC918-style mineable tokens; the user can override.
DEFAULT_VIEWS = {
    "challenge_for": "getChallengeForMiner(address)",   # returns bytes32
    "difficulty":    "currentDifficulty()",             # returns uint256
    "epoch":         "currentEpoch()",                  # returns uint256
    "block_number":  "blockNumber()",                   # returns uint256, optional
    "total_mints":   "totalMints()",                    # returns uint256, optional
}

# The submit function. Most ERC918 forks use `mint(uint256 nonce)`; the
# HASH whitepaper hints the contract may also accept a challenge digest
# alongside the nonce, but does not commit to a signature. We default to
# the simplest form and let the user override.
DEFAULT_SUBMIT = "mint(uint256)"


@dataclass
class MiningJob:
    """Everything a worker needs to start hashing."""
    challenge: bytes        # 32 bytes
    target: int             # uint256 difficulty target
    epoch: int              # whatever the contract considers the current epoch
    fetched_at: float       # wall-clock seconds, for staleness checks


class Hash256RpcClient:
    """Thin wrapper around web3.py for the HASH contract."""

    def __init__(
        self,
        rpc_url: str,
        miner_address: str,
        contract: str = DEFAULT_CONTRACT,
        chain_id: int = MAINNET_CHAIN_ID,
        abi_overrides: Optional[dict[str, str]] = None,
        submit_signature: str = DEFAULT_SUBMIT,
        poa: bool = False,
    ):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if poa:
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not self.w3.is_connected():
            raise ConnectionError(f"could not reach RPC at {rpc_url}")

        self.miner_address = Web3.to_checksum_address(miner_address)
        self.contract = Web3.to_checksum_address(contract)
        self.chain_id = chain_id
        self.submit_signature = submit_signature

        sigs = dict(DEFAULT_VIEWS)
        if abi_overrides:
            sigs.update(abi_overrides)
        self._sigs = sigs

    # --- low-level eth_call ---------------------------------------------------

    def _call(self, sig: str, args: bytes = b"") -> bytes:
        data = selector(sig) + args
        result = self.w3.eth.call({
            "to": self.contract,
            "data": "0x" + data.hex(),
        })
        return bytes(result)

    # --- public state getters ------------------------------------------------

    def get_difficulty(self) -> int:
        raw = self._call(self._sigs["difficulty"])
        return int.from_bytes(raw[-32:], "big") if raw else 0

    def get_epoch(self) -> int:
        raw = self._call(self._sigs["epoch"])
        return int.from_bytes(raw[-32:], "big") if raw else 0

    def get_total_mints(self) -> int:
        try:
            raw = self._call(self._sigs["total_mints"])
            return int.from_bytes(raw[-32:], "big") if raw else 0
        except Exception:  # noqa: BLE001 — optional view, swallow
            return -1

    def get_challenge_from_chain(self) -> Optional[bytes]:
        """Ask the contract directly for *this miner's* challenge.

        Returns None if the contract does not expose such a getter; callers
        can fall back to `build_challenge_locally`.
        """
        try:
            raw = self._call(
                self._sigs["challenge_for"],
                self.miner_address_as_word(),
            )
            if len(raw) >= 32:
                return bytes(raw[:32])
        except Exception as e:  # noqa: BLE001
            log.debug("on-chain challenge fetch failed: %s", e)
        return None

    def miner_address_as_word(self) -> bytes:
        raw = bytes.fromhex(self.miner_address[2:])
        return b"\x00" * 12 + raw

    def build_challenge_locally(self, epoch: int) -> bytes:
        return build_challenge(ChallengeInputs(
            chain_id=self.chain_id,
            contract=self.contract,
            miner=self.miner_address,
            epoch=epoch,
        ))

    # --- the combined "give me a job" call ------------------------------------

    def fetch_job(self) -> MiningJob:
        """Pull a fresh (challenge, target, epoch) snapshot from the chain."""
        epoch = self.get_epoch()
        difficulty = self.get_difficulty()

        # Prefer the on-chain challenge if the getter exists — it removes any
        # ambiguity about the preimage layout. Otherwise reconstruct it from
        # the whitepaper formula.
        challenge = self.get_challenge_from_chain()
        if challenge is None:
            challenge = self.build_challenge_locally(epoch)

        return MiningJob(
            challenge=challenge,
            target=difficulty,
            epoch=epoch,
            fetched_at=time.time(),
        )

    # --- transaction submission ----------------------------------------------

    def build_submit_tx(
        self,
        nonce_value: int,
        account: LocalAccount,
        *,
        gas_limit: int = 200_000,
        priority_fee_gwei: float = 1.0,
        max_fee_gwei: Optional[float] = None,
    ) -> TxParams:
        """Build (but do not sign) the mint transaction."""
        if not (0 <= nonce_value < 1 << 256):
            raise ValueError("nonce out of uint256 range")

        data = selector(self.submit_signature) + encode_uint256(nonce_value)
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas") or self.w3.eth.gas_price
        max_priority = self.w3.to_wei(priority_fee_gwei, "gwei")
        max_fee_wei = (
            self.w3.to_wei(max_fee_gwei, "gwei") if max_fee_gwei is not None
            else base_fee * 2 + max_priority
        )

        return {
            "from": account.address,
            "to": self.contract,
            "data": "0x" + data.hex(),
            "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": self.chain_id,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": max_priority,
            "value": 0,
        }

    def submit_solution(
        self,
        nonce_value: int,
        account: LocalAccount,
        *,
        dry_run: bool = False,
        **tx_overrides: Any,
    ) -> Optional[str]:
        """Sign and broadcast the mint transaction. Returns tx hash hex."""
        tx = self.build_submit_tx(nonce_value, account, **tx_overrides)

        if dry_run:
            log.info("DRY RUN — would send: %s", tx)
            return None

        signed = account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()


def load_account_from_private_key(pk_hex: str) -> LocalAccount:
    if not pk_hex.startswith("0x"):
        pk_hex = "0x" + pk_hex
    return Account.from_key(pk_hex)
