import pytest

from hash256_miner.rpc import (
    DEFAULT_SUBMIT,
    DEFAULT_VIEWS,
    Hash256RpcClient,
    is_rate_limited_error,
    load_account_from_private_key,
)


def test_load_account_accepts_hex_private_key():
    account = load_account_from_private_key("1" * 64)

    assert account.address == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"


@pytest.mark.parametrize(
    "private_key",
    [
        "",
        "0x",
        "0x...",
        "z" * 64,
        "1" * 63,
        "1" * 65,
    ],
)
def test_load_account_rejects_malformed_private_key(private_key):
    with pytest.raises(ValueError, match="private key must be a 32-byte hex string"):
        load_account_from_private_key(private_key)


@pytest.mark.parametrize(
    "message",
    [
        "{'code': 429, 'message': 'rate-limited until later'}",
        "HTTP 429 Too Many Requests",
        "rate limit exceeded",
    ],
)
def test_is_rate_limited_error_detects_provider_throttling(message):
    assert is_rate_limited_error(RuntimeError(message))


def test_is_rate_limited_error_ignores_unrelated_errors():
    assert not is_rate_limited_error(RuntimeError("execution reverted"))


def test_call_error_includes_function_signature():
    class DummyEth:
        def call(self, _tx):
            raise RuntimeError("execution reverted")

    class DummyWeb3:
        eth = DummyEth()

    client = Hash256RpcClient.__new__(Hash256RpcClient)
    client.w3 = DummyWeb3()
    client.contract = "0x0000000000000000000000000000000000000000"
    client.min_request_interval = 0.0
    client._last_rpc_request_at = 0.0

    with pytest.raises(RuntimeError) as exc_info:
        client._call("currentEpoch()")

    message = str(exc_info.value)
    assert "eth_call currentEpoch() selector=0x" in message
    assert "execution reverted" in message


def test_default_hash_contract_signatures_match_deployed_abi():
    assert DEFAULT_VIEWS["challenge_for"] == "getChallenge(address)"
    assert DEFAULT_VIEWS["mining_state"] == "miningState()"
    assert DEFAULT_SUBMIT == "mine(uint256)"


def test_get_mining_state_decodes_tuple_words():
    client = Hash256RpcClient.__new__(Hash256RpcClient)
    client._sigs = dict(DEFAULT_VIEWS)

    def fake_call(sig, args=b""):
        assert sig == "miningState()"
        assert args == b""
        return b"".join(i.to_bytes(32, "big") for i in range(10, 17))

    client._call = fake_call

    state = client.get_mining_state()

    assert state.era == 10
    assert state.reward == 11
    assert state.difficulty == 12
    assert state.minted == 13
    assert state.remaining == 14
    assert state.epoch == 15
    assert state.epoch_blocks_left == 16


def test_fetch_job_uses_mining_state_and_get_challenge():
    miner = "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8"
    challenge = bytes.fromhex("11" * 32)
    client = Hash256RpcClient.__new__(Hash256RpcClient)
    client._sigs = dict(DEFAULT_VIEWS)
    client.miner_address = miner
    client.contract = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
    client.chain_id = 1
    calls = []

    def fake_call(sig, args=b""):
        calls.append((sig, args))
        if sig == "miningState()":
            return b"".join(i.to_bytes(32, "big") for i in [0, 100, 12345, 0, 1, 250, 42])
        if sig == "getChallenge(address)":
            assert args == client.miner_address_as_word()
            return challenge
        raise AssertionError(f"unexpected call {sig}")

    client._call = fake_call

    job = client.fetch_job()

    assert job.challenge == challenge
    assert job.target == 12345
    assert job.epoch == 250
    assert [sig for sig, _args in calls] == ["miningState()", "getChallenge(address)"]


def test_fetch_job_can_include_miner_balance():
    miner = "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8"
    challenge = bytes.fromhex("22" * 32)
    client = Hash256RpcClient.__new__(Hash256RpcClient)
    client._sigs = dict(DEFAULT_VIEWS)
    client.miner_address = miner
    client.contract = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
    client.chain_id = 1
    calls = []

    def fake_call(sig, args=b""):
        calls.append((sig, args))
        if sig == "miningState()":
            return b"".join(i.to_bytes(32, "big") for i in [1, 100, 12345, 500, 900, 250, 42])
        if sig == "getChallenge(address)":
            return challenge
        if sig == "balanceOf(address)":
            assert args == client.miner_address_as_word()
            return (123).to_bytes(32, "big")
        raise AssertionError(f"unexpected call {sig}")

    client._call = fake_call

    job = client.fetch_job(include_balance=True)

    assert job.state is not None
    assert job.state.reward == 100
    assert job.miner_balance == 123
    assert [sig for sig, _args in calls] == [
        "miningState()",
        "getChallenge(address)",
        "balanceOf(address)",
    ]


def test_fetch_job_can_include_total_mints():
    miner = "0x04cec3e6CDfeF6CcEc8c098d70FF4f6E5C00e8e8"
    challenge = bytes.fromhex("33" * 32)
    client = Hash256RpcClient.__new__(Hash256RpcClient)
    client._sigs = dict(DEFAULT_VIEWS)
    client.miner_address = miner
    client.contract = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
    client.chain_id = 1
    calls = []

    def fake_call(sig, args=b""):
        calls.append((sig, args))
        if sig == "miningState()":
            return b"".join(i.to_bytes(32, "big") for i in [1, 100, 12345, 500, 900, 250, 42])
        if sig == "getChallenge(address)":
            return challenge
        if sig == "totalMints()":
            return (100_100).to_bytes(32, "big")
        raise AssertionError(f"unexpected call {sig}")

    client._call = fake_call

    job = client.fetch_job(include_total_mints=True)

    assert job.total_mints == 100_100
    assert [sig for sig, _args in calls] == [
        "miningState()",
        "getChallenge(address)",
        "totalMints()",
    ]
