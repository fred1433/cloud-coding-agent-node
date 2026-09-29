"""One hardened container per run. The host talks to it only through `docker exec`
of two small helpers baked into the image (sandbox/fsops.py, sandbox/runcmd.py).

Nothing secret is ever passed to the container: no -e from the host environment,
no mounted host directory, no Docker socket. The workspace is a size-capped tmpfs
owned by a UID derived from the tenant, so two tenants never share a UID or a disk.
"""
import base64
import fnmatch
import hashlib
import json
import os
import subprocess
import time
import uuid

from .text import clean

IMAGE = "codenode-sandbox:dev"
DOCKER = "docker"


def tenant_uid(tenant):
    """A stable, non-root UID per tenant (20000-59999). A real deployment would
    allocate these from a registry instead of hashing, to rule out collisions."""
    h = int(hashlib.sha256(tenant.encode()).hexdigest(), 16)
    return 20000 + h % 40000


def _docker(*args, input=None, timeout=60, check=True):
    p = subprocess.run([DOCKER, *args], input=input, capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        raise SandboxError(f"docker {args[0]} failed: {clean(p.stderr, 800)}")
    return p


class SandboxError(RuntimeError):
    pass


class Sandbox:
    def __init__(self, tenant, limits, network=None, runtime="runc", image=IMAGE, run_id=None):
        self.tenant = tenant
        self.uid = tenant_uid(tenant)
        self.limits = limits
        self.network = network or {"mode": "none"}
        self.runtime = runtime
        self.image = image
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.name = f"cn-{self.run_id}"
        self.proxy_name = f"cn-{self.run_id}-egress"
        self.net_name = f"cn-{self.run_id}-net"
        self.started = False
        grace = int(os.environ.get("CODENODE_GRACE_S", "60"))
        self.hard_deadline_s = int((limits or {}).get("deadline_s", 900)) + grace
        self.expires = int(time.time()) + self.hard_deadline_s

    # ---------------------------------------------------------------- lifecycle
    def docker_run_args(self):
        L, u = self.limits, self.uid
        args = [
            "run", "-d", "--rm", "--name", self.name,
            "--label", "codenode=1", "--label", f"codenode.run={self.run_id}",
            "--label", f"codenode.tenant={self.tenant}", "--label", f"codenode.expires={self.expires}",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--ipc", "private",
            "--pids-limit", str(L["pids"]),
            "--memory", f"{L['memory_mb']}m", "--memory-swap", f"{L['memory_mb']}m",
            "--cpus", str(L["cpus"]),
            "--user", f"{u}:{u}",
            "--tmpfs", f"/workspace:rw,nosuid,nodev,size={L['workspace_mb']}m,uid={u},gid={u},mode=0700",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--tmpfs", f"/home/agent:rw,noexec,nosuid,nodev,size=32m,uid={u},gid={u},mode=0700",
        ]
        if self.runtime != "runc":
            args += ["--runtime", self.runtime]
        if self.network["mode"] == "none":
            args += ["--network", "none"]
        else:
            proxy = getattr(self, "proxy_url", "http://egress:3128")
            args += ["--network", self.net_name,
                     "-e", f"HTTPS_PROXY={proxy}", "-e", f"HTTP_PROXY={proxy}",
                     "-e", f"https_proxy={proxy}", "-e", f"http_proxy={proxy}"]
        # PID 1 exits at the hard deadline; with --rm Docker then removes the container
        # even if the controller that started it has died.
        return args + [self.image, "python3", "/opt/codenode/init.py", str(self.hard_deadline_s)]

    def start(self):
        if self.network["mode"] == "egress_proxy":
            self._start_egress()
        _docker(*self.docker_run_args())
        self.started = True
        return self

    def _start_egress(self):
        allow = ",".join(self.network.get("allow", []))
        _docker("network", "create", "--internal", "--label", "codenode=1",
                "--label", f"codenode.run={self.run_id}", "--label", f"codenode.expires={self.expires}",
                self.net_name)
        _docker("run", "-d", "--rm", "--name", self.proxy_name,
                "--label", "codenode=1", "--label", f"codenode.run={self.run_id}",
                "--label", f"codenode.expires={self.expires}",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "64", "--memory", "128m", "--user", "65534:65534",
                "--network", self.net_name, "--network-alias", "egress",
                self.image, "timeout", str(self.hard_deadline_s), "python3", "/opt/codenode/egress_proxy.py", allow)
        ip = _docker("inspect", "-f", '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % self.net_name,
                     self.proxy_name).stdout.decode().strip()
        # by address, not by name: gVisor's netstack does not use Docker's embedded DNS
        self.proxy_url = f"http://{ip}:3128"
        _docker("network", "connect", "bridge", self.proxy_name)
        # wait until the proxy logs that it listens
        for _ in range(50):
            if "ready" in self.egress_log_raw():
                return
            subprocess.run(["sleep", "0.1"])
        raise SandboxError("egress proxy did not start")

    def egress_log_raw(self):
        p = _docker("logs", self.proxy_name, check=False)
        return p.stdout.decode(errors="replace")

    def egress_log(self):
        out = []
        for line in self.egress_log_raw().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("decision") != "ready":
                out.append(rec)
        return out

    def destroy(self):
        """Remove everything this run created and report anything left behind."""
        _docker("rm", "-f", "-v", self.name, self.proxy_name, check=False)
        _docker("network", "rm", self.net_name, check=False)
        return self.leftovers()

    def leftovers(self):
        c = _docker("ps", "-aq", "--filter", f"label=codenode.run={self.run_id}", check=False)
        n = _docker("network", "ls", "-q", "--filter", f"label=codenode.run={self.run_id}", check=False)
        return {"containers": c.stdout.split(), "networks": n.stdout.split()}

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.destroy()

    # ---------------------------------------------------------------- execution
    def _helper(self, script, payload, timeout):
        p = _docker("exec", "-i", "-u", f"{self.uid}:{self.uid}", "-w", "/workspace", self.name,
                    "python3", f"/opt/codenode/{script}",
                    input=json.dumps(payload).encode(), timeout=timeout, check=False)
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {"ok": False, "error": f"sandbox helper failed (exit {p.returncode}): "
                                          f"{clean(p.stderr or p.stdout, 500)}"}

    def fs(self, op, **kw):
        return self._helper("fsops.py", {"op": op, **kw}, timeout=60)

    def bash(self, command, timeout=None, max_output=64_000):
        t = timeout or self.limits["command_timeout_s"]
        try:
            res = self._helper("runcmd.py", {"command": command, "timeout": t, "max_output": max_output},
                               timeout=t + 20)
        except subprocess.TimeoutExpired:
            return {"exit_code": None, "timed_out": True, "output": "", "note": "host-side timeout"}
        if "output_b64" not in res:
            return {"exit_code": None, "timed_out": False, "output": res.get("error", "sandbox error")}
        res["output"] = clean(base64.b64decode(res.pop("output_b64")))
        return res

    def export(self, patterns, max_files=50, max_total_bytes=10_000_000, max_file_bytes=2_000_000):
        """Copy out only the files the node declares as outputs. Symlinks, special files
        and hostile names are never exported; oversized sets are cut and reported."""
        snap = self.fs("snapshot")
        if not snap.get("ok"):
            raise SandboxError(f"snapshot failed: {snap.get('error')}")
        files, refused = {}, list(snap.get("skipped", []))
        total = 0
        for path, meta in sorted(snap["files"].items()):
            if not any(fnmatch.fnmatch(path, p) for p in patterns):
                refused.append({"path": path, "reason": "not a declared output"})
                continue
            if meta["size"] > max_file_bytes:
                refused.append({"path": path, "reason": f"{meta['size']} bytes, over the per-file limit"})
                continue
            if len(files) >= max_files or total + meta["size"] > max_total_bytes:
                refused.append({"path": path, "reason": "output set limit reached"})
                continue
            data = self.get(path, limit=max_file_bytes)
            if hashlib.sha256(data).hexdigest() != meta["sha"]:
                refused.append({"path": path, "reason": "changed during export"})
                continue
            files[path] = data
            total += len(data)
        return files, refused

    def restart_shell(self):
        return self._helper("runcmd.py", {"restart": True}, timeout=30)

    def put(self, path, data):
        if isinstance(data, str):
            data = data.encode()
        res = self.fs("put", path=path, b64=base64.b64encode(data).decode())
        if not res.get("ok"):
            raise SandboxError(f"could not upload {path}: {res.get('error')}")

    def get(self, path, limit=5_000_000):
        res = self.fs("get", path=path, limit=limit)
        if not res.get("ok"):
            raise SandboxError(f"could not read {path}: {res.get('error')}")
        return base64.b64decode(res["b64"])


def janitor(now=None):
    """Remove codenode containers and networks whose hard deadline has passed.
    Meant to run on a schedule next to the workflow engine; it is what cleans up
    after a controller that was killed before it could call destroy()."""
    now = now or time.time()
    removed = []
    for kind, ls in (("container", ["ps", "-a"]), ("network", ["network", "ls"])):
        p = _docker(*ls, "--filter", "label=codenode=1", "--format",
                    '{{.ID}} {{.Label "codenode.expires"}}' if kind == "container" else "{{.ID}}", check=False)
        for line in p.stdout.decode().split("\n"):
            if not line.strip():
                continue
            ident = line.split()[0]
            if kind == "network":
                q = _docker("network", "inspect", ident, "--format", '{{index .Labels "codenode.expires"}}', check=False)
                exp = q.stdout.decode().strip()
            else:
                exp = line.split()[1] if len(line.split()) > 1 else ""
            if exp.isdigit() and int(exp) < now:
                if kind == "container":
                    _docker("rm", "-f", "-v", ident, check=False)
                else:
                    _docker("network", "rm", ident, check=False)
                removed.append((kind, ident))
    return removed
