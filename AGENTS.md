# AGENTS.md

本文件为后续在本仓库工作的编码代理提供项目约定、常用命令和风险提示。修改前请先阅读 `README.md`，它是用户侧安装、运行、调优和安全说明的主文档。
你与我的交流必须全程使用中文，Git 提交信息也必须使用中文，并明确标注“新特性”、“BUG 修复”或“优化/重构”。

## 项目概览

`hash256-miner` 是面向 hash256.org / HASH 代币的 Python 3.10+ GPU 命令行挖矿工具。核心 PoW 规则是：

```text
challenge = keccak256(chainId || contract || miner || epoch)
valid iff keccak256(challenge || nonce) < currentDifficulty
```

项目通过 OpenCL 内核在 GPU 上搜索 nonce，并通过 `web3.py` 与以太坊 JSON-RPC 交互。默认主网合约地址、chain id 和提交函数签名定义在 `hash256_miner/constants.py`。

## 目录结构

- `hash256_miner/__main__.py`：CLI 入口和 argparse 子命令；运行时依赖应按子命令惰性加载。
- `hash256_miner/constants.py`：默认合约地址、主网 chain id、默认 submit signature。
- `hash256_miner/protocol.py`：纯协议逻辑，包括 ABI selector、challenge 构造、uint256 编码和 CPU 验证。
- `hash256_miner/gpu.py`：pyopencl 封装，负责设备选择、内核编译、自适应 work size、批次调度、结果解析、CPU 二次验证和 OpenCL reset 识别。
- `hash256_miner/rpc.py`：web3.py RPC 客户端，负责读取链上状态、ABI override、构建和提交 `mine(uint256)` 交易。
- `hash256_miner/orchestrator.py`：主挖矿循环，负责拉取任务、驱动 GPU、刷新任务、提交结果、普通日志和持久化事件日志。
- `hash256_miner/tui.py`：可选 Rich TUI 仪表盘，由 `--tui` 启用。
- `kernels/keccak256_miner.cl`：OpenCL Keccak-256 挖矿内核。
- `build_support/pyinstaller_entry.py`、`hash256-miner.spec`：PyInstaller 打包入口和配置，需包含 OpenCL kernel 文件。
- `tests/`：pytest 测试；真实 GPU kernel 测试在无 OpenCL 设备时自动跳过。
- `pyproject.toml`：包元数据、依赖和 `hash256-miner` console script。

## 环境与安装

要求：

- Python 3.10+
- OpenCL 运行时或 ICD
- 以太坊 JSON-RPC 端点

本地开发安装：

```bash
pip install -e ".[test]"
```

如果只运行 CLI 而不跑测试，可使用：

```bash
pip install -e .
```

本仓库通常使用仓库根目录下的 `.venv` 虚拟环境。执行 Python、pytest 或 CLI 时，优先使用虚拟环境里的命令，避免落到系统 Python：

```bash
.venv/bin/python --version
.venv/bin/python -m pytest
.venv/bin/hash256-miner --help
```

已知本地 `.venv` 使用 Python 3.12.7，并安装了测试依赖；如果直接运行 `python`、`pytest` 或 `hash256-miner` 失败，先检查是否没有载入 `.venv`。

私钥优先通过环境变量传入：

```bash
export MINER_PRIVATE_KEY=0x...
```

不要把真实私钥写入源码、测试、文档示例、事件日志或命令历史敏感位置。

## 常用命令

列出 OpenCL 设备：

```bash
.venv/bin/hash256-miner devices
```

运行基准测试：

```bash
.venv/bin/hash256-miner benchmark --seconds 30
```

只寻找解、不广播交易：

```bash
.venv/bin/hash256-miner mine --address 0xYourMinerAddress --rpc https://eth.llamarpc.com --no-submit
```

运行测试：

```bash
.venv/bin/python -m pytest
```

只跑协议测试：

```bash
.venv/bin/python -m pytest tests/test_protocol.py
```

只跑不依赖真实 OpenCL 设备的 GPU helper 测试：

```bash
.venv/bin/python -m pytest tests/test_gpu.py
```

只跑真实 GPU 内核测试：

```bash
.venv/bin/python -m pytest tests/test_gpu_kernel.py
```

## GPU 调度与 nonce 布局

35cb650 引入了 work-item 内 nonce 循环和自适应 work size，修改相关代码时必须保持以下不变量：

- `HASH256_NONCES_PER_ITEM` 控制每个 OpenCL work-item 在 kernel 内连续扫描的 nonce 数，默认 64，只允许 1 到 256 之间的 2 的幂。
- 实际每批哈希数是 `global_size * HASH256_NONCES_PER_ITEM`，不是单纯的 `global_size`。
- nonce 分层为 `192-bit 随机主机基值 || (64 - log2N)-bit batch_index || (32 + log2N)-bit GPU 段`，其中 N = `HASH256_NONCES_PER_ITEM`。
- GPU 段由 `gid_base * N + i` 填充，`i` 是 work-item 内循环偏移；host 端必须预先清零 `w3` 的低 `32 + log2N` 位，让 kernel 用 OR 写入。
- 未显式传 `--local-size` / `--global-size` 时，`gpu.auto_work_size()` 选择 `local = min(256, device.max_work_group_size)`，`global = max(4M, compute_units * local * HASH256_OVER_SUBSCRIBE)`，`HASH256_OVER_SUBSCRIBE` 默认 256。
- OpenCL build options 必须继续传入 `-DNONCES_PER_ITEM=...`；NVIDIA 的 vendor options 和 `HASH256_OPENCL_BUILD_OPTIONS` / `HASH256_DISABLE_VENDOR_OPTIONS` 仍应保留。

如果改动 PoW 公式、nonce 编码、字节序、target 比较或 GPU 调度，必须同步检查 `protocol.py`、`gpu.py`、`kernels/keccak256_miner.cl` 和相关测试。

## 编码约定

- 保持模块边界清晰：纯数学和编码逻辑放在 `protocol.py`，默认常量放在 `constants.py`，链交互放在 `rpc.py`，OpenCL 设备和内核调度放在 `gpu.py`，循环控制和 reporter 放在 `orchestrator.py`，TUI 放在 `tui.py`。
- OpenCL 内核返回的候选解必须继续在 CPU 上通过 `protocol.verify_solution` 等价逻辑复核后再提交，不要移除这层保护。
- 默认 ABI 已对齐当前主网合约，但必须保留 `--abi-override` 和 `--submit-signature` 的可配置能力。override key 包括 `challenge_for`、`difficulty`、`mining_state`、`total_mints`、`balance`。
- CLI 行为应保持脚本友好：错误返回非零状态码，用户可通过环境变量提供 `ETH_RPC_URL` 和 `MINER_PRIVATE_KEY`。
- `__main__.py` 应继续惰性导入 pyopencl / web3 / rich 等运行时依赖，让 `--help`、`verify` 和缺依赖场景保持友好错误。
- 默认 RPC 请求间隔和失败退避是公共 RPC 保护；不要在测试或默认路径里高频打公共端点。
- 代码风格以现有文件为准：类型标注、dataclass、小函数、少量必要注释。
- 除非用户明确要求，不要引入大型框架或后台服务；这是一个小型 CLI 项目。

## 测试注意事项

- `tests/test_protocol.py` 不依赖 OpenCL，适合快速验证协议改动。
- `tests/test_gpu.py` 使用 mock device，覆盖 `auto_work_size()` 和 `_resolve_nonces_per_item()`，不依赖真实 OpenCL 设备。
- `tests/test_gpu_kernel.py` 依赖 `pyopencl` 和可用 OpenCL 平台；没有设备时会 skip。
- `tests/test_cli.py` 覆盖 CLI 惰性加载、默认 work size 参数、事件日志、OpenCL reset 报错和私钥诊断脱敏。
- `tests/test_rpc.py`、`tests/test_orchestrator.py`、`tests/test_tui.py` 不需要真实链或真实私钥；新增 RPC、gas、签名、交易构建、reporter 或 TUI 行为时应补充这些测试。
- 任何触及 `kernels/keccak256_miner.cl`、`hash256_miner/gpu.py` 或 nonce 布局的改动，都应至少运行 `tests/test_gpu.py` 和 `tests/test_gpu_kernel.py`；若当前机器没有 OpenCL 设备，在最终说明中明确 GPU kernel 测试缺口。
- 任何触及 RPC、gas、签名或交易构建的改动，都应补充或更新不需要真实私钥和真实链上提交的单元测试。

## 安全与链上风险

- 真实挖矿会消耗 gas；默认优先使用 `--no-submit` 和 `--dry-run` 路径进行验证。
- `MINER_PRIVATE_KEY` 和 `--private-key` 只应在本进程内用于本地签名，不应记录到 stdout、stderr、事件日志或测试输出。
- `hash256-miner-events.log` 默认记录关键事件；不要把私钥、完整敏感凭据或 RPC 密钥写入该文件。
- HASH 合约源码已公开验证，但仍避免在代码、文档或提交里声称“已与链上完全匹配且无风险”，除非用户提供或确认了已验证 ABI 和审计结论。
- 不要在测试中广播交易，不要默认连接主网执行有成本的操作。

## 发布或交付前检查

在可行时执行：

```bash
.venv/bin/python -m pytest
```

如果改动影响 CLI，也手动检查：

```bash
.venv/bin/hash256-miner --help
.venv/bin/hash256-miner verify --challenge 0x0000000000000000000000000000000000000000000000000000000000000000 --nonce 0 --target 0
```

预期第二条命令输出 `INVALID` 且退出码为 1；这是正常行为。

如果改动影响 GPU 调优路径，可额外做 benchmark sweep：

```bash
for n in 16 32 64 128; do
    echo "=== N=$n ==="
    HASH256_NONCES_PER_ITEM=$n .venv/bin/hash256-miner benchmark --seconds 30
done
```
