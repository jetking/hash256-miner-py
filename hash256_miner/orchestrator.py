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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, TextIO

from eth_account.signers.local import LocalAccount

from .gpu import GpuMiner, Solution
from .rpc import Hash256RpcClient, MiningJob, is_rate_limited_error

log = logging.getLogger(__name__)


@dataclass
class MinerConfig:
    refresh_seconds: float = 30.0
    print_status_seconds: float = 2.0
    submit: bool = True            # if False, found solutions are printed only
    dry_run: bool = False
    priority_fee_gwei: float = 1.0
    max_fee_gwei: Optional[float] = None
    gas_limit: int = 200_000
    credential_diagnostics: Optional[str] = None
    tui: bool = False


class MinerReporter(Protocol):
    def start(self, gpu: GpuMiner, config: MinerConfig) -> None: ...
    def stop(self) -> None: ...
    def signal(self, signum: int) -> None: ...
    def job(self, job: MiningJob, config: MinerConfig) -> None: ...
    def refresh(self, elapsed: float, refresh_seconds: float) -> None: ...
    def status(self, gpu: GpuMiner, job: MiningJob, *, refresh_seconds: float) -> None: ...
    def solution(self, sol: Solution, job: MiningJob) -> None: ...
    def submit_disabled(self) -> None: ...
    def submit_success(self, tx_hash: str) -> None: ...
    def submit_dry_run(self) -> None: ...
    def submit_failed(self, reason: str) -> None: ...


class ConsoleReporter:
    def start(self, gpu: GpuMiner, config: MinerConfig) -> None:
        return None

    def stop(self) -> None:
        return None

    def signal(self, signum: int) -> None:
        print(
            f"\n[signal] caught signal {signum}, stopping after current GPU batch...",
            file=sys.stderr,
            flush=True,
        )

    def job(self, job: MiningJob, config: MinerConfig) -> None:
        _print_job(job, config)

    def refresh(self, elapsed: float, refresh_seconds: float) -> None:
        print(
            f"[refresh] job age={elapsed:.1f}s reached "
            f"refresh interval={refresh_seconds:.1f}s; fetching new state",
            file=sys.stderr,
            flush=True,
        )

    def status(self, gpu: GpuMiner, job: MiningJob, *, refresh_seconds: float) -> None:
        _print_status(gpu, job, refresh_seconds=refresh_seconds)

    def solution(self, sol: Solution, job: MiningJob) -> None:
        print(
            "\n[found]\n"
            f"  nonce  = 0x{sol.nonce.to_bytes(32, 'big').hex()}\n"
            f"  digest = 0x{sol.digest.hex()}\n"
            f"  target = 0x{job.target.to_bytes(32, 'big').hex()}\n"
            f"  epoch  = {job.epoch}",
            file=sys.stderr,
            flush=True,
        )

    def submit_disabled(self) -> None:
        print("[submit] disabled; not broadcasting", file=sys.stderr, flush=True)

    def submit_success(self, tx_hash: str) -> None:
        print(f"[submit] submitted: {_normalize_tx_hash(tx_hash)}", file=sys.stderr, flush=True)

    def submit_dry_run(self) -> None:
        print("[submit] dry-run complete", file=sys.stderr, flush=True)

    def submit_failed(self, reason: str) -> None:
        print(f"[submit] failed: {reason}", file=sys.stderr, flush=True)


class PersistentEventReporter:
    """Reporter wrapper that appends important mining events to a file."""

    def __init__(self, inner: MinerReporter, path: str):
        self.inner = inner
        self.path = path
        self._file: Optional[TextIO] = None
        self._exit_signal: Optional[int] = None

    def start(self, gpu: GpuMiner, config: MinerConfig) -> None:
        path = Path(self.path).expanduser()
        if str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8", buffering=1)
        self._write(
            "task_start",
            device=gpu.device.name.strip(),
            submit=_submit_mode(config),
            refresh_seconds=f"{config.refresh_seconds:.1f}",
            status_seconds=f"{config.print_status_seconds:.1f}",
        )
        self._write(
            "miner_start",
            device=gpu.device.name.strip(),
            submit=_submit_mode(config),
            refresh_seconds=f"{config.refresh_seconds:.1f}",
            status_seconds=f"{config.print_status_seconds:.1f}",
        )
        self.inner.start(gpu, config)

    def stop(self) -> None:
        try:
            if self._exit_signal is not None:
                self._write("signal_exit", signum=str(self._exit_signal))
            self._write("miner_stop")
        finally:
            try:
                self.inner.stop()
            finally:
                if self._file:
                    self._file.close()
                    self._file = None

    def signal(self, signum: int) -> None:
        self._exit_signal = signum
        self._write("signal_exit_requested", signum=str(signum))
        self._write("signal", signum=str(signum))
        self.inner.signal(signum)

    def job(self, job: MiningJob, config: MinerConfig) -> None:
        self._write(
            "job",
            epoch=str(job.epoch),
            target="0x" + job.target.to_bytes(32, "big").hex(),
            challenge="0x" + job.challenge.hex(),
            submit=_submit_mode(config),
        )
        self.inner.job(job, config)

    def refresh(self, elapsed: float, refresh_seconds: float) -> None:
        self._write(
            "refresh",
            elapsed_seconds=f"{elapsed:.1f}",
            refresh_seconds=f"{refresh_seconds:.1f}",
        )
        self.inner.refresh(elapsed, refresh_seconds)

    def status(self, gpu: GpuMiner, job: MiningJob, *, refresh_seconds: float) -> None:
        self.inner.status(gpu, job, refresh_seconds=refresh_seconds)

    def solution(self, sol: Solution, job: MiningJob) -> None:
        self._write(
            "solution_found",
            epoch=str(job.epoch),
            nonce="0x" + sol.nonce.to_bytes(32, "big").hex(),
            digest="0x" + sol.digest.hex(),
            target="0x" + job.target.to_bytes(32, "big").hex(),
        )
        self.inner.solution(sol, job)

    def submit_disabled(self) -> None:
        self._write("submit_disabled")
        self.inner.submit_disabled()

    def submit_success(self, tx_hash: str) -> None:
        normalized = _normalize_tx_hash(tx_hash)
        self._write("submit_success", tx_hash=normalized)
        self.inner.submit_success(tx_hash)

    def submit_dry_run(self) -> None:
        self._write("submit_dry_run")
        self.inner.submit_dry_run()

    def submit_failed(self, reason: str) -> None:
        self._write("submit_failed", reason=reason)
        self.inner.submit_failed(reason)

    def _write(self, event: str, **fields: str) -> None:
        if self._file is None:
            return
        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        parts = [timestamp, f"event={event}"]
        parts.extend(f"{key}={_quote_event_value(value)}" for key, value in fields.items())
        print(" ".join(parts), file=self._file, flush=True)


class Orchestrator:
    def __init__(
        self,
        rpc: Hash256RpcClient,
        gpu: GpuMiner,
        account: Optional[LocalAccount],
        config: MinerConfig,
        reporter: Optional[MinerReporter] = None,
    ):
        self.rpc = rpc
        self.gpu = gpu
        self.account = account
        self.config = config
        self.reporter = reporter or ConsoleReporter()
        self._stop = threading.Event()

    # --- lifecycle -----------------------------------------------------------

    def stop(self):
        self._stop.set()

    def install_signal_handlers(self):
        def handler(signum, frame):
            self.reporter.signal(signum)
            self.stop()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    # --- main loop -----------------------------------------------------------

    def run(self):
        self.install_signal_handlers()
        self.reporter.start(self.gpu, self.config)
        fetch_retry_seconds = 5.0

        try:
            while not self._stop.is_set():
                try:
                    job = self.rpc.fetch_job(
                        include_balance=self.config.tui,
                        include_total_mints=self.config.tui,
                    )
                except Exception as e:  # noqa: BLE001 — RPC layer is finicky
                    retry_seconds = fetch_retry_seconds
                    if is_rate_limited_error(e):
                        fetch_retry_seconds = min(fetch_retry_seconds * 2, 120.0)
                    else:
                        fetch_retry_seconds = 5.0
                    log.error(
                        "Failed to fetch job: %s. %sRetrying in %.0fs.",
                        e,
                        _format_credential_diagnostics(self.config.credential_diagnostics),
                        retry_seconds,
                    )
                    time.sleep(retry_seconds)
                    continue
                fetch_retry_seconds = 5.0

                self.reporter.job(job, self.config)

                # Mine until either: solution found, epoch likely rotated, or
                # the refresh timer fires. We run the GPU in short slices so
                # status output is visible while a job is still active.
                while not self._stop.is_set():
                    elapsed = time.time() - job.fetched_at
                    refresh_in = self.config.refresh_seconds - elapsed
                    if refresh_in <= 0:
                        self.reporter.refresh(elapsed, self.config.refresh_seconds)
                        break

                    status_seconds = max(self.config.print_status_seconds, 0.1)
                    slice_seconds = min(status_seconds, refresh_in)
                    found_solution = False

                    generator = self.gpu.mine(
                        challenge=job.challenge,
                        target=job.target,
                        max_seconds=slice_seconds,
                    )
                    for solution in generator:
                        self._handle_solution(solution, job)
                        found_solution = True
                        # After submitting, break out and pull a fresh job — the
                        # contract may have advanced the epoch / difficulty.
                        break

                    self.reporter.status(
                        self.gpu,
                        job,
                        refresh_seconds=self.config.refresh_seconds,
                    )

                    if found_solution:
                        break

                if self._stop.is_set():
                    break
        finally:
            self.reporter.stop()

    def _handle_solution(self, sol: Solution, job: MiningJob):
        self.reporter.solution(sol, job)

        if not self.config.submit:
            self.reporter.submit_disabled()
            return
        if self.account is None:
            self.reporter.submit_failed("no private key configured; cannot submit")
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
                self.reporter.submit_success(tx_hash)
            else:
                self.reporter.submit_dry_run()
        except Exception as e:  # noqa: BLE001
            self.reporter.submit_failed(str(e))


# --- helpers ------------------------------------------------------------------

def _difficulty_bits(target: int) -> float:
    """Approximate number of leading zero bits required, as a difficulty proxy."""
    if target <= 0:
        return 256.0
    return 256.0 - target.bit_length()


def _format_credential_diagnostics(diagnostics: Optional[str]) -> str:
    if not diagnostics:
        return ""
    return f"Credential check: {diagnostics}. "


def _print_job(job: MiningJob, config: MinerConfig):
    target_hex = "0x" + job.target.to_bytes(32, "big").hex()
    challenge_hex = "0x" + job.challenge.hex()
    submit_mode = "enabled"
    if not config.submit:
        submit_mode = "disabled"
    elif config.dry_run:
        submit_mode = "dry-run"

    print(
        "[job] "
        f"epoch={job.epoch}  "
        f"difficulty_bits={_difficulty_bits(job.target):.2f}  "
        f"refresh={config.refresh_seconds:.1f}s  "
        f"status={config.print_status_seconds:.1f}s  "
        f"submit={submit_mode}\n"
        f"      target={target_hex}\n"
        f"      challenge={challenge_hex}",
        file=sys.stderr,
        flush=True,
    )


def _print_status(gpu: GpuMiner, job: MiningJob, *, refresh_seconds: float):
    rate = gpu.stats.hashrate()
    last = gpu.stats.last_hashrate()
    age = time.time() - job.fetched_at
    refresh_in = max(refresh_seconds - age, 0.0)
    batch_ms = gpu.stats.last_batch_seconds * 1000.0
    print(
        f"[status] {time.strftime('%H:%M:%S')}  "
        f"epoch={job.epoch}  "
        f"age={age:.1f}s  "
        f"refresh_in={refresh_in:.1f}s  "
        f"hashes={gpu.stats.hashes:,}  "
        f"avg={_format_hashrate(rate)}  "
        f"inst={_format_hashrate(last)}  "
        f"last_batch={gpu.stats.last_batch_hashes:,} hashes/{batch_ms:.1f}ms",
        file=sys.stderr,
        flush=True,
    )


def _format_hashrate(h_per_s: float) -> str:
    for unit in ("H/s", "KH/s", "MH/s", "GH/s"):
        if h_per_s < 1000:
            return f"{h_per_s:7.2f} {unit}"
        h_per_s /= 1000
    return f"{h_per_s:7.2f} TH/s"


def _submit_mode(config: MinerConfig) -> str:
    if not config.submit:
        return "disabled"
    if config.dry_run:
        return "dry-run"
    return "enabled"


def _quote_event_value(value: str) -> str:
    text = str(value).replace("\\", "\\\\").replace("\n", "\\n")
    if not text or any(ch.isspace() for ch in text) or "=" in text:
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _normalize_tx_hash(tx_hash: str) -> str:
    return "0x" + tx_hash.removeprefix("0x")
