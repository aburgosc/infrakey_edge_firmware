try:
    import ujson as json
except Exception:
    import json
import os


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


class JsonlEventOutbox:
    def __init__(self, outbox_path, state_path, debug=1):
        self.outbox_path = outbox_path
        self.state_path = state_path
        self.debug = debug
        self._state = self._load_state()
        self._ensure_file(self.outbox_path)

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
        default = {"offset": 0}
        try:
            if self.state_path in os.listdir():
                with open(self.state_path, "r") as f:
                    obj = json.loads(f.read() or "{}")
                if isinstance(obj, dict):
                    default.update(obj)
        except Exception:
            pass
        return default

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(json.dumps(self._state))
            _safe_remove(self.state_path)
            os.rename(tmp, self.state_path)
            return True
        except Exception:
            _safe_remove(tmp)
            return False

    def enqueue(self, record):
        try:
            with open(self.outbox_path, "a") as f:
                f.write(json.dumps(record) + "\n")
            return True
        except Exception:
            return False

    def flush(self, sender, max_n=5):
        sent = 0
        offset = int(self._state.get("offset", 0) or 0)
        try:
            with open(self.outbox_path, "rb") as f:
                f.seek(offset)
                while sent < max_n:
                    pos = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break
                    next_offset = f.tell()
                    raw = line.strip()
                    if not raw:
                        self._state["offset"] = next_offset
                        sent += 1
                        continue
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except Exception:
                        self._state["offset"] = next_offset
                        sent += 1
                        continue
                    if not sender(record):
                        f.seek(pos)
                        break
                    self._state["offset"] = next_offset
                    sent += 1
        except Exception as exc:
            self._log("[outbox] flush error:", exc)
        self._save_state()
        return sent

    def compact_if_needed(self, min_bytes=2048, min_ratio_pct=50):
        offset = int(self._state.get("offset", 0) or 0)
        size = _safe_size(self.outbox_path)
        if offset <= 0 or size < int(min_bytes):
            return False
        ratio = int((offset * 100) // size) if size else 0
        if ratio < int(min_ratio_pct):
            return False
        tmp = self.outbox_path + ".tmp"
        try:
            with open(self.outbox_path, "rb") as src:
                src.seek(offset)
                remaining = src.read()
            with open(tmp, "wb") as dst:
                dst.write(remaining or b"")
            _safe_remove(self.outbox_path)
            os.rename(tmp, self.outbox_path)
            self._state["offset"] = 0
            self._save_state()
            return True
        except Exception:
            _safe_remove(tmp)
            return False

