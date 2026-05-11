from hash256_miner import tui


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
