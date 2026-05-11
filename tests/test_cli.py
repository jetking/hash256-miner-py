from hash256_miner import __main__ as cli


def test_mine_rejects_bad_private_key_before_rpc(monkeypatch, capsys):
    monkeypatch.delenv("MINER_PRIVATE_KEY", raising=False)

    def fail_rpc(*_args, **_kwargs):
        raise AssertionError("RPC should not be constructed for a malformed key")

    monkeypatch.setattr(cli, "Hash256RpcClient", fail_rpc)

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

    seen = {}

    class DummyOrchestrator:
        def __init__(self, _rpc, _gpu, account, config):
            seen["account"] = account
            seen["submit"] = config.submit
            seen["status_seconds"] = config.print_status_seconds

        def run(self):
            seen["ran"] = True

    def make_rpc(**kwargs):
        seen["rpc_min_interval"] = kwargs["min_request_interval"]
        return DummyRpc()

    monkeypatch.setenv("MINER_PRIVATE_KEY", "0x...")
    monkeypatch.setattr(cli, "Hash256RpcClient", make_rpc)
    monkeypatch.setattr(cli, "pick_device", lambda _platform, _device: DummyDevice())
    monkeypatch.setattr(cli, "GpuMiner", lambda _device, **_kwargs: object())
    monkeypatch.setattr(cli, "Orchestrator", DummyOrchestrator)

    rc = cli.main([
        "mine",
        "--address", "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8",
        "--rpc", "http://127.0.0.1:1",
        "--no-submit",
        "--rpc-min-interval", "2.5",
        "--status-seconds", "1.25",
    ])

    assert rc == 0
    assert seen == {
        "rpc_min_interval": 2.5,
        "account": None,
        "submit": False,
        "status_seconds": 1.25,
        "ran": True,
    }


def test_mine_reports_rpc_constructor_value_error(monkeypatch, capsys):
    def fail_rpc(**_kwargs):
        raise ValueError("bad checksum")

    monkeypatch.delenv("MINER_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(cli, "Hash256RpcClient", fail_rpc)

    rc = cli.main([
        "mine",
        "--address", "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8",
        "--rpc", "http://127.0.0.1:1",
        "--no-submit",
    ])

    assert rc == 2
    assert "error: bad address or RPC configuration: bad checksum" in capsys.readouterr().err


def test_credential_diagnostics_redacts_private_key():
    private_key = "1" * 64
    account = cli.load_account_from_private_key(private_key)

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
