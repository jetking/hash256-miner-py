"""CLI entry point: argparse, logging, sub-commands."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from typing import Optional

from . import __version__
from .constants import DEFAULT_CONTRACT, DEFAULT_SUBMIT, MAINNET_CHAIN_ID


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hash256-miner",
        description=(
            "GPU miner for hash256.org. Implements the keccak256-preimage PoW "
            "described in the HASH whitepaper."
        ),
    )
    p.add_argument("--version", action="version", version=f"hash256-miner {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v for INFO, -vv for DEBUG")

    sub = p.add_subparsers(dest="command", required=True)

    # ---------- mine ----------
    mine = sub.add_parser("mine", help="Run the GPU miner against the live contract")
    mine.add_argument("--address", required=True,
                      help="Your miner address (where rewards go). 0x-prefixed.")
    mine.add_argument("--rpc", default=os.environ.get("ETH_RPC_URL"),
                      help="Ethereum JSON-RPC URL. Defaults to $ETH_RPC_URL.")
    mine.add_argument("--contract", default=DEFAULT_CONTRACT,
                      help=f"HASH contract address (default: {DEFAULT_CONTRACT})")
    mine.add_argument("--chain-id", type=int, default=MAINNET_CHAIN_ID)
    mine.add_argument("--private-key", default=None,
                      help="Private key for the miner address. Required to submit "
                           "solutions on-chain. Read from $MINER_PRIVATE_KEY by "
                           "default. Pass --no-submit to omit.")
    mine.add_argument("--no-submit", action="store_true",
                      help="Find solutions but do not broadcast them.")
    mine.add_argument("--dry-run", action="store_true",
                      help="Build but do not send the submit transaction.")
    mine.add_argument("--platform", type=int, default=None,
                      help="OpenCL platform index (see `devices`)")
    mine.add_argument("--device", type=int, default=None,
                      help="OpenCL device index within the platform")
    mine.add_argument("--local-size", type=int, default=256)
    mine.add_argument("--global-size", type=int, default=1 << 22,
                      help="Work-items per kernel launch. Tune to your GPU.")
    mine.add_argument("--refresh-seconds", type=float, default=30.0,
                      help="How often to re-pull challenge/difficulty.")
    mine.add_argument("--status-seconds", type=float, default=2.0,
                      help="How often to print live mining status.")
    mine.add_argument("--rpc-min-interval", type=float, default=1.0,
                      help="Minimum seconds between JSON-RPC requests. Increase "
                           "this for public endpoints that return HTTP/RPC 429.")
    mine.add_argument("--priority-fee-gwei", type=float, default=1.0)
    mine.add_argument("--max-fee-gwei", type=float, default=None)
    mine.add_argument("--gas-limit", type=int, default=200_000)
    mine.add_argument("--abi-override", action="append", default=[],
                      metavar="KEY=SIGNATURE",
                      help="Override a contract function signature. Example: "
                           "--abi-override difficulty=getDifficulty()  "
                           "Keys: challenge_for, difficulty, mining_state, total_mints.")
    mine.add_argument("--submit-signature", default=None,
                      help="Override the submit function signature, e.g. "
                           "mint(uint256). Default: mine(uint256)")

    # ---------- benchmark ----------
    bench = sub.add_parser("benchmark", help="Run a self-contained GPU hashrate benchmark")
    bench.add_argument("--platform", type=int, default=None)
    bench.add_argument("--device", type=int, default=None)
    bench.add_argument("--seconds", type=float, default=10.0)
    bench.add_argument("--local-size", type=int, default=256)
    bench.add_argument("--global-size", type=int, default=1 << 22)

    # ---------- devices ----------
    sub.add_parser("devices", help="List available OpenCL devices")

    # ---------- verify ----------
    ver = sub.add_parser("verify", help="CPU-verify a (challenge, nonce, target) triple")
    ver.add_argument("--challenge", required=True, help="32-byte challenge in hex")
    ver.add_argument("--nonce", required=True, help="uint256 nonce in decimal or 0x hex")
    ver.add_argument("--target", required=True, help="uint256 target in decimal or 0x hex")

    return p


# ---------------------------------------------------------------------------
# Sub-command implementations
# ---------------------------------------------------------------------------

def cmd_devices(_args) -> int:
    try:
        list_devices = _load_device_listing()
    except ModuleNotFoundError as e:
        return _missing_dependency_error(e, command="devices")

    rows = list_devices()
    if not rows:
        print("No OpenCL devices found. Install GPU drivers / OpenCL ICDs.")
        return 1
    print(f"{'P':>2}  {'D':>2}  {'Platform':<30}  Device")
    print("-" * 80)
    for p_idx, d_idx, platform, device in rows:
        print(f"{p_idx:>2}  {d_idx:>2}  {platform:<30}  {device}")
    return 0


def cmd_benchmark(args) -> int:
    import secrets
    import time

    try:
        GpuMiner, pick_device = _load_benchmark_dependencies()
    except ModuleNotFoundError as e:
        return _missing_dependency_error(e, command="benchmark")

    device = pick_device(args.platform, args.device)
    print(f"Benchmarking on: {device.name.strip()}")

    miner = GpuMiner(device, local_size=args.local_size, global_size=args.global_size)

    # Use a target of 1 << 252 → ~16-bit difficulty, finds many solutions but
    # we ignore them. The kernel still does the full keccak so the rate is
    # honest.
    challenge = secrets.token_bytes(32)
    target = (1 << 256) - 1   # impossible-to-fail: every nonce wins
    # Actually, "every nonce wins" makes us hit the atomic_cmpxchg path every
    # batch — fine, but means the kernel writes results every batch. For a
    # clean H/s number we instead want most batches to find nothing, so use
    # a tight target.
    target = 1   # essentially nothing will hit; we measure pure hashing

    deadline = time.time() + args.seconds
    found = 0
    for _ in miner.mine(challenge, target, max_seconds=args.seconds):
        found += 1
        if time.time() > deadline:
            break

    rate = miner.stats.hashrate()
    print(f"Total hashes : {miner.stats.hashes:,}")
    print(f"Elapsed      : {args.seconds:.1f}s")
    print(f"Hashrate     : {rate:,.0f} H/s  ({rate/1e6:,.2f} MH/s)")
    print(f"Solutions    : {found} (with target=1, expected ≈ 0)")
    return 0


def cmd_verify(args) -> int:
    try:
        from .protocol import verify_solution
    except ModuleNotFoundError as e:
        return _missing_dependency_error(e, command="verify")

    challenge = bytes.fromhex(args.challenge.removeprefix("0x"))
    nonce = int(args.nonce, 0)
    target = int(args.target, 0)
    ok = verify_solution(challenge, nonce, target)
    print("VALID" if ok else "INVALID")
    return 0 if ok else 1


def cmd_mine(args) -> int:
    if not args.rpc:
        print("error: --rpc is required (or set $ETH_RPC_URL)", file=sys.stderr)
        return 2

    abi_overrides = {}
    for kv in args.abi_override:
        if "=" not in kv:
            print(f"error: bad --abi-override {kv!r}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        abi_overrides[k.strip()] = v.strip()

    account = None
    private_key_source = None
    if args.no_submit:
        private_key = None
    elif args.private_key:
        private_key = args.private_key
        private_key_source = "--private-key"
    else:
        private_key = os.environ.get("MINER_PRIVATE_KEY")
        private_key_source = "$MINER_PRIVATE_KEY" if private_key else None

    if not private_key and not args.no_submit:
        print(
            "error: no private key supplied and --no-submit not set. "
            "Pass --private-key, set $MINER_PRIVATE_KEY, or use --no-submit "
            "to mine without broadcasting.",
            file=sys.stderr,
        )
        return 2

    try:
        Hash256RpcClient, _load_account_from_private_key = _load_rpc_dependencies()
    except ModuleNotFoundError as e:
        return _missing_dependency_error(e, command="mine")

    if private_key:
        try:
            account = _load_account_from_private_key(private_key)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if account.address.lower() != args.address.lower():
            print(
                f"warning: private key derives {account.address}, but --address is {args.address}. "
                "The mint will land at the key's address, not the one you passed.",
                file=sys.stderr,
            )

    try:
        rpc = Hash256RpcClient(
            rpc_url=args.rpc,
            miner_address=args.address,
            contract=args.contract,
            chain_id=args.chain_id,
            abi_overrides=abi_overrides,
            submit_signature=args.submit_signature or DEFAULT_SUBMIT,
            min_request_interval=args.rpc_min_interval,
        )
    except ValueError as e:
        print(f"error: bad address or RPC configuration: {e}", file=sys.stderr)
        return 2

    # Sanity check the connection.
    try:
        block = rpc.get_block_number()
        print(f"Connected to RPC, head block = {block}")
    except Exception as e:
        print(f"error: RPC unreachable: {e}", file=sys.stderr)
        return 1

    try:
        GpuMiner, MinerConfig, Orchestrator, pick_device = _load_mining_dependencies()
    except ModuleNotFoundError as e:
        return _missing_dependency_error(e, command="mine")

    device = pick_device(args.platform, args.device)
    print(f"Using device: {device.name.strip()}")
    gpu = GpuMiner(device, local_size=args.local_size, global_size=args.global_size)

    config = MinerConfig(
        refresh_seconds=args.refresh_seconds,
        print_status_seconds=args.status_seconds,
        submit=not args.no_submit,
        dry_run=args.dry_run,
        priority_fee_gwei=args.priority_fee_gwei,
        max_fee_gwei=args.max_fee_gwei,
        gas_limit=args.gas_limit,
        credential_diagnostics=_build_credential_diagnostics(
            account=account,
            private_key_source=private_key_source,
            private_key=private_key,
            miner_address=args.address,
            submit=not args.no_submit,
        ),
    )

    Orchestrator(rpc, gpu, account, config).run()
    return 0


def _build_credential_diagnostics(
    *,
    account,
    private_key_source: Optional[str],
    private_key: Optional[str],
    miner_address: str,
    submit: bool,
) -> str:
    if not submit:
        return "submit=disabled, private_key=not-used"
    if account is None:
        return "private_key=missing"

    match = "yes" if account.address.lower() == miner_address.lower() else "no"
    return (
        f"private_key_source={private_key_source or 'unknown'}, "
        f"derived_address={account.address}, "
        f"miner_address={miner_address}, "
        f"address_match={match}, "
        f"key_fingerprint={_private_key_fingerprint(private_key)}"
    )


def _private_key_fingerprint(private_key: Optional[str]) -> str:
    if not private_key:
        return "unavailable"

    raw = private_key.strip()
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    try:
        digest = hashlib.sha256(bytes.fromhex(raw)).hexdigest()
    except ValueError:
        return "unavailable"
    return f"sha256:{digest[:12]}"


def _load_device_listing():
    from .gpu import list_devices

    return list_devices


def _load_benchmark_dependencies():
    from .gpu import GpuMiner, pick_device

    return GpuMiner, pick_device


def _load_rpc_dependencies():
    from .rpc import Hash256RpcClient, load_account_from_private_key

    return Hash256RpcClient, load_account_from_private_key


def _load_mining_dependencies():
    from .gpu import GpuMiner, pick_device
    from .orchestrator import MinerConfig, Orchestrator

    return GpuMiner, MinerConfig, Orchestrator, pick_device


def _missing_dependency_error(exc: ModuleNotFoundError, *, command: str) -> int:
    missing = exc.name or str(exc)
    print(
        f"error: missing Python dependency {missing!r} while loading `{command}`.",
        file=sys.stderr,
    )
    print(
        "Install the project dependencies first, for example:\n"
        "  py -m pip install -e .\n"
        "or, for tests:\n"
        '  py -m pip install -e ".[test]"',
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    dispatch = {
        "mine":      cmd_mine,
        "benchmark": cmd_benchmark,
        "devices":   cmd_devices,
        "verify":    cmd_verify,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
