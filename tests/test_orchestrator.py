from hash256_miner.orchestrator import MinerConfig, PersistentEventReporter


class DummyDevice:
    name = "test gpu"


class DummyGpu:
    device = DummyDevice()


class DummyReporter:
    def start(self, _gpu, _config):
        pass

    def stop(self):
        pass

    def signal(self, _signum):
        pass


def test_persistent_event_reporter_logs_task_start_and_signal_exit(tmp_path):
    log_file = tmp_path / "events.log"
    reporter = PersistentEventReporter(DummyReporter(), str(log_file))

    reporter.start(DummyGpu(), MinerConfig(submit=False))
    reporter.signal(2)
    reporter.stop()

    contents = log_file.read_text(encoding="utf-8")
    assert "event=task_start" in contents
    assert "event=signal_exit_requested signum=2" in contents
    assert "event=signal_exit signum=2" in contents
    assert "event=miner_stop" in contents
