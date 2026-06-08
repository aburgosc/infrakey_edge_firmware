try:
    import ujson as json
except Exception:
    import json
import os


def _safe_int(v, default):
    try:
        return int(v)
    except Exception:
        return default


def _safe_remove(path):
    try:
        os.remove(path)
    except Exception:
        pass


def _safe_size(path):
    try:
        return os.stat(path)[6]
    except Exception:
        return 0


class Command:
    def __init__(self, cmd_id: str, cmd_type: str, payload=None, source="unknown", meta=None):
        self.id = cmd_id
        self.type = cmd_type
        self.payload = payload or {}
        self.source = source
        self.meta = meta or {}


class InMemoryCommandQueue:
    def __init__(self, max_size=32, debug=1):
        self.max_size = max(1, _safe_int(max_size, 32))
        self.debug = debug
        self._queue = []
        self.dropped = 0

    def _log(self, *args):
        if self.debug >= 1:
            try:
                print(*args)
            except Exception:
                pass

    def push(self, cmd):
        if len(self._queue) >= self.max_size:
            self.dropped += 1
            self._log("[queue] overflow drop id=", getattr(cmd, "id", "?"))
            return False
        self._queue.append(cmd)
        return True

    def pop_many(self, max_n=5):
        out = self._queue[:max_n]
        self._queue = self._queue[max_n:]
        return out

    def __len__(self):
        return len(self._queue)

    def stats(self):
        return {
            "queued": len(self._queue),
            "max_size": self.max_size,
            "dropped": self.dropped,
        }


class JsonlCommandJournal:
    FORMAT_VERSION = 2

    def __init__(self, journal_path, state_path, dead_letter_path, debug=1, state_save_every=1, processed_id_cache_size=128):
        self.journal_path = journal_path
        self.state_path = state_path
        self.dead_letter_path = dead_letter_path
        self.debug = debug
        self.state_save_every = max(1, _safe_int(state_save_every, 1))
        self.processed_id_cache_size = max(16, _safe_int(processed_id_cache_size, 128))
        self._pull_since_save = 0
        self._stats = {
            "appended": 0,
            "pulled": 0,
            "dead_lettered": 0,
            "compactions": 0,
            "legacy_imported": 0,
            "legacy_quarantined": 0,
            "recovered_partial_lines": 0,
            "duplicates_skipped": 0,
            "inflight_recovered": 0,
        }
        self._state = self._load_state()
        self._ensure_file(self.journal_path)
        self._ensure_file(self.dead_letter_path)

    def _log(self, *args):
        if self.debug >= 1:
            try:
                print(*args)
            except Exception:
                pass

    def _ensure_file(self, path):
        try:
            if path not in os.listdir():
                with open(path, "a"):
                    pass
        except Exception:
            try:
                with open(path, "a"):
                    pass
            except Exception:
                pass

    def _load_state(self):
        default = {
            "format_version": self.FORMAT_VERSION,
            "offset": 0,
            "recent_ids": [],
            "inflight": [],
            "last_processed": None,
        }
        try:
            if self.state_path in os.listdir():
                with open(self.state_path, "r") as f:
                    obj = json.loads(f.read() or "{}")
                if isinstance(obj, dict):
                    default.update(obj)
        except Exception:
            pass
        if not isinstance(default.get("recent_ids"), list):
            default["recent_ids"] = []
        if not isinstance(default.get("inflight"), list):
            default["inflight"] = []
        return default

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(json.dumps(self._state))
            try:
                _safe_remove(self.state_path)
            except Exception:
                pass
            os.rename(tmp, self.state_path)
            return True
        except Exception:
            _safe_remove(tmp)
            return False

    def state_offset(self):
        return _safe_int(self._state.get("offset", 0), 0)

    def has_processed(self, cmd_id):
        try:
            return cmd_id in self._state.get("recent_ids", [])
        except Exception:
            return False

    def _is_inflight(self, cmd_id):
        try:
            return any(item.get("id") == cmd_id for item in self._state.get("inflight", []))
        except Exception:
            return False

    def _add_inflight(self, record):
        inflight = self._state.setdefault("inflight", [])
        cmd_id = record.get("id")
        for item in inflight:
            if item.get("id") == cmd_id:
                return
        inflight.append(record)
        self._mark_dirty()

    def _remove_inflight(self, cmd_id):
        inflight = self._state.setdefault("inflight", [])
        original_len = len(inflight)
        inflight[:] = [item for item in inflight if item.get("id") != cmd_id]
        if len(inflight) != original_len:
            self._mark_dirty()

    def mark_processed(self, cmd_id, result=None):
        if not cmd_id:
            return
        self._remove_inflight(cmd_id)
        recent = self._state.setdefault("recent_ids", [])
        if cmd_id in recent:
            return
        recent.append(cmd_id)
        if len(recent) > self.processed_id_cache_size:
            del recent[:len(recent) - self.processed_id_cache_size]
        if result is not None:
            self._state["last_processed"] = {
                "id": cmd_id,
                "local_ok": bool(result.get("local_ok", False)),
                "ack_http_ok": bool(result.get("ack_http_ok", False)),
                "ack_ws_ok": bool(result.get("ack_ws_ok", False)),
            }
        self._mark_dirty()

    def recover_inflight(self, max_n=5):
        out = []
        for item in self._state.get("inflight", [])[:max_n]:
            cmd_id = item.get("id")
            cmd_type = item.get("command_type") or item.get("type")
            if not cmd_id or not cmd_type or self.has_processed(cmd_id):
                continue
            out.append(Command(
                cmd_id,
                cmd_type,
                item.get("payload") or {},
                source=item.get("source") or "journal_recovery",
                meta={"recovered": True},
            ))
        if out:
            self._stats["inflight_recovered"] += len(out)
        return out

    def _mark_dirty(self):
        self._pull_since_save += 1
        if self._pull_since_save >= self.state_save_every:
            self._save_state()
            self._pull_since_save = 0

    def _flush_state_if_dirty(self):
        if self._pull_since_save:
            self._save_state()
            self._pull_since_save = 0

    def stats(self):
        return {
            "offset": self.state_offset(),
            "journal_size": _safe_size(self.journal_path),
            "dead_letter_size": _safe_size(self.dead_letter_path),
            "appended": self._stats["appended"],
            "pulled": self._stats["pulled"],
            "dead_lettered": self._stats["dead_lettered"],
            "compactions": self._stats["compactions"],
            "legacy_imported": self._stats["legacy_imported"],
            "legacy_quarantined": self._stats["legacy_quarantined"],
            "recovered_partial_lines": self._stats["recovered_partial_lines"],
            "duplicates_skipped": self._stats["duplicates_skipped"],
            "inflight_recovered": self._stats["inflight_recovered"],
            "recent_ids": len(self._state.get("recent_ids", [])),
            "inflight": len(self._state.get("inflight", [])),
        }

    def append(self, cmd_dict):
        record = {
            "format_version": self.FORMAT_VERSION,
            "id": cmd_dict.get("id"),
            "command_type": cmd_dict.get("command_type"),
            "payload": cmd_dict.get("payload") or {},
            "source": cmd_dict.get("source") or "debug",
            "created_at": cmd_dict.get("created_at"),
        }
        try:
            with open(self.journal_path, "a") as f:
                f.write(json.dumps(record) + "\n")
            self._stats["appended"] += 1
            return True
        except Exception:
            return False

    def _dead_letter(self, raw_line, reason):
        try:
            with open(self.dead_letter_path, "a") as f:
                rec = {
                    "reason": reason,
                    "raw": raw_line.decode("utf-8", "ignore") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line),
                }
                f.write(json.dumps(rec) + "\n")
            self._stats["dead_lettered"] += 1
        except Exception:
            pass

    def pull(self, max_n=5):
        out = []
        offset = self.state_offset()
        try:
            with open(self.journal_path, "rb") as f:
                f.seek(offset)
                while len(out) < max_n:
                    pos = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        # linea parcial por corte de energia: no avanzar
                        f.seek(pos)
                        self._stats["recovered_partial_lines"] += 1
                        break
                    next_offset = f.tell()
                    raw = line.strip()
                    if not raw:
                        self._state["offset"] = next_offset
                        self._mark_dirty()
                        continue
                    try:
                        obj = json.loads(raw.decode("utf-8"))
                    except Exception:
                        self._dead_letter(raw, "invalid_json")
                        self._state["offset"] = next_offset
                        self._mark_dirty()
                        continue

                    cmd_id = obj.get("id")
                    cmd_type = obj.get("command_type") or obj.get("type")
                    payload = obj.get("payload") or {}
                    if not cmd_id or not cmd_type:
                        self._dead_letter(raw, "missing_fields")
                        self._state["offset"] = next_offset
                        self._mark_dirty()
                        continue
                    if self.has_processed(cmd_id):
                        self._state["offset"] = next_offset
                        self._stats["duplicates_skipped"] += 1
                        self._mark_dirty()
                        continue
                    if self._is_inflight(cmd_id):
                        self._state["offset"] = next_offset
                        self._mark_dirty()
                        continue

                    record = {
                        "id": cmd_id,
                        "command_type": cmd_type,
                        "payload": payload,
                        "source": obj.get("source") or "journal",
                        "created_at": obj.get("created_at"),
                    }
                    self._add_inflight(record)

                    out.append(Command(
                        cmd_id,
                        cmd_type,
                        payload,
                        source=obj.get("source") or "journal",
                        meta={"persisted": True},
                    ))
                    self._state["offset"] = next_offset
                    self._stats["pulled"] += 1
                    self._mark_dirty()
        except Exception as exc:
            self._log("[journal] pull error:", exc)

        self._flush_state_if_dirty()
        return out

    def compact_if_needed(self, min_bytes=4096, min_ratio_pct=50):
        offset = self.state_offset()
        size = _safe_size(self.journal_path)
        if offset <= 0 or size < min_bytes:
            return False
        compact_ratio = int((offset * 100) // size) if size else 0
        if compact_ratio < int(min_ratio_pct):
            return False
        tmp = self.journal_path + ".tmp"
        try:
            with open(self.journal_path, "rb") as src:
                src.seek(offset)
                remaining = src.read()
            with open(tmp, "wb") as dst:
                dst.write(remaining or b"")
            _safe_remove(self.journal_path)
            os.rename(tmp, self.journal_path)
            self._state["offset"] = 0
            self._save_state()
            self._stats["compactions"] += 1
            return True
        except Exception:
            _safe_remove(tmp)
            return False


class LegacyInboxImporter:
    def __init__(self, inbox_dir, journal, debug=1):
        self.inbox = inbox_dir
        self.journal = journal
        self.debug = debug
        try:
            if self.inbox not in os.listdir():
                os.mkdir(self.inbox)
        except Exception:
            try:
                os.mkdir(self.inbox)
            except Exception:
                pass

    def import_once(self, max_n=5):
        try:
            files = sorted([f for f in os.listdir(self.inbox) if f.endswith(".json")])
        except Exception:
            return 0
        moved = 0
        for fname in files[:max_n]:
            fpath = self.inbox + "/" + fname
            try:
                with open(fpath, "r") as f:
                    obj = json.loads(f.read() or "{}")
                cmd_id = obj.get("id") or fname.rsplit(".", 1)[0]
                cmd_type = obj.get("command_type") or obj.get("type") or ""
                payload = obj.get("payload") or {}
                if cmd_type and self.journal.append({
                    "id": cmd_id,
                    "command_type": cmd_type,
                    "payload": payload,
                    "source": "legacy_inbox",
                }):
                    moved += 1
                    self.journal._stats["legacy_imported"] += 1
                    _safe_remove(fpath)
                else:
                    self.journal._dead_letter(obj, "legacy_missing_command_type")
                    self.journal._stats["legacy_quarantined"] += 1
                    _safe_remove(fpath)
            except Exception:
                self.journal._dead_letter(fname, "legacy_invalid_json")
                self.journal._stats["legacy_quarantined"] += 1
                _safe_remove(fpath)
        return moved


class CommandPipeline:
    def __init__(self, ws=None, journal=None, legacy_importer=None, max_queue=32, debug=1, persist_ws_commands=False):
        self.ws = ws
        self.journal = journal
        self.legacy_importer = legacy_importer
        self.debug = debug
        self.persist_ws_commands = bool(persist_ws_commands)
        self.queue = InMemoryCommandQueue(max_size=max_queue, debug=debug)
        self._recovery_loaded = False

    def _log(self, *args):
        if self.debug >= 1:
            try:
                print(*args)
            except Exception:
                pass

    def _feed_ws(self, max_ws):
        if not self.ws:
            return
        self.ws.tick(max_reads=3)
        try:
            cmds_ws = self.ws.pull(max_n=max_ws)
        except Exception:
            cmds_ws = []
        for cmd in cmds_ws:
            if self.journal and self.journal.has_processed(cmd.id):
                self._log("[queue] duplicate ws id=", cmd.id)
                continue
            if self.persist_ws_commands and self.journal:
                self.journal.append({
                    "id": cmd.id,
                    "command_type": cmd.type,
                    "payload": cmd.payload,
                    "source": "ws",
                })
            self.queue.push(cmd)

    def _feed_journal(self, max_n):
        if not self.journal:
            return
        for cmd in self.journal.pull(max_n=max_n):
            self.queue.push(cmd)

    def _feed_recovery(self, max_n):
        if self._recovery_loaded or not self.journal:
            return
        for cmd in self.journal.recover_inflight(max_n=max_n):
            self.queue.push(cmd)
        self._recovery_loaded = True

    def pull(self, max_out=10, max_ws=10, max_journal=10, import_legacy=0):
        if self.legacy_importer and import_legacy:
            self.legacy_importer.import_once(max_n=import_legacy)
        self._feed_recovery(max_n=max_journal)
        self._feed_ws(max_ws=max_ws)
        self._feed_journal(max_n=max_journal)
        return self.queue.pop_many(max_n=max_out)

    def stats(self):
        out = {
            "queue": self.queue.stats(),
        }
        if self.journal:
            out["journal"] = self.journal.stats()
        if self.ws and hasattr(self.ws, "stats"):
            out["ws"] = self.ws.stats()
        return out

    def mark_processed(self, cmd_id, result=None):
        if self.journal:
            self.journal.mark_processed(cmd_id, result=result)
