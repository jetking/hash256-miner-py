import pytest

from hash256_miner import __main__ as cli


class DummyAccount:
    address = "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8"


def test_help_does_not_load_runtime_dependencies(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    assert "hash256-miner" in capsys.readouterr().out


def test_mine_rejects_bad_private_key_before_rpc(monkeypatch, capsys):
    monkeypatch.delenv("MINER_PRIVATE_KEY", raising=False)

    def fail_rpc(*_args, **_kwargs):
        raise AssertionError("RPC should not be constructed for a malformed key")

    def bad_key(_private_key):
        raise ValueError("private key must be a 32-byte hex string")

    monkeypatch.setattr(cli, "_load_rpc_dependencies", lambda: (fail_rpc, bad_key))

    rc = cli.main([
        "mine",
        "--address", "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8",
        "--rpc", "http://127.0.0.1:1",
        "--private-key", "0x...",
    ])

    assert rc == 2
    assert "private key must be a 32-byte hex string" in capsys.readouterr().err


def test_mine_no_submit_ignores_private_key_env(monkeypatch):
    class DummyRpc:
        def get_block_number(self):
            return 1

    class DummyDevice:
        name = "test device"

    class DummyConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    seen = {}

    class DummyOrchestrator:
        def __init__(self, _rpc, _gpu, account, config, reporter=None):
            seen["account"] = account
            seen["submit"] = config.submit
            seen["status_seconds"] = config.print_status_seconds
            seen["tui"] = config.tui
            seen["reporter"] = reporter

        def run(self):
            seen["ran"] = True

    def make_rpc(**kwargs):
        seen["rpc_min_interval"] = kwargs["min_request_interval"]
        return DummyRpc()

    monkeypatch.setenv("MINER_PRIVATE_KEY", "0x...")
    monkeypatch.setattr(cli, "_load_rpc_dependencies", lambda: (make_rpc, object()))
    monkeypatch.setattr(
        cli,
        "_load_mining_dependencies",
        lambda: (
            lambda _device, **kwargs: seen.setdefault("gpu_kwargs", kwargs) or object(),
            DummyConfig,
            DummyOrchestrator,
            lambda _platform, _device: DummyDevice(),
            RuntimeError,
        ),
    )

    rc = cli.main([
        "mine",
        "--address", "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8",
        "--rpc", "http://127.0.0.1:1",
        "--no-submit",
        "--rpc-min-interval", "2.5",
        "--status-seconds", "1.25",
        "--batch-cooldown-seconds", "0.1",
    ])

    assert rc == 0
    assert seen == {
        "rpc_min_interval": 2.5,
        "account": None,
        "submit": False,
        "status_seconds": 1.25,
        "tui": False,
        "reporter": None,
        "gpu_kwargs": {
            "local_size": 256,
            "global_size": 1 << 22,
            "batch_cooldown_seconds": 0.1,
        },
        "ran": True,
    }


def test_mine_reports_rpc_constructor_value_error(monkeypatch, capsys):
    def fail_rpc(**_kwargs):
        raise ValueError("bad checksum")

    monkeypatch.delenv("MINER_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_rpc_dependencies", lambda: (fail_rpc, object()))

    rc = cli.main([
        "mine",
        "--address", "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8",
        "--rpc", "http://127.0.0.1:1",
        "--no-submit",
    ])

    assert rc == 2
    assert "error: bad address or RPC configuration: bad checksum" in capsys.readouterr().err


def test_mine_reports_opencl_device_reset_without_traceback(monkeypatch, capsys):
    class DeviceResetError(RuntimeError):
        pass

    class DummyRpc:
        def get_block_number(self):
            return 1

    class DummyDevice:
        name = "test device"

    class DummyOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            raise DeviceResetError("GPU driver reset")

    monkeypatch.delenv("MINER_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_rpc_dependencies", lambda: (lambda **_kwargs: DummyRpc(), object()))
    monkeypatch.setattr(
        cli,
        "_load_mining_dependencies",
        lambda: (
            lambda _device, **_kwargs: object(),
            lambda **kwargs: type("DummyConfig", (), kwargs)(),
            DummyOrchestrator,
            lambda _platform, _device: DummyDevice(),
            DeviceResetError,
        ),
    )

    rc = cli.main([
        "mine",
        "--address", "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8",
        "--rpc", "http://127.0.0.1:1",
        "--no-submit",
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "error: GPU driver reset" in captured.err
    assert "Traceback" not in captured.err


def test_credential_diagnostics_redacts_private_key():
    private_key = "1" * 64
    account = DummyAccount()

    diagnostics = cli._build_credential_diagnostics(
        account=account,
        private_key_source="$MINER_PRIVATE_KEY",
        private_key=private_key,
        miner_address=account.address,
        submit=True,
    )

    assert private_key not in diagnostics
    assert "private_key_source=$MINER_PRIVATE_KEY" in diagnostics
    assert f"derived_address={account.address}" in diagnostics
    assert "address_match=yes" in diagnostics
    assert "key_fingerprint=sha256:" in diagnostics
