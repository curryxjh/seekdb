"""Test client using the official JSONL transport."""
import json
import os
import select
import subprocess


class Client:
    def __init__(self, command, directory, env=None, timeout=30):
        self.command, self.directory, self.timeout = command, directory, timeout
        self.env = dict(os.environ)
        self.env.pop("MVDB_FAULT", None)
        if env:
            self.env.update(env)
        self.process = subprocess.Popen(
            [*command, "--data", str(directory)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, bufsize=0)
        self.pending = bytearray()
        self.sequence = 0
        self.mode = "exact"
        ready = self.receive()
        assert ready == {"type": "ready"}, ready

    def receive(self):
        # Keep surplus bytes explicitly, so readiness cannot hide a buffered line.
        while True:
            newline = self.pending.find(b"\n")
            if newline >= 0:
                result = self.pending[:newline]
                del self.pending[:newline + 1]
                return json.loads(result)
            readable, _, _ = select.select([self.process.stdout], [], [], self.timeout)
            if not readable:
                raise TimeoutError(f"response timeout, pid={self.process.pid}")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise EOFError(f"database exited: {self.process.poll()}")
            self.pending += chunk

    def send(self, sql):
        self.sequence += 1
        self.send_request({"id": self.sequence, "op": "execute", "sql": sql,
                           "search_mode": self.mode})

    def send_request(self, request):
        payload = memoryview(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
        while payload:
            n = self.process.stdin.write(payload)
            if n is None or n <= 0:
                raise EOFError("database input closed")
            payload = payload[n:]

    def sql(self, sql):
        self.send(sql)
        result = self.receive()
        assert type(result["id"]) is int and result["id"] == self.sequence, result
        return result

    def ok(self, sql):
        result = self.sql(sql)
        assert result.get("ok") is True, (sql, result)
        return result

    def close(self):
        if self.process.poll() is None:
            self.sequence += 1
            self.send_request({"id": self.sequence, "op": "shutdown"})
            assert self.receive() == {"id": self.sequence, "ok": True}
            self.process.stdin.close()
            self.process.wait(timeout=self.timeout)
        stderr = self.process.stderr.read().decode("utf-8", "replace")
        assert self.process.returncode == 0, (self.process.returncode, stderr)
        assert not stderr, stderr
        assert not self.pending and not self.process.stdout.read(), "extra stdout after shutdown"
        self.process.stdout.close()
        self.process.stderr.close()

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=self.timeout)
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()


def sql_string(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
