from hash256_miner import tui
from hash256_miner.rpc import MiningJob, MiningState


def _job(state, *, total_mints=None):
    return MiningJob(
        challenge=b"\x00" * 32,
        target=1,
        epoch=state.epoch,
        fetched_at=0,
        state=state,
        total_mints=total_mints,
    )


def test_parse_nvidia_smi_gpu_load_averages_multiple_devices():
    assert tui._parse_numeric_lines("95\n40\n") == 67.5


def test_parse_rocm_smi_gpu_load_reads_gpu_use_lines():
    output = """card,Sensor,Value
0,GPU use (%),82
1,GPU use (%),18
"""

    assert tui._parse_rocm_smi_gpu_load(output) == 50.0


def test_parse_ioreg_gpu_load_prefers_device_utilization():
    output = '"PerformanceStatistics" = {"Renderer Utilization %"=25,"Device Utilization %"=70}'

    assert tui._parse_ioreg_gpu_load(output) == 70.0


def test_parse_ioreg_gpu_load_falls_back_to_renderer_or_tiler_utilization():
    output = '"PerformanceStatistics" = {"Renderer Utilization %"=25,"Tiler Utilization %"=40}'

    assert tui._parse_ioreg_gpu_load(output) == 40.0


def test_format_percent_handles_missing_and_clamps_values():
    assert tui._format_percent(None) == "—"
    assert tui._format_percent(-1) == "0%"
    assert tui._format_percent(42.4) == "42%"


def test_total_mining_progress_uses_minted_over_mining_supply():
    state = MiningState(
        era=0,
        reward=100 * tui.TOKEN_SCALE,
        difficulty=1,
        minted=1_742_580 * tui.TOKEN_SCALE,
        remaining=17_157_420 * tui.TOKEN_SCALE,
        epoch=123,
        epoch_blocks_left=42,
    )

    assert tui._total_mining_progress(state) == 0.0922


def test_era_display_is_one_based_like_the_site():
    state = MiningState(
        era=0,
        reward=100 * tui.TOKEN_SCALE,
        difficulty=1,
        minted=0,
        remaining=18_900_000 * tui.TOKEN_SCALE,
        epoch=123,
        epoch_blocks_left=42,
    )

    assert tui._format_era(state) == "1"


def test_era_and_retarget_counts_use_total_mints_when_available():
    state = MiningState(
        era=1,
        reward=50 * tui.TOKEN_SCALE,
        difficulty=1,
        minted=10_005_000 * tui.TOKEN_SCALE,
        remaining=8_895_000 * tui.TOKEN_SCALE,
        epoch=123,
        epoch_blocks_left=42,
    )
    job = _job(state, total_mints=100_100)

    assert tui._era_mint_count(job) == 100
    assert tui._format_next_retarget(job) == "700 / 2,016 mints"


def test_mint_count_fallback_accounts_for_prior_halving_eras():
    state = MiningState(
        era=1,
        reward=50 * tui.TOKEN_SCALE,
        difficulty=1,
        minted=10_005_000 * tui.TOKEN_SCALE,
        remaining=8_895_000 * tui.TOKEN_SCALE,
        epoch=123,
        epoch_blocks_left=42,
    )

    assert tui._mint_count(state) == 100_100


def test_expected_reward_per_hour_uses_hashrate_target_and_reward():
    state = MiningState(
        era=0,
        reward=tui.TOKEN_SCALE,
        difficulty=1 << 255,
        minted=0,
        remaining=18_900_000 * tui.TOKEN_SCALE,
        epoch=123,
        epoch_blocks_left=42,
    )
    job = _job(state)
    job.target = 1 << 255

    assert tui._format_expected_reward_per_hour(2.0, job) == "3,600 HASH/hr"


def test_elapsed_and_hash_formatters_are_compact():
    assert tui._format_elapsed(65) == "1m 05s"
    assert tui._format_elapsed(3661) == "1h 01m 01s"

    formatted = tui._format_hash(bytes.fromhex("11" * 32))
    assert formatted.startswith("0x1111111111")
    assert formatted.endswith("11111111")
