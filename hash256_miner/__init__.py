"""hash256-miner: CLI miner for hash256.org (HASH token)

Usage:
    hash256-miner --address 0xYourMiner... --rpc https://eth.llamarpc.com
    hash256-miner benchmark
    hash256-miner devices

See `hash256-miner --help`.
"""

__version__ = "0.1.0"


def main(argv=None):
    """Lazy entry point — imports CLI machinery on first use."""
    from .__main__ import main as _main
    return _main(argv)


__all__ = ["main", "__version__"]
