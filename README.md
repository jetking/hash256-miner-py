# hash256-miner

面向 [hash256.org](https://hash256.org/) 的 GPU 命令行挖矿工具（HASH 代币，以太坊主网）。

实现了 HASH 白皮书中描述的 keccak256 预映像 PoW：

```
challenge = keccak256(chainId ‖ contract ‖ miner ‖ epoch)
valid iff keccak256(challenge ‖ nonce) < currentDifficulty
```

官方网站目前只支持浏览器挖矿。这个工具通过 OpenCL 在你的 GPU 上运行同一个谜题。

## 状态

HASH 合约源码已公开验证。当前默认 ABI 对齐主网合约：
`getChallenge(address)`、`miningState()` 和 `mine(uint256)`。如果后续合约或分叉版本更改函数名，
仍可通过 `--abi-override` 和 `--submit-signature` 调整。

## 为什么使用 GPU？

白皮书强调浏览器 CPU 挖矿（“no GPU”）。这是出于公平性的设计选择，并不是技术限制。这个谜题就是普通的
keccak256 预映像问题，在 GPU 上运行通常比 WASM 快约 100 到 1000 倍。这个工具面向希望在自己的硬件上运行，并与其他同样选择这样做的人处于同等条件下的用户。

## 要求

- Python 3.10+
- 适用于你的 GPU 的 OpenCL 运行时
  - NVIDIA：随驱动提供
  - AMD：`rocm-opencl-runtime` 或 AMD 专有驱动
  - Intel：`intel-opencl-icd`
  - macOS：内置
  - CPU 回退（用于测试）：`pocl-opencl-icd`
- 一个以太坊 JSON-RPC 端点（Infura、Alchemy、你自己的节点等）

## 安装

```bash
git clone <this-repo> hash256-miner
cd hash256-miner
pip install -e .
```

这会安装 `hash256-miner` 命令。

Windows 下请在仓库目录中使用同一个 Python 启动器安装依赖：

```powershell
py -m pip install -e .
py -m hash256_miner --help
```

如果只是复制源码后直接运行 `py -m hash256_miner`，会缺少 `numpy`、`pyopencl`、`web3`
等依赖并导致导入失败。

## 快速开始

```bash
# 1. 检查你的 GPU 是否可见
hash256-miner devices

# 2. 基准测试（不与链交互）
hash256-miner benchmark --seconds 30

# 3. 挖矿：打印解，但不广播
hash256-miner mine \
    --address 0xYourMinerAddress \
    --rpc https://eth.llamarpc.com \
    --no-submit

# 4. 真实挖矿
export MINER_PRIVATE_KEY=0x...
hash256-miner mine \
    --address 0xYourMinerAddress \
    --rpc https://eth.llamarpc.com \
    --global-size $((1<<22))
```

## 子命令

| 命令 | 作用 |
|---|---|
| `devices` | 列出可用的 OpenCL 平台和设备 |
| `benchmark` | 在选定设备上测量哈希率，不连接链 |
| `verify` | 使用 CPU 验证一组 `(challenge, nonce, target)` |
| `mine` | 真正执行挖矿：获取任务、挖矿、提交 |

运行 `hash256-miner <cmd> --help` 查看完整选项列表。

## 配置

### 设备选择

```bash
hash256-miner mine --platform 0 --device 1 ...    # 平台 0 上的 AMD/NVIDIA，设备 1
```

`--platform`/`--device` 是来自 `hash256-miner devices` 的索引。不传这些参数时，工具会选择找到的第一个 GPU；如果没有 GPU，则回退到 CPU 设备。

### 调整批大小

`--global-size N` 是每次内核启动计算的 keccak 哈希数量。默认值 `2^22`（4M）适合现代中端 GPU。高端显卡可以调高（8M 到 32M），笔记本可以调低。

`--local-size` 是 OpenCL 工作组大小。256 是安全默认值；某些硬件更适合 64 或 128。

`--status-seconds` 控制挖矿中的状态输出频率，默认每 2 秒打印一次当前 epoch、job age、下次刷新倒计时、累计 hashes、平均算力、瞬时算力和最近 GPU batch 耗时：

```bash
hash256-miner mine ... --status-seconds 1
```

如果希望使用类似官网风格的实时终端界面，可以启用 TUI。上半区显示纪元、奖励、难度、周期轮换、已铸造、剩余、余额和进度条；设备区显示算力、批次耗时和本机 CPU/GPU 负载；下半区保留最近日志：

```bash
hash256-miner mine ... --tui
```

不传 `--tui` 时仍使用普通日志输出，便于脚本、重定向和 systemd 等场景。

### ABI 覆盖

默认 ABI 已对齐当前主网 HASH 合约。对于任何与默认值不同的函数，可以进行覆盖：

| 此矿工中的默认值 | 覆盖参数 |
|---|---|
| `currentDifficulty()` | `--abi-override difficulty=getDifficulty()` |
| `miningState()` | `--abi-override mining_state=getMiningState()` |
| `getChallenge(address)` | `--abi-override challenge_for=challengeOf(address)` |
| `totalMints()` | `--abi-override total_mints=mintsTotal()` |
| `balanceOf(address)` | `--abi-override balance=balanceOf(address)` |

对于铸造交易本身：

```bash
hash256-miner mine ... --submit-signature "mint(uint256)"
```

（默认值是 `mine(uint256)`。）

### 公共 RPC 限流

公共端点可能会返回 `429` 或 `rate-limited`。矿工默认在 JSON-RPC 请求之间至少等待 1 秒，并在取任务失败时自动递增退避。若仍然被限流，可以调高间隔或换成自己的/付费 RPC：

```bash
hash256-miner mine ... --rpc-min-interval 3 --refresh-seconds 120
```

### Gas

`--priority-fee-gwei` 控制你的优先费。默认值 `1.0` 较为保守；在挖矿热潮中，你可能需要 2 到 10。`--max-fee-gwei` 默认按照 EIP-1559 设置为 `2 * baseFee + tip`。

## 架构

```
       ┌─────────────────────────────────────────────┐
       │           hash256-miner orchestrator         │
       │                                              │
       │   ┌─────────┐   任务   ┌──────────────┐     │
       │   │  RPC    │─────────▶│  GPU miner   │     │
       │   │ client  │          │ (OpenCL)     │     │
       │   │         │   解     │              │     │
       │   │         │◀─────────│              │     │
       │   └────┬────┘          └──────────────┘     │
       │        │                                    │
       │        ▼                                    │
       │   签名 + 提交交易                           │
       └─────────────────────────────────────────────┘
                 │
                 ▼
       Ethereum mainnet — HASH contract
       0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc
```

### 文件

```
hash256-miner/
├── kernels/
│   └── keccak256_miner.cl     # OpenCL Keccak-f[1600] 挖矿内核
├── hash256_miner/
│   ├── protocol.py            # 纯数学：challenge、target 比较、selector
│   ├── rpc.py                 # web3.py RPC：获取任务，构建并发送交易
│   ├── gpu.py                 # pyopencl 粘合层：调度批次，解析结果
│   ├── orchestrator.py        # 主循环：拉取 → 挖矿 → 提交 → 重复
│   └── __main__.py            # CLI / argparse
└── tests/                     # pytest 单元测试
```

### GPU 搜索的工作方式

一个 256 位 nonce 被拆分到三个索引中，以确保搜索不会发生碰撞：

```
   bit  255                                          64 63          32 31           0
        ┌─────────────────────────────────────────────┬────────────────┬──────────────┐
        │   192 位随机基值（主机端，每个会话生成）     │  batch_index   │   OpenCL gid │
        └─────────────────────────────────────────────┴────────────────┴──────────────┘
```

- `gid`（OpenCL 全局工作项 ID）在单次内核启动中的各个工作项之间变化。
- `batch_index` 由主机在每次启动之间递增。
- 随机基值可以防止同一地址上的两个矿工重复搜索完全相同的 nonce 范围。

内核声称找到的每个解都会先在 CPU 上验证，然后才会提交到链上，因此内核 bug 不会让你损失 gas。

## 测试

```bash
pip install -e ".[test]"
pytest
```

在没有任何 OpenCL 设备的机器上，GPU 测试会自动跳过。

## 安全说明

- `--private-key` 的值不会离开本进程。它会通过 `eth_account` 在本地签名交易。
- 相比在命令行上传入私钥，更建议使用 `$MINER_PRIVATE_KEY`（避免 shell 历史、`ps` 等泄露）。
- 这个工具没有遥测、没有自动更新、没有隐藏费用。你可以自己阅读并检查约 600 行 Python 代码。
- HASH 合约由匿名团队开发，且未经审计。挖矿 gas 成本是真金白银；代币价值没有保证。**请自行研究。**

## 许可证

MIT
