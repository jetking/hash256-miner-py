#Requires -Version 5.1
<#
.SYNOPSIS
    在 Windows 上跑 hash256-miner 基准并自动找出 GPU 最佳参数。

.DESCRIPTION
    1. 跑基线（默认 60 秒 × 3）
    2. 扫 HASH256_NONCES_PER_ITEM（A1 内核 inner loop，1/16/32/64/128/256）
    3. 用上一步最佳 N 扫 HASH256_OVER_SUBSCRIBE（A2 调度密度，64/128/256/512）
    4. 用最佳 N + 最佳 OVER 跑一次确认（60 秒）
    5. 打印推荐的环境变量

    总耗时约 10 分钟。所有结果在屏幕上输出，并写入 win_benchmark_result.txt。

.NOTES
    - 必须从仓库根目录运行：.\scripts\win_benchmark.ps1
    - 默认从 .venv\Scripts\hash256-miner.exe 找矿工程序，找不到再回退到 PATH。
    - 若执行策略阻拦，用：
        powershell -ExecutionPolicy Bypass -File scripts\win_benchmark.ps1
    - 故意避开 OVER_SUBSCRIBE=1024：在 RTX 5080 + N≥64 时单 batch
      会接近 1 秒，Windows 显卡驱动 TDR 可能误杀进程。需要时改 -ExtendedOver。

.EXAMPLE
    .\scripts\win_benchmark.ps1
    # 默认 baseline=60s, sweep=30s

.EXAMPLE
    .\scripts\win_benchmark.ps1 -BaselineSeconds 90 -SweepSeconds 45
    # 更长采样，结果更稳但总时间更长

.EXAMPLE
    .\scripts\win_benchmark.ps1 -ExtendedOver
    # 把 OVER_SUBSCRIBE 上限扩到 1024，注意 TDR 风险
#>

[CmdletBinding()]
param(
    [int] $BaselineSeconds = 60,
    [int] $SweepSeconds = 30,
    [string] $MinerExe = "",
    [string] $OutputFile = "win_benchmark_result.txt",
    [switch] $ExtendedOver
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 找矿工可执行程序
# ---------------------------------------------------------------------------
if (-not $MinerExe) {
    $candidate = Join-Path (Get-Location) ".venv\Scripts\hash256-miner.exe"
    if (Test-Path $candidate) {
        $MinerExe = $candidate
    } else {
        $cmd = Get-Command "hash256-miner" -ErrorAction SilentlyContinue
        if ($cmd) {
            $MinerExe = $cmd.Source
        } else {
            Write-Host "ERROR: 找不到 hash256-miner.exe。" -ForegroundColor Red
            Write-Host "请先在仓库根目录建 venv 并安装："
            Write-Host "  python -m venv .venv"
            Write-Host "  .venv\Scripts\Activate.ps1"
            Write-Host "  pip install -e ."
            Write-Host "或者用 -MinerExe 显式指定路径。"
            exit 1
        }
    }
}

Write-Host "使用矿工: $MinerExe" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# 工具函数：跑一次 benchmark，解析 MH/s，环境变量自动还原
# ---------------------------------------------------------------------------
function Invoke-Bench {
    param(
        [int] $Seconds,
        [hashtable] $EnvVars = @{}
    )

    $originals = @{}
    foreach ($k in $EnvVars.Keys) {
        $originals[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
        [Environment]::SetEnvironmentVariable($k, $EnvVars[$k], "Process")
    }

    try {
        $output = & $MinerExe benchmark --seconds $Seconds 2>&1 | Out-String
    } catch {
        Write-Warning "矿工进程异常: $_"
        $output = ""
    } finally {
        foreach ($k in $originals.Keys) {
            [Environment]::SetEnvironmentVariable($k, $originals[$k], "Process")
        }
    }

    if ($output -match 'Hashrate\s*:\s*[\d,]+\s*H/s\s*\(([\d.]+)\s*MH/s\)') {
        return [double]$Matches[1]
    }
    Write-Warning "无法解析输出："
    Write-Host $output -ForegroundColor DarkGray
    return $null
}

function Format-Rate {
    param([Nullable[double]] $Rate)
    if ($null -eq $Rate) { return "FAILED" }
    return "$([math]::Round($Rate, 2)) MH/s"
}

# ---------------------------------------------------------------------------
# 输出辅助：同时打到屏幕和文件
# ---------------------------------------------------------------------------
$transcript = [System.Collections.Generic.List[string]]::new()
function Log {
    param([string] $Line, [string] $Color = "White")
    Write-Host $Line -ForegroundColor $Color
    $transcript.Add($Line) | Out-Null
}

Log ""
Log "==========================================" "Yellow"
Log " hash256-miner Windows GPU 基准 + 调优" "Yellow"
Log " 开始时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" "Yellow"
Log "==========================================" "Yellow"

# ---------------------------------------------------------------------------
# 设备信息
# ---------------------------------------------------------------------------
Log ""
Log "=== OpenCL 设备 ===" "Cyan"
$devicesOut = & $MinerExe devices 2>&1 | Out-String
Log $devicesOut.TrimEnd()

# ---------------------------------------------------------------------------
# 1. 基线（默认参数）
# ---------------------------------------------------------------------------
Log ""
Log "=== 1) 基线 ($BaselineSeconds s × 3, 默认参数) ===" "Cyan"
$baselines = @()
for ($i = 1; $i -le 3; $i++) {
    Write-Host "    run $i ... " -NoNewline
    $r = Invoke-Bench -Seconds $BaselineSeconds
    Write-Host (Format-Rate $r)
    if ($null -ne $r) { $baselines += $r }
}
if ($baselines.Count -eq 0) {
    Log "基线全部失败，终止。" "Red"
    exit 1
}
$baseline = ($baselines | Measure-Object -Average).Average
Log ("基线平均: {0:N2} MH/s" -f $baseline) "Green"

# ---------------------------------------------------------------------------
# 2. A1 sweep：HASH256_NONCES_PER_ITEM
# ---------------------------------------------------------------------------
Log ""
Log "=== 2) A1 扫描 HASH256_NONCES_PER_ITEM ($SweepSeconds s × 6) ===" "Cyan"
$a1Values = 1, 16, 32, 64, 128, 256
$a1Results = [ordered]@{}
foreach ($n in $a1Values) {
    Write-Host "    N = $n ... " -NoNewline
    $r = Invoke-Bench -Seconds $SweepSeconds -EnvVars @{
        "HASH256_NONCES_PER_ITEM" = "$n"
    }
    Write-Host (Format-Rate $r)
    $a1Results[$n] = $r
}

$bestNEntry = $a1Results.GetEnumerator() |
    Where-Object { $null -ne $_.Value } |
    Sort-Object Value -Descending |
    Select-Object -First 1
if (-not $bestNEntry) {
    Log "A1 扫描全部失败，终止。" "Red"
    exit 1
}
$bestN = [int]$bestNEntry.Key
$bestNRate = [double]$bestNEntry.Value

Log ""
Log "A1 结果一览：" "Green"
foreach ($k in $a1Values) {
    $marker = if ($k -eq $bestN) { "  <-- 最佳" } else { "" }
    Log ("  N={0,3}  ->  {1}{2}" -f $k, (Format-Rate $a1Results[$k]), $marker)
}

# ---------------------------------------------------------------------------
# 3. A2 sweep：HASH256_OVER_SUBSCRIBE（用上一步最佳 N）
# ---------------------------------------------------------------------------
$overValues = if ($ExtendedOver) { 64, 128, 256, 512, 1024 } else { 64, 128, 256, 512 }
Log ""
Log "=== 3) A2 扫描 HASH256_OVER_SUBSCRIBE ($SweepSeconds s × $($overValues.Count), N=$bestN) ===" "Cyan"
if (-not $ExtendedOver) {
    Log "  （默认跳过 OVER=1024 以规避 TDR；想测就加 -ExtendedOver）" "DarkGray"
}
$a2Results = [ordered]@{}
foreach ($s in $overValues) {
    Write-Host "    OVER = $s ... " -NoNewline
    $r = Invoke-Bench -Seconds $SweepSeconds -EnvVars @{
        "HASH256_NONCES_PER_ITEM" = "$bestN"
        "HASH256_OVER_SUBSCRIBE"  = "$s"
    }
    Write-Host (Format-Rate $r)
    $a2Results[$s] = $r
}

$bestOverEntry = $a2Results.GetEnumerator() |
    Where-Object { $null -ne $_.Value } |
    Sort-Object Value -Descending |
    Select-Object -First 1
if (-not $bestOverEntry) {
    Log "A2 扫描全部失败，使用默认 OVER=256。" "Yellow"
    $bestOver = 256
    $bestOverRate = $null
} else {
    $bestOver = [int]$bestOverEntry.Key
    $bestOverRate = [double]$bestOverEntry.Value
}

Log ""
Log "A2 结果一览：" "Green"
foreach ($k in $overValues) {
    $marker = if ($k -eq $bestOver) { "  <-- 最佳" } else { "" }
    Log ("  OVER={0,4}  ->  {1}{2}" -f $k, (Format-Rate $a2Results[$k]), $marker)
}

# ---------------------------------------------------------------------------
# 4. 最终确认（最佳 N + 最佳 OVER, 60s）
# ---------------------------------------------------------------------------
Log ""
Log "=== 4) 最终确认 (N=$bestN, OVER=$bestOver, $BaselineSeconds s × 3) ===" "Cyan"
$finals = @()
for ($i = 1; $i -le 3; $i++) {
    Write-Host "    run $i ... " -NoNewline
    $r = Invoke-Bench -Seconds $BaselineSeconds -EnvVars @{
        "HASH256_NONCES_PER_ITEM" = "$bestN"
        "HASH256_OVER_SUBSCRIBE"  = "$bestOver"
    }
    Write-Host (Format-Rate $r)
    if ($null -ne $r) { $finals += $r }
}
if ($finals.Count -eq 0) {
    Log "最终确认失败。" "Red"
    exit 1
}
$final = ($finals | Measure-Object -Average).Average
Log ("最终平均: {0:N2} MH/s" -f $final) "Green"

# ---------------------------------------------------------------------------
# 5. 总结 & 推荐
# ---------------------------------------------------------------------------
$improvement = if ($baseline -gt 0) { (($final - $baseline) / $baseline) * 100 } else { 0 }

Log ""
Log "==========================================" "Yellow"
Log " 总结" "Yellow"
Log "==========================================" "Yellow"
Log ("基线（默认）        : {0,8:N2} MH/s" -f $baseline)
Log ("调优后（N+OVER）    : {0,8:N2} MH/s" -f $final)
Log ("相对基线提升        : {0,+8:N2} %" -f $improvement) $(if ($improvement -ge 1) { "Green" } else { "Yellow" })
Log ""
Log "最佳参数：" "Yellow"
Log "  HASH256_NONCES_PER_ITEM = $bestN"
Log "  HASH256_OVER_SUBSCRIBE  = $bestOver"
Log ""

if ([math]::Abs($improvement) -lt 1) {
    Log "提升幅度 < 1%，落在测量噪声内。建议保留默认参数（N=64, OVER=256）。" "Yellow"
    Log "如果你确实想用调优值，可在 PowerShell 设环境变量后再启动 mine：" "DarkGray"
} else {
    Log "建议在 PowerShell 中设置以下环境变量后再启动挖矿：" "Green"
}
Log ""
Log "  # 当前 PowerShell 会话内生效" "DarkGray"
Log "  `$env:HASH256_NONCES_PER_ITEM = `"$bestN`""
Log "  `$env:HASH256_OVER_SUBSCRIBE  = `"$bestOver`""
Log "  hash256-miner mine --address 0xYourAddress --rpc https://eth.llamarpc.com --no-submit"
Log ""
Log "  # 永久生效（用户级环境变量）" "DarkGray"
Log "  [Environment]::SetEnvironmentVariable('HASH256_NONCES_PER_ITEM', '$bestN', 'User')"
Log "  [Environment]::SetEnvironmentVariable('HASH256_OVER_SUBSCRIBE',  '$bestOver', 'User')"
Log ""
Log ("结束时间: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) "DarkGray"

# 写入结果文件
try {
    $transcript -join "`r`n" | Set-Content -Path $OutputFile -Encoding UTF8
    Write-Host ""
    Write-Host "完整报告已保存到: $OutputFile" -ForegroundColor DarkGray
} catch {
    Write-Warning "写入 $OutputFile 失败: $_"
}
