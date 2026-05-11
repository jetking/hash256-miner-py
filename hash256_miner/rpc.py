"""Talk to an Ethereum node to fetch mining state and submit solutions.

The deployed HASH contract exposes `getChallenge(address)`, `miningState()`,
and `mine(uint256)`. This module keeps the low-level selector plumbing small
and still allows CLI overrides for future contract variants.
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
    build_challenge,
    encode_uint256,
    selector,
)
from .constants import DEFAULT_CONTRACT, DEFAULT_SUBMIT, MAINNET_CHAIN_ID

log = logging.getLogger(__name__)


DEFAULT_VIEWS = {
    "challenge_for": "getChallenge(address)",           # returns bytes32
    "difficulty":    "currentDifficulty()",             # returns uint256
    "mining_state":  "miningState()",                   # returns 7 uint256 words
    "block_number":  "blockNumber()",                   # returns uint256, optional
    "total_mints":   "totalMints()",                    # returns uint256, optional
    "balance":       "balanceOf(address)",              # returns uint256, optional
}

def is_rate_limited_error(exc: BaseException) -> bool:
    """Return True if an RPC exception looks like provider throttling."""
    message = str(exc).lower()
    return (
        "429" in message
        or "rate limit" in message
        or "rate-limited" in message
        or "too many requests" in message
    )


@dataclass
class MiningState:
    era: int
    reward: int
    difficulty: int
    minted: int
    remaining: int
    epoch: int
    epoch_blocks_left: int


@dataclass
class MiningJob:
    """Everything a worker needs to start hashing."""
    challenge: bytes        # 32 bytes
    target: int             # uint256 difficulty target
    epoch: int              # whatever the contract considers the current epoch
    fetched_at: float       # wall-clock seconds, for staleness checks
    state: Optional[MiningState] = None
    miner_balance: Optional[int] = None


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
        min_request_interval: float = 1.0,
    ):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if poa:
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        self.miner_address = Web3.to_checksum_address(miner_address)
        self.contract = Web3.to_checksum_address(contract)
        self.chain_id = chain_id
        self.submit_signature = submit_signature
        self.min_request_interval = max(0.0, min_request_interval)
        self._last_rpc_request_at = 0.0

        sigs = dict(DEFAULT_VIEWS)
        if abi_overrides:
            sigs.update(abi_overrides)
        self._sigs = sigs

    # --- low-level eth_call ---------------------------------------------------

    def _throttle_rpc(self) -> None:
        if self.min_request_interval <= 0:
            return
        now = time.monotonic()
        wait_seconds = self.min_request_interval - (now - self._last_rpc_request_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        self._last_rpc_request_at = time.monotonic()

    def _call(self, sig: str, args: bytes = b"") -> bytes:
        data = selector(sig) + args
        self._throttle_rpc()
        try:
            result = self.w3.eth.call({
                "to": self.contract,
                "data": "0x" + data.hex(),
            })
        except Exception as exc:  # noqa: BLE001 - preserve provider details
            raise RuntimeError(
                f"eth_call {sig} selector=0x{data[:4].hex()} failed: {exc}"
            ) from exc
        return bytes(result)

    # --- public state getters ------------------------------------------------

    @staticmethod
    def _decode_uint256_words(raw: bytes, count: int) -> list[int]:
        if len(raw) < count * 32:
            raise RuntimeError(
                f"ABI response too short: expected at least {count * 32} bytes, got {len(raw)}"
            )
        return [
            int.from_bytes(raw[i * 32:(i + 1) * 32], "big")
            for i in range(count)
        ]

    def get_mining_state(self) -> MiningState:
        raw = self._call(self._sigs["mining_state"])
        words = self._decode_uint256_words(raw, 7)
        return MiningState(
            era=words[0],
            reward=words[1],
            difficulty=words[2],
            minted=words[3],
            remaining=words[4],
            epoch=words[5],
            epoch_blocks_left=words[6],
        )

    def get_difficulty(self) -> int:
        raw = self._call(self._sigs["difficulty"])
        return int.from_bytes(raw[-32:], "big") if raw else 0

    def get_epoch(self) -> int:
        return self.get_mining_state().epoch

    def get_total_mints(self) -> int:
        try:
            raw = self._call(self._sigs["total_mints"])
            return int.from_bytes(raw[-32:], "big") if raw else 0
        except Exception:  # noqa: BLE001 — optional view, swallow
            return -1

    def get_balance(self) -> Optional[int]:
        try:
            raw = self._call(
                self._sigs["balance"],
                self.miner_address_as_word(),
            )
            return int.from_bytes(raw[-32:], "big") if raw else 0
        except Exception:  # noqa: BLE001 — optional ERC-20 view, swallow
            return None

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
            if is_rate_limited_error(e):
                raise
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

    def get_block_number(self) -> int:
        self._throttle_rpc()
        return self.w3.eth.block_number

    # --- the combined "give me a job" call ------------------------------------

    def fetch_job(self, *, include_balance: bool = False) -> MiningJob:
        """Pull a fresh (challenge, target, epoch) snapshot from the chain."""
        state = self.get_mining_state()

        # Prefer the on-chain challenge if the getter exists — it removes any
        # ambiguity about the preimage layout. Otherwise reconstruct it from
        # the whitepaper formula.
        challenge = self.get_challenge_from_chain()
        if challenge is None:
            challenge = self.build_challenge_locally(state.epoch)

        return MiningJob(
            challenge=challenge,
            target=state.difficulty,
            epoch=state.epoch,
            fetched_at=time.time(),
            state=state,
            miner_balance=self.get_balance() if include_balance else None,
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
        self._throttle_rpc()
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas")
        if base_fee is None:
            self._throttle_rpc()
            base_fee = self.w3.eth.gas_price
        max_priority = self.w3.to_wei(priority_fee_gwei, "gwei")
        max_fee_wei = (
            self.w3.to_wei(max_fee_gwei, "gwei") if max_fee_gwei is not None
            else base_fee * 2 + max_priority
        )

        self._throttle_rpc()
        account_nonce = self.w3.eth.get_transaction_count(account.address, "pending")

        return {
            "from": account.address,
            "to": self.contract,
            "data": "0x" + data.hex(),
            "nonce": account_nonce,
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
        self._throttle_rpc()
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()


def load_account_from_private_key(pk_hex: str) -> LocalAccount:
    raw = pk_hex.strip()
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    if len(raw) != 64 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        raise ValueError("private key must be a 32-byte hex string, with or without 0x")

    try:
        return Account.from_key("0x" + raw)
    except Exception as exc:  # noqa: BLE001 - hide low-level parser details
        raise ValueError("invalid private key") from exc
