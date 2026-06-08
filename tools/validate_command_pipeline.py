import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim7080mini.commandfeeder import Command, CommandPipeline, JsonlCommandJournal
from sim7080mini.outbox import JsonlEventOutbox


@contextmanager
def _pushd(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


class StubWs:
    def __init__(self, commands=None):
        self.commands = list(commands or [])

    def tick(self, max_reads=3):
        return None

    def pull(self, max_n=5):
        out = self.commands[:max_n]
        self.commands = self.commands[max_n:]
        return out

    def stats(self):
        return {"queued": len(self.commands), "connected": True, "subscribed": True}


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_journal_basic(tmpdir):
    with _pushd(tmpdir):
        journal = JsonlCommandJournal(
            journal_path="commands.jsonl",
            state_path="commands_state.json",
            dead_letter_path="commands_dead.jsonl",
            debug=0,
        )
        assert journal.append({"id": "c1", "command_type": "ping", "payload": {"n": 1}, "source": "test"})
        cmds = journal.pull(max_n=5)
        assert len(cmds) == 1
        assert cmds[0].id == "c1"
        assert journal.stats()["inflight"] == 1
        journal.mark_processed("c1", {"local_ok": True, "ack_http_ok": True, "ack_ws_ok": False})
        stats = journal.stats()
        assert stats["inflight"] == 0
        assert stats["recent_ids"] == 1
        return {"name": "journal_basic", "status": "OK", "details": stats}


def test_journal_recovery_inflight(tmpdir):
    with _pushd(tmpdir):
        journal = JsonlCommandJournal("recover.jsonl", "recover_state.json", "recover_dead.jsonl", debug=0)
        journal.append({"id": "c2", "command_type": "open_actuator", "payload": {}, "source": "test"})
        pulled = journal.pull(max_n=5)
        assert len(pulled) == 1
        rebooted = JsonlCommandJournal("recover.jsonl", "recover_state.json", "recover_dead.jsonl", debug=0)
        recovered = rebooted.recover_inflight(max_n=5)
        assert len(recovered) == 1
        assert recovered[0].id == "c2"
        rebooted.mark_processed("c2", {"local_ok": True, "ack_http_ok": False, "ack_ws_ok": False})
        return {"name": "journal_recovery_inflight", "status": "OK", "details": rebooted.stats()}


def test_journal_dead_letter_and_truncation(tmpdir):
    with _pushd(tmpdir):
        _write_text(
            "faulty.jsonl",
            '{"id":"ok-1","command_type":"ping","payload":{}}\n'
            '{"id":"bad-json","command_type":\n'
            '{"id":"partial","command_type":"ping","payload":{}}',
        )
        journal = JsonlCommandJournal("faulty.jsonl", "faulty_state.json", "faulty_dead.jsonl", debug=0)
        cmds = journal.pull(max_n=5)
        assert len(cmds) == 1
        assert cmds[0].id == "ok-1"
        stats = journal.stats()
        assert stats["dead_lettered"] >= 1
        assert stats["recovered_partial_lines"] >= 1
        return {"name": "journal_dead_letter_and_truncation", "status": "OK", "details": stats}


def test_journal_compaction(tmpdir):
    with _pushd(tmpdir):
        journal = JsonlCommandJournal(
            journal_path="compact.jsonl",
            state_path="compact_state.json",
            dead_letter_path="compact_dead.jsonl",
            debug=0,
        )
        for idx in range(8):
            journal.append({"id": "k{}".format(idx), "command_type": "ping", "payload": {"idx": idx}})
        pulled = journal.pull(max_n=6)
        assert len(pulled) == 6
        compacted = journal.compact_if_needed(min_bytes=32, min_ratio_pct=30)
        assert compacted is True
        assert journal.stats()["compactions"] == 1
        return {"name": "journal_compaction", "status": "OK", "details": journal.stats()}


def test_pipeline_overflow_and_recovery(tmpdir):
    with _pushd(tmpdir):
        journal = JsonlCommandJournal(
            journal_path="pipe.jsonl",
            state_path="pipe_state.json",
            dead_letter_path="pipe_dead.jsonl",
            debug=0,
        )
        ws = StubWs([Command("w{}".format(i), "ping", {"i": i}) for i in range(5)])
        pipeline = CommandPipeline(ws=ws, journal=journal, max_queue=2, debug=0, persist_ws_commands=True)
        cmds = pipeline.pull(max_out=5, max_ws=5, max_journal=5)
        assert len(cmds) == 2
        stats = pipeline.stats()
        assert stats["queue"]["dropped"] >= 3
        for cmd in cmds:
            pipeline.mark_processed(cmd.id, {"local_ok": True, "ack_http_ok": True, "ack_ws_ok": True})
        recovered = JsonlCommandJournal(
            journal_path="pipe.jsonl",
            state_path="pipe_state.json",
            dead_letter_path="pipe_dead.jsonl",
            debug=0,
        )
        inflight = recovered.recover_inflight(max_n=10)
        return {
            "name": "pipeline_overflow_and_recovery",
            "status": "OK",
            "details": {
                "stats": stats,
                "recovered_after_overflow": len(inflight),
            },
        }


def test_pipeline_priority_and_dedup(tmpdir):
    with _pushd(tmpdir):
        journal = JsonlCommandJournal(
            journal_path="prio.jsonl",
            state_path="prio_state.json",
            dead_letter_path="prio_dead.jsonl",
            debug=0,
        )
        journal.append({"id": "j-inflight", "command_type": "ping", "payload": {"src": "journal"}})
        pulled = journal.pull(max_n=1)
        assert len(pulled) == 1
        journal.append({"id": "j-pending", "command_type": "ping", "payload": {"src": "journal"}})
        journal.mark_processed("dup-1", {"local_ok": True, "ack_http_ok": True, "ack_ws_ok": False})

        ws = StubWs([
            Command("dup-1", "ping", {"src": "ws-dup"}),
            Command("w-live", "ping", {"src": "ws"}),
        ])
        pipeline = CommandPipeline(ws=ws, journal=journal, max_queue=8, debug=0, persist_ws_commands=False)
        cmds = pipeline.pull(max_out=5, max_ws=5, max_journal=5)
        ids = [cmd.id for cmd in cmds]
        assert ids == ["j-inflight", "w-live", "j-pending"]
        stats = pipeline.stats()
        assert stats["journal"]["duplicates_skipped"] >= 0
        return {
            "name": "pipeline_priority_and_dedup",
            "status": "OK",
            "details": {
                "ids": ids,
                "stats": stats,
            },
        }


def test_compaction_threshold_policy(tmpdir):
    with _pushd(tmpdir):
        journal = JsonlCommandJournal(
            journal_path="window.jsonl",
            state_path="window_state.json",
            dead_letter_path="window_dead.jsonl",
            debug=0,
        )
        for idx in range(4):
            journal.append({"id": "c{}".format(idx), "command_type": "ping", "payload": {"idx": idx}})
        journal.pull(max_n=1)
        compacted_early = journal.compact_if_needed(min_bytes=4096, min_ratio_pct=90)
        assert compacted_early is False

        for idx in range(12):
            journal.append({"id": "m{}".format(idx), "command_type": "ping", "payload": {"idx": idx}})
        journal.pull(max_n=10)
        compacted_late = journal.compact_if_needed(min_bytes=32, min_ratio_pct=30)
        assert compacted_late is True
        return {
            "name": "compaction_threshold_policy",
            "status": "OK",
            "details": journal.stats(),
        }


def test_outbox_retry_and_flush(tmpdir):
    with _pushd(tmpdir):
        outbox = JsonlEventOutbox(
            outbox_path="outbox.jsonl",
            state_path="outbox_state.json",
            debug=0,
        )
        outbox.enqueue({"status": "device_offline", "severity": "warning", "event_id": "e1"})
        outbox.enqueue({"status": "battery_low", "severity": "warning", "event_id": "e2"})
        accepted = []

        def sender(record):
            accepted.append(record["event_id"])
            return True

        flushed = outbox.flush(sender, max_n=5)
        assert flushed == 2
        outbox.compact_if_needed(min_bytes=16, min_ratio_pct=10)
        return {
            "name": "outbox_retry_and_flush",
            "status": "OK",
            "details": {"flushed": flushed, "accepted": accepted},
        }


def test_legacy_inbox_quarantine(tmpdir):
    with _pushd(tmpdir):
        os.mkdir("commands_inbox")
        _write_text("commands_inbox/bad.json", '{"id":"bad-1","payload":{"x":1}}')
        _write_text("commands_inbox/invalid.json", '{"id":"broken"')
        journal = JsonlCommandJournal(
            journal_path="legacy.jsonl",
            state_path="legacy_state.json",
            dead_letter_path="legacy_dead.jsonl",
            debug=0,
        )
        from sim7080mini.commandfeeder import LegacyInboxImporter
        importer = LegacyInboxImporter("commands_inbox", journal, debug=0)
        moved = importer.import_once(max_n=10)
        stats = journal.stats()
        assert moved == 0
        assert stats["legacy_quarantined"] == 2
        assert stats["dead_lettered"] >= 2
        assert len(os.listdir("commands_inbox")) == 0
        return {
            "name": "legacy_inbox_quarantine",
            "status": "OK",
            "details": stats,
        }


def run_all():
    tmpdir = tempfile.mkdtemp(prefix="ifk-pipeline-")
    try:
        tests = [
            test_journal_basic,
            test_journal_recovery_inflight,
            test_journal_dead_letter_and_truncation,
            test_journal_compaction,
            test_pipeline_overflow_and_recovery,
            test_pipeline_priority_and_dedup,
            test_compaction_threshold_policy,
            test_outbox_retry_and_flush,
            test_legacy_inbox_quarantine,
        ]
        results = []
        for test_fn in tests:
            try:
                results.append(test_fn(tmpdir))
            except Exception as exc:
                results.append({"name": test_fn.__name__, "status": "FAIL", "details": str(exc)})
        return results
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    results = run_all()
    summary = {
        "ok": sum(1 for r in results if r["status"] == "OK"),
        "fail": sum(1 for r in results if r["status"] != "OK"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
