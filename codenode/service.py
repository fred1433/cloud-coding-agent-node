"""What a workflow engine calls: start, follow, cancel, fetch the result.

- start() is idempotent on run_id: calling it again (a retry, a browser reconnect)
  returns the existing run instead of executing it twice.
- events() takes the last seq the caller saw and returns only what came after, so a
  reconnecting UI resumes without duplicates or gaps.
- runs are keyed by (tenant, run_id): each tenant has its own run-id namespace, so
  tenant B cannot read, follow, cancel or restart tenant A's run, cannot squat an id A
  will use, and gets exactly the same answers whether or not A has a run with that id.
"""
import hashlib
import threading

from .node import Recorder, run_node


class RunNotFound(KeyError):
    pass


class NodeService:
    def __init__(self, model_factory=None, sandbox_cls=None):
        self.runs = {}
        self.lock = threading.Lock()
        self.model_factory = model_factory
        self.sandbox_cls = sandbox_cls

    def _get(self, ctx, run_id):
        with self.lock:
            r = self.runs.get((ctx.tenant, run_id))
        if r is None:
            raise RunNotFound(run_id)
        return r

    def start(self, ctx, run_id, node_config, task, files=None, outputs=("*",)):
        with self.lock:
            existing = self.runs.get((ctx.tenant, run_id))
            if existing is not None:
                return {"run_id": run_id, "started": False}
            rec = Recorder()
            cancel = threading.Event()
            entry = {"tenant": ctx.tenant, "recorder": rec, "cancel": cancel, "result": None,
                     "done": threading.Event()}
            self.runs[(ctx.tenant, run_id)] = entry

        # the sandbox name must be unique across tenants too
        sandbox_id = hashlib.sha256(f"{ctx.tenant}\0{run_id}".encode()).hexdigest()[:20]

        def work():
            kw = {}
            if self.model_factory:
                kw["model"] = self.model_factory()
            if self.sandbox_cls:
                kw["sandbox_cls"] = self.sandbox_cls
            try:
                entry["result"] = run_node(ctx, node_config, task, files, outputs=outputs, run_id=sandbox_id,
                                           cancel=cancel, recorder=rec, **kw)
            finally:
                entry["done"].set()

        threading.Thread(target=work, daemon=True).start()
        return {"run_id": run_id, "started": True}

    def events(self, ctx, run_id, after_seq=-1):
        return self._get(ctx, run_id)["recorder"].since(after_seq)

    def cancel(self, ctx, run_id):
        self._get(ctx, run_id)["cancel"].set()

    def wait(self, ctx, run_id, timeout=None):
        r = self._get(ctx, run_id)
        r["done"].wait(timeout)
        return r["result"]
