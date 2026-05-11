"""Rich-based terminal UI for live mining status."""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import time
from collections import deque
from typing import Optional

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .gpu import GpuMiner, Solution
from .orchestrator import MinerConfig
from .rpc import MiningJob, MiningState


RETARGET_MINTS = 2016
ERA_MINTS = 100_000
ETH_BLOCK_SECONDS = 12
TOKEN_SCALE = 10**18


class TuiReporter(logging.Handler):
    """Reporter that renders a dashboard and captures log records."""

    def __init__(self, *, max_log_lines: int = 12):
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.console = Console(stderr=True)
        self.max_log_lines = max_log_lines
        self.log_lines: deque[str] = deque(maxlen=max_log_lines)
        self.gpu: Optional[GpuMiner] = None
        self.config: Optional[MinerConfig] = None
        self.current_job: Optional[MiningJob] = None
        self.live: Optional[Live] = None
        self.previous_handlers: list[logging.Handler] = []
        self._gpu_load: Optional[float] = None
        self._gpu_load_checked_at = 0.0
        self._last_tx: Optional[str] = None

    def start(self, gpu: GpuMiner, config: MinerConfig) -> None:
        self.gpu = gpu
        self.config = config
        root = logging.getLogger()
        self.previous_handlers = list(root.handlers)
        for handler in self.previous_handlers:
            root.removeHandler(handler)
        root.addHandler(self)
        self.live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            screen=False,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self.live.start()

    def stop(self) -> None:
        root = logging.getLogger()
        root.removeHandler(self)
        for handler in self.previous_handlers:
            root.addHandler(handler)
        self.previous_handlers = []
        if self.live:
            self.live.update(self._render())
            self.live.stop()
            self.live = None

    def emit(self, record: logging.LogRecord) -> None:
        self._append_log(self.format(record))

    def signal(self, signum: int) -> None:
        self._append_log(f"[signal] caught signal {signum}, stopping after current GPU batch...")

    def job(self, job: MiningJob, config: MinerConfig) -> None:
        self.current_job = job
        self._append_log(
            "[job] "
            f"epoch={job.epoch} difficulty={_format_multiplier(job.target)} "
            f"refresh={config.refresh_seconds:.1f}s submit={_submit_mode(config)}"
        )

    def refresh(self, elapsed: float, refresh_seconds: float) -> None:
        self._append_log(
            f"[refresh] job age={elapsed:.1f}s reached refresh interval={refresh_seconds:.1f}s; "
            "fetching new state"
        )

    def status(self, gpu: GpuMiner, job: MiningJob, *, refresh_seconds: float) -> None:
        self.gpu = gpu
        self.current_job = job
        self._append_log(
            f"[status] {time.strftime('%H:%M:%S')} epoch={job.epoch} "
            f"hashes={gpu.stats.hashes:,} avg={_format_hashrate(gpu.stats.hashrate())} "
            f"inst={_format_hashrate(gpu.stats.last_hashrate())}"
        )
        self._update()

    def solution(self, sol: Solution, job: MiningJob) -> None:
        self._append_log(
            "[found] "
            f"nonce=0x{sol.nonce.to_bytes(32, 'big').hex()} "
            f"digest=0x{sol.digest.hex()} epoch={job.epoch}"
        )

    def submit_disabled(self) -> None:
        self._last_tx = "submit disabled"
        self._append_log("[submit] disabled; not broadcasting")

    def submit_success(self, tx_hash: str) -> None:
        self._last_tx = "0x" + tx_hash.removeprefix("0x")
        self._append_log(f"[submit] submitted: {self._last_tx}")

    def submit_dry_run(self) -> None:
        self._last_tx = "dry-run"
        self._append_log("[submit] dry-run complete")

    def _append_log(self, line: str) -> None:
        self.log_lines.append(line)
        self._update()

    def _update(self) -> None:
        if self.live:
            self.live.update(self._render())

    def _render(self) -> Group:
        return Group(
            self._status_panel(),
            self._device_panel(),
            self._log_panel(),
        )

    def _status_panel(self) -> Panel:
        job = self.current_job
        state = job.state if job else None
        gpu = self.gpu
        config = self.config

        metrics = Table(
            show_header=False,
            expand=True,
            box=box.SQUARE,
            show_edge=False,
            pad_edge=False,
            collapse_padding=False,
        )
        metrics.add_column(ratio=1)
        metrics.add_column(ratio=1)

        metrics.add_row(
            _metric_group(
                [
                    ("ERA", _format_era(state)),
                    ("DIFFICULTY", _format_multiplier(job.target) if job else "—"),
                    ("PROJECTED", "—"),
                    ("EPOCH ROTATES", _format_epoch_rotation(state)),
                    ("REMAINING", _format_token(_state_value(state, "remaining"))),
                ]
            ),
            _metric_group(
                [
                    ("REWARD / MINT", _format_token(_state_value(state, "reward"))),
                    ("NEXT RETARGET", _format_next_retarget(job)),
                    ("EPOCH", _display_state(state, "epoch")),
                    ("MINTED", _format_token(_state_value(state, "minted"))),
                    ("YOUR BALANCE", _format_token(job.miner_balance if job else None)),
                ]
            ),
        )

        hashes = gpu.stats.hashes if gpu else 0
        avg_hashrate = gpu.stats.hashrate() if gpu else 0.0
        solution_chance = _solution_probability(hashes, job.target) if job else 0.0
        total_progress = _total_mining_progress(state)
        round_progress = _round_progress(job, config.refresh_seconds) if job and config else 0.0
        era_mints_done = _era_mint_count(job)

        browser = Table(
            show_header=False,
            expand=True,
            box=box.SQUARE,
            show_edge=False,
            pad_edge=False,
            collapse_padding=False,
        )
        browser.add_column(ratio=1)
        browser.add_column(ratio=1)
        browser.add_row(
            _metric_group(
                [
                    ("HASHRATE", _format_hashrate(avg_hashrate) if gpu else "—"),
                    ("HASHES TRIED", f"{hashes:,}"),
                    ("CHALLENGE", _format_hash(job.challenge) if job else "—"),
                ]
            ),
            _metric_group(
                [
                    (
                        "EXPECTED REWARD / HR",
                        _format_expected_reward_per_hour(avg_hashrate, job) if job else "—",
                    ),
                    (
                        "ELAPSED",
                        _format_elapsed(time.time() - gpu.stats.started_at) if gpu else "—",
                    ),
                    ("TX", self._last_tx or "—"),
                ]
            ),
        )

        progress = Table.grid(expand=True)
        progress.add_row(Text("TOTAL MINING PROGRESS", style="dim green"))
        progress.add_row(_bar(total_progress, "green"))
        progress.add_row(
            Text(
                f"CURRENT ROUND  {round_progress * 100:,.2f}%    "
                f"HASHES {hashes:,}    "
                f"SOLUTION CHANCE {solution_chance * 100:,.4f}%",
                style="dim green",
            )
        )
        progress.add_row(_bar(round_progress, "yellow"))
        if era_mints_done is not None:
            era_progress = min(era_mints_done / ERA_MINTS, 1.0)
            progress.add_row(Text(f"THIS ERA  ·  {era_mints_done:,} OF {ERA_MINTS:,} MINTS", style="dim green"))
            progress.add_row(_bar(era_progress, "yellow"))

        body = Table(
            show_header=False,
            expand=True,
            box=box.SQUARE,
            show_edge=False,
            show_lines=True,
            pad_edge=False,
            collapse_padding=True,
        )
        body.add_column(ratio=1)
        body.add_row(metrics)
        body.add_row(browser)
        body.add_row(progress)

        title = "[bold green] M I N I N G  S T A T E [/]"
        subtitle = f"era {_format_era(state)}"
        return Panel(body, title=title, subtitle=subtitle, border_style="green", box=box.SQUARE)

    def _device_panel(self) -> Panel:
        gpu = self.gpu
        name = gpu.device.name.strip() if gpu else "—"
        avg = _format_hashrate(gpu.stats.hashrate()) if gpu else "—"
        inst = _format_hashrate(gpu.stats.last_hashrate()) if gpu else "—"
        last_batch = "—"
        if gpu:
            batch_ms = gpu.stats.last_batch_seconds * 1000.0
            last_batch = f"{gpu.stats.last_batch_hashes:,} hashes / {batch_ms:.1f}ms"

        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        table.add_column(ratio=1)
        table.add_row(_kv("DEVICE", name), _kv("CURRENT SPEED", inst))
        table.add_row(
            _kv("AVERAGE SPEED", avg),
            _kv("global/local", f"{gpu.global_size:,} / {gpu.local_size}" if gpu else "—"),
        )
        table.add_row(_kv("last batch", last_batch), _kv("CPU LOAD", _format_cpu_load()))
        table.add_row("", _kv("GPU LOAD", self._format_gpu_load()))
        return Panel(table, title="[bold green] DEVICE [/]", border_style="green", box=box.SQUARE)

    def _format_gpu_load(self) -> str:
        now = time.monotonic()
        if now - self._gpu_load_checked_at >= 2.0:
            self._gpu_load = _detect_gpu_load()
            self._gpu_load_checked_at = now
        return _format_percent(self._gpu_load)

    def _log_panel(self) -> Panel:
        text = Text()
        for line in self.log_lines:
            style = "green"
            if "[ERROR]" in line or "failed" in line.lower():
                style = "red"
            elif "[WARNING]" in line or "warning" in line.lower():
                style = "yellow"
            text.append(line + "\n", style=style)
        if not self.log_lines:
            text.append("Waiting for logs...\n", style="dim")
        return Panel(text, title="[bold green] LOGS [/]", border_style="green", box=box.SQUARE)


def _metric_group(items: list[tuple[str, str]]) -> Table:
    table = Table.grid(expand=True)
    table.add_column(justify="left", ratio=1)
    table.add_column(justify="right", ratio=1)
    for label, value in items:
        table.add_row(Text(label, style="dim green"), Text(value, style=_value_style(value)))
    return table


def _kv(label: str, value: str) -> Text:
    out = Text()
    out.append(f"{label}: ", style="dim green")
    out.append(value, style=_value_style(value))
    return out


def _bar(progress: float, color: str, *, width: int = 56) -> Text:
    progress = min(max(progress, 0.0), 1.0)
    filled = int(width * progress)
    out = Text()
    out.append("[ ", style=color)
    out.append("█" * filled, style=color)
    out.append("░" * (width - filled), style=f"dim {color}")
    out.append(f" ] {progress * 100:,.2f}%", style=f"dim {color}")
    return out


def _state_value(state: Optional[MiningState], field: str) -> Optional[int]:
    return getattr(state, field) if state else None


def _display_state(state: Optional[MiningState], field: str) -> str:
    value = _state_value(state, field)
    return f"{value:,}" if value is not None else "—"


def _format_era(state: Optional[MiningState]) -> str:
    if state is None:
        return "—"
    return f"{state.era + 1:,}"


def _format_epoch_rotation(state: Optional[MiningState]) -> str:
    if state is None:
        return "—"
    seconds = state.epoch_blocks_left * ETH_BLOCK_SECONDS
    minutes, secs = divmod(seconds, 60)
    return f"in {state.epoch_blocks_left:,} blk (~{minutes}m {secs:02d}s)"


def _mint_count(state: Optional[MiningState]) -> Optional[int]:
    if state is None or state.reward <= 0:
        return None
    previous_minted = 0
    for era in range(state.era):
        era_reward = state.reward << (state.era - era)
        previous_minted += ERA_MINTS * era_reward
    current_era_minted = max(state.minted - previous_minted, 0)
    return state.era * ERA_MINTS + current_era_minted // state.reward


def _job_mint_count(job: Optional[MiningJob]) -> Optional[int]:
    if job is None:
        return None
    if job.total_mints is not None:
        return job.total_mints
    return _mint_count(job.state)


def _era_mint_count(job: Optional[MiningJob]) -> Optional[int]:
    mints = _job_mint_count(job)
    if mints is None:
        return None
    return mints % ERA_MINTS


def _format_next_retarget(job: Optional[MiningJob]) -> str:
    mints = _job_mint_count(job)
    if mints is None:
        return "—"
    remainder = mints % RETARGET_MINTS
    remaining = RETARGET_MINTS if remainder == 0 else RETARGET_MINTS - remainder
    return f"{remaining:,} / {RETARGET_MINTS:,} mints"


def _total_mining_progress(state: Optional[MiningState]) -> float:
    if state is None or state.minted <= 0:
        return 0.0
    mining_supply = state.minted + state.remaining
    if mining_supply <= 0:
        return 0.0
    return min(max(state.minted / mining_supply, 0.0), 1.0)


def _format_token(value: Optional[int]) -> str:
    if value is None:
        return "—"
    whole, frac = divmod(value, TOKEN_SCALE)
    if frac == 0:
        return f"{whole:,} HASH"
    frac_text = f"{frac:018d}".rstrip("0")[:6]
    return f"{whole:,}.{frac_text} HASH"


def _format_token_rate(value: Optional[float]) -> str:
    if value is None or value <= 0:
        return "0 HASH/hr"
    tokens = value / TOKEN_SCALE
    if tokens >= 1000:
        text = f"{tokens:,.0f}"
    elif tokens >= 1:
        text = f"{tokens:,.4f}".rstrip("0").rstrip(".")
    else:
        text = f"{tokens:.8f}".rstrip("0").rstrip(".")
    return f"{text} HASH/hr"


def _format_expected_reward_per_hour(hashrate: float, job: MiningJob) -> str:
    if hashrate <= 0 or job.target <= 0 or job.state is None or job.state.reward <= 0:
        return "0 HASH/hr"
    expected_hashes = (1 << 256) / job.target
    expected_mints_per_hour = hashrate * 3600.0 / expected_hashes
    return _format_token_rate(expected_mints_per_hour * job.state.reward)


def _format_elapsed(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m {secs:02d}s"
    return f"{minutes:d}m {secs:02d}s"


def _format_hash(value: bytes) -> str:
    text = "0x" + value.hex()
    if len(text) <= 22:
        return text
    return f"{text[:12]}…{text[-8:]}"


def _format_multiplier(target: int) -> str:
    if target <= 0:
        return "∞x"
    difficulty = (1 << 256) / target
    if difficulty >= 1000:
        return f"{difficulty:,.0f}x"
    if difficulty >= 10:
        return f"{difficulty:,.2f}x"
    return f"{difficulty:,.4f}x"


def _solution_probability(hashes: int, target: int) -> float:
    if hashes <= 0 or target <= 0:
        return 0.0
    expected_hashes = (1 << 256) / target
    return 1.0 - math.exp(-hashes / expected_hashes)


def _round_progress(job: MiningJob, refresh_seconds: float) -> float:
    if refresh_seconds <= 0:
        return 1.0
    return min(max((time.time() - job.fetched_at) / refresh_seconds, 0.0), 1.0)


def _format_hashrate(h_per_s: float) -> str:
    for unit in ("H/s", "KH/s", "MH/s", "GH/s"):
        if h_per_s < 1000:
            return f"{h_per_s:,.2f} {unit}"
        h_per_s /= 1000
    return f"{h_per_s:,.2f} TH/s"


def _format_cpu_load() -> str:
    try:
        load_1m = os.getloadavg()[0]
    except (AttributeError, OSError):
        return "—"
    cores = os.cpu_count() or 1
    return f"{min((load_1m / cores) * 100.0, 999.0):.0f}% ({load_1m:.2f}/{cores})"


def _detect_gpu_load() -> Optional[float]:
    for command, parser in (
        (
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            _parse_numeric_lines,
        ),
        (["rocm-smi", "--showuse", "--csv"], _parse_rocm_smi_gpu_load),
        (["ioreg", "-r", "-d", "1", "-w", "0", "-c", "AGXAccelerator"], _parse_ioreg_gpu_load),
    ):
        output = _run_load_command(command)
        if output is None:
            continue
        value = parser(output)
        if value is not None:
            return value
    return None


def _run_load_command(command: list[str]) -> Optional[str]:
    if shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _parse_numeric_lines(output: str) -> Optional[float]:
    values: list[float] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(float(line.rstrip("%")))
        except ValueError:
            continue
    if not values:
        return None
    return sum(values) / len(values)


def _parse_rocm_smi_gpu_load(output: str) -> Optional[float]:
    values: list[float] = []
    for line in output.splitlines():
        if "use" not in line.lower():
            continue
        matches = re.findall(r"\b\d+(?:\.\d+)?\b", line)
        if matches:
            values.append(float(matches[-1]))
    if not values:
        return None
    return sum(values) / len(values)


def _parse_ioreg_gpu_load(output: str) -> Optional[float]:
    values = [
        float(value)
        for value in re.findall(r'"Device Utilization %"\s*=\s*(\d+(?:\.\d+)?)', output)
    ]
    if values:
        return sum(values) / len(values)

    fallback_values = [
        float(value)
        for value in re.findall(r'"(?:Renderer|Tiler) Utilization %"\s*=\s*(\d+(?:\.\d+)?)', output)
    ]
    if not fallback_values:
        return None
    return max(fallback_values)


def _format_percent(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{min(max(value, 0.0), 999.0):.0f}%"


def _submit_mode(config: MinerConfig) -> str:
    if not config.submit:
        return "disabled"
    if config.dry_run:
        return "dry-run"
    return "enabled"


def _value_style(value: str) -> str:
    if value == "—":
        return "dim"
    if value.startswith("↑") or "HASH" in value or value.endswith("x"):
        return "bold green"
    if "mints" in value:
        return "bold green"
    return "bold yellow"
