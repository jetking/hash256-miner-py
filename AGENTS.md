# AGENTS.md

本文件为后续在本仓库工作的编码代理提供项目约定、常用命令和风险提示。修改前请先阅读 `README.md`，它是用户侧安装、运行和安全说明的主文档。
你与我的交流必须全程使用中言语， GIT 提交也必须使用中文。

## 项目概览

`hash256-miner` 是面向 hash256.org / HASH 代币的 Python 3.10+ GPU 命令行挖矿工具。核心 PoW 规则是：

```text
challenge = keccak256(chainId || contract || miner || epoch)
valid iff keccak256(challenge || nonce) < currentDifficulty
```

项目通过 OpenCL 内核在 GPU 上搜索 nonce，并通过 `web3.py` 与以太坊 JSON-RPC 交互。默认合约地址和主网 chain id 定义在 `hash256_miner/protocol.py`。

## 目录结构

- `hash256_miner/__main__.py`：CLI 入口和 argparse 子命令。
- `hash256_miner/protocol.py`：纯协议逻辑，包括 ABI selector、challenge 构造、uint256 编码和 CPU 验证。
- `hash256_miner/gpu.py`：pyopencl 封装，负责设备选择、内核编译、批次调度、结果解析和 CPU 二次验证。
- `hash256_miner/rpc.py`：web3.py RPC 客户端，负责读取链上状态、构建和提交 mint 交易。
- `hash256_miner/orchestrator.py`：主挖矿循环，负责拉取任务、驱动 GPU、刷新任务和提交结果。
- `kernels/keccak256_miner.cl`：OpenCL Keccak-256 挖矿内核。
- `tests/`：pytest 测试；GPU 测试在无 OpenCL 设备时自动跳过。
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

不要把真实私钥写入源码、测试、文档示例或命令历史敏感位置。

## 常用命令

列出 OpenCL 设备：

```bash
hash256-miner devices
```

运行基准测试：

```bash
hash256-miner benchmark --seconds 30
```

只寻找解、不广播交易：

```bash
hash256-miner mine --address 0xYourMinerAddress --rpc https://eth.llamarpc.com --no-submit
```

运行测试：

```bash
.venv/bin/python -m pytest
```

只跑协议测试：

```bash
.venv/bin/python -m pytest tests/test_protocol.py
```

只跑 GPU 内核测试：

```bash
.venv/bin/python -m pytest tests/test_gpu_kernel.py
```

## 编码约定

- 保持模块边界清晰：纯数学和编码逻辑放在 `protocol.py`，链交互放在 `rpc.py`，OpenCL 设备和内核调度放在 `gpu.py`，循环控制放在 `orchestrator.py`。
- 修改 PoW 公式、nonce 编码、字节序或 target 比较时，必须同步检查 Python CPU 验证、OpenCL 内核和测试。
- OpenCL 内核返回的候选解必须继续在 CPU 上验证后再提交，不要移除这层保护。
- ABI 名称仍有不确定性；保留 `--abi-override` 和 `--submit-signature` 的可配置能力，不要把未验证的合约 ABI 写死到核心逻辑里。
- CLI 行为应保持脚本友好：错误返回非零状态码，用户可通过环境变量提供 `ETH_RPC_URL` 和 `MINER_PRIVATE_KEY`。
- 代码风格以现有文件为准：类型标注、dataclass、小函数、少量必要注释。
- 除非用户明确要求，不要引入大型框架或后台服务；这是一个小型 CLI 项目。

## 测试注意事项

- `tests/test_protocol.py` 不依赖 OpenCL，适合快速验证协议改动。
- `tests/test_gpu_kernel.py` 依赖 `pyopencl` 和可用 OpenCL 平台；没有设备时会 skip。
- 任何触及 `kernels/keccak256_miner.cl`、`hash256_miner/gpu.py` 或 nonce 布局的改动，都应至少运行 GPU 测试；若当前机器没有 OpenCL 设备，在最终说明中明确测试缺口。
- 任何触及 RPC、gas、签名或交易构建的改动，都应补充或更新不需要真实私钥和真实链上提交的单元测试。

## 安全与链上风险

- 真实挖矿会消耗 gas；默认优先支持 `--no-submit` 和 `--dry-run` 路径进行验证。
- `MINER_PRIVATE_KEY` 和 `--private-key` 只应在本进程内用于本地签名，不应记录到日志。
- HASH 合约 ABI 可能与当前默认猜测不同；提交相关改动时避免声称已经与链上合约完全匹配，除非用户提供或确认了已验证 ABI。
- 不要在测试中广播交易，不要默认连接主网执行有成本的操作。

## 发布或交付前检查

在可行时执行：

```bash
.venv/bin/python -m pytest
```

如果改动影响 CLI，也手动检查：

```bash
hash256-miner --help
hash256-miner verify --challenge 0x0000000000000000000000000000000000000000000000000000000000000000 --nonce 0 --target 0
```

预期第二条命令输出 `INVALID` 且退出码为 1；这是正常行为。
