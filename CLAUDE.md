# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 沟通与提交规范

- 与用户的所有对话以及 Git 提交信息必须使用中文（来源：`AGENTS.md`）。
- 提交信息需明确标注是“新特性”、“BUG 修复”还是“优化/重构”。

## 项目定位

`hash256-miner` 是面向 [hash256.org](https://hash256.org) HASH 代币（以太坊主网）的 Python GPU 命令行挖矿工具。PoW 规则：

```
challenge = keccak256(chainId ‖ contract ‖ miner ‖ epoch)
valid iff keccak256(challenge ‖ nonce) < currentDifficulty
```

默认合约地址 `0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc`、`chainId=1`、`mine(uint256)` 定义在 `hash256_miner/constants.py`。

## 环境

- Python 3.10+，仓库本地 `.venv` 使用 3.12.7 并已安装测试依赖。**始终优先使用 `.venv/bin/python` / `.venv/bin/pytest` / `.venv/bin/hash256-miner`**，避免回落到系统 Python。
- OpenCL 运行时（NVIDIA 驱动自带 / AMD ROCm / Intel ICD / macOS 内置 / PoCL 作 CPU 回退）。
- 测试需要 `pip install -e ".[test]"`；只跑 CLI 用 `pip install -e .`。
- 私钥通过环境变量 `MINER_PRIVATE_KEY` 传入，不要写入源码、测试或提交。

## 常用命令

```bash
# 列出 OpenCL 设备
.venv/bin/hash256-miner devices

# 基准测试（不连接链）
.venv/bin/hash256-miner benchmark --seconds 30

# 离线挖矿（只找解，不广播）
.venv/bin/hash256-miner mine --address 0xYOUR --rpc https://eth.llamarpc.com --no-submit

# 全量测试
.venv/bin/python -m pytest

# 单个测试文件
.venv/bin/python -m pytest tests/test_protocol.py
.venv/bin/python -m pytest tests/test_gpu_kernel.py   # 无 OpenCL 设备会 skip

# 单个用例
.venv/bin/python -m pytest tests/test_rpc.py::test_name -xvs
```

`verify` 子命令可用于回归 PoW 数学：

```bash
.venv/bin/hash256-miner verify --challenge 0x00..00 --nonce 0 --target 0
```

预期 `INVALID` + 退出码 1（这是正常行为，`AGENTS.md` 的发布前检查依赖它）。

## 架构（big-picture）

CLI 入口 → orchestrator 主循环 → RPC 拉任务 / GPU 跑哈希 / RPC 提交解。所有模块边界**严格按职责划分**，跨模块改动时必须同步更新对应文件、内核和测试：

```
hash256_miner/
├── __main__.py        # CLI / argparse；按子命令惰性加载依赖，使 `devices`/`verify` 在缺 pyopencl 时仍可用
├── constants.py       # 合约地址、chainId、默认 submit signature
├── protocol.py        # 纯数学：keccak、challenge 构造、uint256 编码、selector、CPU 验证
├── rpc.py             # web3.py 封装：读取链上状态、构建/签名/发送 mine 交易、ABI override
├── gpu.py             # pyopencl 封装：设备选择、内核编译、批次调度、CPU 二次验证、device reset 检测
├── orchestrator.py    # 主循环：拉任务 → 调 GPU → 找到解 → 提交；含 ConsoleReporter / PersistentEventReporter
└── tui.py             # 可选 Rich TUI 仪表盘（--tui）
kernels/keccak256_miner.cl                # OpenCL Keccak-f[1600] 挖矿内核
build_support/pyinstaller_entry.py + hash256-miner.spec   # PyInstaller 打包
```

关键不变量（修改时必须同步）：

- **GPU 候选解必须先在 CPU 端通过 `protocol.verify_solution` 复核才能提交**。不要去掉这层保护——内核 bug 不应让用户烧 gas。修改 PoW 公式 / nonce 编码 / 字节序 / target 比较时，**`protocol.py` + `kernels/keccak256_miner.cl` + 对应测试**三处必须一起改。
- **nonce 布局**：256 位 nonce 拆为 `192-bit 主机随机基值 ‖ (64 − log₂N) bit batch_index ‖ (32 + log₂N) bit GPU 段`，其中 N = `HASH256_NONCES_PER_ITEM`（默认 64，2 的幂，1–256）。GPU 段由 `gid_base × N + i`（i 为 work-item 内循环偏移）填充。改 GPU 调度时不要破坏这个分层。
- **ABI 可覆盖**：默认 ABI 对齐当前主网合约，但 `--abi-override` 与 `--submit-signature` 必须保持可配置；不要把“已确认 ABI”硬编码进核心逻辑。允许的 override key：`challenge_for / difficulty / mining_state / total_mints / balance`，submit signature 默认 `mine(uint256)`。
- **CLI 依赖惰性加载**：`__main__.py` 用 `_load_*_dependencies()` 封装 pyopencl/web3 导入，让 `devices` 和 `verify` 在依赖缺失时给出友好错误。新增子命令请沿用这一模式。
- **RPC 限流**：默认 `--rpc-min-interval=1`，失败时退避递增；不要在测试或默认路径里打爆公共 RPC。

## 测试要点

- `tests/test_protocol.py`：纯 CPU，最快回归点。
- `tests/test_gpu.py`：纯 Python（mock device），覆盖 `auto_work_size` / `_resolve_nonces_per_item`，无 OpenCL 也能跑。
- `tests/test_gpu_kernel.py`：依赖 `pyopencl` + 真实 OpenCL 设备，无设备自动 skip——若改了内核或 `gpu.py` 但本机无 GPU，请在最终说明中明示测试缺口。
- `tests/test_rpc.py` / `test_orchestrator.py` / `test_cli.py` / `test_tui.py`：不需要真实链/私钥；新增 RPC、gas、签名或交易构建相关改动时必须扩展这些用例，**绝不允许在测试中广播主网交易**。

## 安全 / 链上风险

- 真实挖矿消耗真实 gas；任何疑似改动到提交路径时，默认走 `--no-submit` 或 `--dry-run` 验证。
- `MINER_PRIVATE_KEY` 仅在本进程内用于本地签名，**不要写入日志、事件文件或测试**。`hash256-miner-events.log`（默认事件日志）也不应包含私钥相关信息。
- HASH 合约为匿名团队部署且未经审计，避免在文档/提交里声称“已与链上完全匹配”，除非用户明确确认。

## 打包

PyInstaller 入口 `build_support/pyinstaller_entry.py`，spec 文件 `hash256-miner.spec`，会把 `kernels/keccak256_miner.cl` 一并打包进发行包。

## 更多细节

`README.md` 是用户侧权威文档（CLI 选项、调优、ABI override 表）；`AGENTS.md` 是 agent 侧约定与发布前检查清单。改动前先阅读这两份文件。
