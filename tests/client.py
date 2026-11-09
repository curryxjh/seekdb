"""Client for the explicitly LOCAL minivectordb-local-v1 adapter."""
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
        ready = self.receive()
        assert ready == {"ready": True, "protocol": "minivectordb-local-v1"}, ready

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
        payload = memoryview(sql.encode("utf-8") + b"\n")
        while payload:
            n = self.process.stdin.write(payload)
            if n is None or n <= 0:
                raise EOFError("database input closed")
            payload = payload[n:]

    def sql(self, sql):
        self.send(sql)
        return self.receive()

    def ok(self, sql):
        result = self.sql(sql)
        assert result.get("ok") is True, (sql, result)
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=self.timeout)
        stderr = self.process.stderr.read().decode("utf-8", "replace")
        assert self.process.returncode == 0, (self.process.returncode, stderr)
        assert not stderr, stderr
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
