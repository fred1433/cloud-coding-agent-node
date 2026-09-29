"""What a workflow engine calls: start, follow, cancel, fetch the result.

- start() is idempotent on run_id: calling it again (a retry, a browser reconnect)
  returns the existing run instead of executing it twice.
- events() takes the last seq the caller saw and returns only what came after, so a
  reconnecting UI resumes without duplicates or gaps.
- every call checks the caller's TenantContext: tenant A cannot read, follow, cancel
  or restart tenant B's run, and cannot even learn that it exists.
"""
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
            r = self.runs.get(run_id)
        if r is None or r["tenant"] != ctx.tenant:
            raise RunNotFound(run_id)       # same answer whether it is missing or someone else's
        return r

    def start(self, ctx, run_id, node_config, task, files=None, outputs=("*",)):
        with self.lock:
            existing = self.runs.get(run_id)
            if existing is not None:
                if existing["tenant"] != ctx.tenant:
                    raise RunNotFound(run_id)
                return {"run_id": run_id, "started": False}
            rec = Recorder()
            cancel = threading.Event()
            entry = {"tenant": ctx.tenant, "recorder": rec, "cancel": cancel, "result": None,
                     "done": threading.Event()}
            self.runs[run_id] = entry

        def work():
            kw = {}
            if self.model_factory:
                kw["model"] = self.model_factory()
            if self.sandbox_cls:
                kw["sandbox_cls"] = self.sandbox_cls
            try:
                entry["result"] = run_node(ctx, node_config, task, files, outputs=outputs, run_id=run_id,
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
