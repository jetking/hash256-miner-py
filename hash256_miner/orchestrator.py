"""Mining orchestrator: pulls jobs from the chain, feeds the GPU, and submits
solutions when they land.

The control flow follows the standard PoW miner pattern:

    ┌────────────┐    job (challenge, target, epoch)    ┌──────────┐
    │  RpcClient │ ────────────────────────────────────▶│ GpuMiner │
    └────────────┘                                       └──────────┘
          ▲                                                    │
          │           Solution (nonce, digest)                 │
          │◀───────────────────────────────────────────────────┘
          │
          ▼
   submit_solution()  →  tx hash printed to stdout

Epoch rotation: every 100 blocks (~20 min on Ethereum mainnet) the challenge
changes. We refresh the job whenever (a) we find a solution and want to
submit one, or (b) `--refresh-seconds` elapses since the last fetch. Any
in-flight kernel batch that happens to overlap an epoch rotation is
discarded by the verify step in `GpuMiner.mine`.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from eth_account.signers.local import LocalAccount

from .gpu import GpuMiner, Solution
from .rpc import Hash256RpcClient, MiningJob

log = logging.getLogger(__name__)


@dataclass
class MinerConfig:
    refresh_seconds: float = 30.0
    print_status_seconds: float = 5.0
    submit: bool = True            # if False, found solutions are printed only
    dry_run: bool = False
    priority_fee_gwei: float = 1.0
    max_fee_gwei: Optional[float] = None
    gas_limit: int = 200_000


class Orchestrator:
    def __init__(
        self,
        rpc: Hash256RpcClient,
        gpu: GpuMiner,
        account: Optional[LocalAccount],
        config: MinerConfig,
    ):
        self.rpc = rpc
        self.gpu = gpu
        self.account = account
        self.config = config
        self._stop = threading.Event()

    # --- lifecycle -----------------------------------------------------------

    def stop(self):
        self._stop.set()

    def install_signal_handlers(self):
        def handler(signum, frame):
            log.info("Caught signal %d, stopping after current batch...", signum)
            self.stop()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    # --- main loop -----------------------------------------------------------

    def run(self):
        self.install_signal_handlers()
        last_status_print = 0.0

        while not self._stop.is_set():
            try:
                job = self.rpc.fetch_job()
            except Exception as e:  # noqa: BLE001 — RPC layer is finicky
                log.error("Failed to fetch job: %s. Retrying in 5s.", e)
                time.sleep(5)
                continue

            log.info(
                "New job: epoch=%d target=%s difficulty_bits=%.2f",
                job.epoch,
                "0x" + job.target.to_bytes(32, "big").hex()[:16] + "...",
                _difficulty_bits(job.target),
            )

            # Mine until either: solution found, epoch likely rotated, or
            # the refresh timer fires.
            generator = self.gpu.mine(
                challenge=job.challenge,
                target=job.target,
                max_seconds=self.config.refresh_seconds,
            )
            for solution in generator:
                self._handle_solution(solution, job)
                # After submitting, break out and pull a fresh job — the
                # contract may have advanced the epoch / difficulty.
                break
            else:
                # Generator exhausted by timer — print a heartbeat and loop.
                pass

            # Periodic status print.
            now = time.time()
            if now - last_status_print > self.config.print_status_seconds:
                _print_status(self.gpu, job)
                last_status_print = now

    def _handle_solution(self, sol: Solution, job: MiningJob):
        log.info("🎉  Found solution!")
        log.info("   nonce      = 0x%s", sol.nonce.to_bytes(32, "big").hex())
        log.info("   digest     = 0x%s", sol.digest.hex())
        log.info("   target     = 0x%s", job.target.to_bytes(32, "big").hex())
        log.info("   epoch      = %d", job.epoch)

        if not self.config.submit:
            log.info("submit disabled — not broadcasting")
            return
        if self.account is None:
            log.warning("no private key configured — cannot submit")
            return

        try:
            tx_hash = self.rpc.submit_solution(
                sol.nonce,
                self.account,
                dry_run=self.config.dry_run,
                priority_fee_gwei=self.config.priority_fee_gwei,
                max_fee_gwei=self.config.max_fee_gwei,
                gas_limit=self.config.gas_limit,
            )
            if tx_hash:
                log.info("✓  submitted: 0x%s", tx_hash)
            else:
                log.info("✓  dry-run complete")
        except Exception as e:  # noqa: BLE001
            log.error("Submit failed: %s", e)


# --- helpers ------------------------------------------------------------------

def _difficulty_bits(target: int) -> float:
    """Approximate number of leading zero bits required, as a difficulty proxy."""
    if target <= 0:
        return 256.0
    return 256.0 - target.bit_length()


def _print_status(gpu: GpuMiner, job: MiningJob):
    rate = gpu.stats.hashrate()
    last = gpu.stats.last_hashrate()
    print(
        f"  [status]  hashes={gpu.stats.hashes:,}  "
        f"avg={_format_hashrate(rate)}  "
        f"inst={_format_hashrate(last)}  "
        f"epoch={job.epoch}  "
        f"age={time.time() - job.fetched_at:.1f}s",
        file=sys.stderr,
    )


def _format_hashrate(h_per_s: float) -> str:
    for unit in ("H/s", "KH/s", "MH/s", "GH/s"):
        if h_per_s < 1000:
            return f"{h_per_s:7.2f} {unit}"
        h_per_s /= 1000
    return f"{h_per_s:7.2f} TH/s"
