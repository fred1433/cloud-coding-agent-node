"""Checks the Kubernetes manifests in a kind cluster (run in CI; needs kind, kubectl, Docker).

What it establishes, each with a positive control where one makes sense:
- the sandbox pod runs as its non-root UID with a read-only root and no service-account token;
- the NetworkPolicy is enforced by this cluster: a pod WITHOUT the policy reaches the network,
  the sandbox pod with it does not;
- a pod naming the gvisor RuntimeClass never falls back to runc: rejected while the class does
  not exist, stuck without starting when the class exists but the node has no runsc handler.
Writes bench/k8s-kind.json.
"""
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
NS = "codenode-sandboxes"


def sh(*a, check=True, input=None):
    p = subprocess.run(list(a), capture_output=True, text=True, input=input)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(a)}: {p.stderr[-800:]}")
    return p


def kubectl(*a, **kw):
    return sh("kubectl", *a, **kw)


def wait_ready(name, ns, timeout=180):
    return kubectl("wait", "--for=condition=Ready", f"pod/{name}", "-n", ns, f"--timeout={timeout}s", check=False)


def pod_yaml(name, runtime_class=True):
    y = (HERE / "sandbox-pod.yaml").read_text().replace("codenode-run-example", name)
    if not runtime_class:
        y = y.replace("  runtimeClassName: gvisor\n", "")
    return y


PROBE = ("import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), 5); print('CONNECTED')\n"
         "except Exception as e:\n    print('blocked:', type(e).__name__, e)\n")


def main():
    out = []

    def rec(check, held, observed):
        out.append({"id": check, "outcome": "held" if held else "FAILED", "observed": observed})
        print(("held   " if held else "FAILED ") + check + ": " + observed)

    kubectl("apply", "-f", str(HERE / "namespace.yaml"))
    kubectl("apply", "-f", str(HERE / "networkpolicy.yaml"))

    # positive control: same image, other namespace, no policy
    kubectl("create", "namespace", "control", check=False)
    kubectl("run", "control", "-n", "control", "--image=codenode-sandbox:dev", "--image-pull-policy=IfNotPresent",
            "--restart=Never", "--", "python3", "/opt/codenode/init.py", "600")
    wait_ready("control", "control")
    ctl = kubectl("exec", "-n", "control", "control", "--", "python3", "-c", PROBE, check=False).stdout.strip()

    kubectl("apply", "-f", "-", input=pod_yaml("sbx-runc", runtime_class=False))
    ready = wait_ready("sbx-runc", NS)
    rec("pod_starts", ready.returncode == 0, "sandbox pod (no RuntimeClass) reached Ready under PSS restricted"
        if ready.returncode == 0 else ready.stderr[-300:])
    ex = lambda *c: kubectl("exec", "-n", NS, "sbx-runc", "--", *c, check=False)  # noqa: E731
    uid = ex("id", "-u").stdout.strip()
    ro = ex("sh", "-c", "touch /usr/x 2>&1; echo x > /workspace/ok && echo ws-ok").stdout
    tok = ex("sh", "-c", "ls /var/run/secrets/kubernetes.io/serviceaccount 2>&1").stdout
    rec("non_root_readonly_no_token", uid == "46298" and "Read-only" in ro and "ws-ok" in ro and "No such file" in tok,
        f"uid {uid}; root write: {ro.splitlines()[0] if ro else '?'}; workspace writable; service-account token: "
        f"{'absent' if 'No such file' in tok else 'PRESENT'}")
    sbx = ex("python3", "-c", PROBE).stdout.strip()
    rec("networkpolicy_enforced", "CONNECTED" in ctl and "blocked" in sbx,
        f"control pod without policy: {ctl}; sandbox pod with policy: {sbx}")

    # RuntimeClass must not fall back to runc
    missing = kubectl("apply", "-f", "-", input=pod_yaml("sbx-gvisor-noclass"), check=False)
    rec("runtimeclass_missing_rejected", missing.returncode != 0,
        (missing.stderr.strip().splitlines() or ["accepted"])[-1][:200])
    kubectl("apply", "-f", str(HERE / "runtimeclass-gvisor.yaml"))
    kubectl("apply", "-f", "-", input=pod_yaml("sbx-gvisor"))
    time.sleep(30)
    phase = kubectl("get", "pod", "sbx-gvisor", "-n", NS, "-o", "jsonpath={.status.phase}").stdout
    events = kubectl("get", "events", "-n", NS, "--field-selector", "involvedObject.name=sbx-gvisor",
                     "-o", "jsonpath={range .items[*]}{.reason}: {.message}{'\\n'}{end}").stdout
    reason = next((l for l in events.splitlines() if "runsc" in l or "handler" in l), events.splitlines()[-1:] or [""])
    reason = reason if isinstance(reason, str) else (reason[0] if reason else "")
    rec("runtimeclass_no_fallback", phase != "Running",
        f"phase after 30 s: {phase}; event: {reason[:220]}")

    doc = {"label": "Kubernetes manifests checked in a kind cluster (CI). gVisor itself is not installed in this "
                    "cluster; the RuntimeClass check only shows there is no silent fallback to runc.",
           "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "kubectl": kubectl("version", "-o", "json", check=False).stdout[:2000], "results": out}
    Path("bench").mkdir(exist_ok=True)
    Path("bench/k8s-kind.json").write_text(json.dumps(doc, indent=2) + "\n")
    return 0 if all(r["outcome"] == "held" for r in out) else 1


if __name__ == "__main__":
    sys.exit(main())
