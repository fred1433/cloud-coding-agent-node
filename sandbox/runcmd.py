"""Run one shell command INSIDE the sandbox with a deadline and an output cap.

Shell state persists between calls the way the bash tool contract expects: the
working directory, exported variables and shell functions are saved when a command
ends and restored before the next one ({"restart": true} clears them).

The command gets its own process group. When it finishes or times out, every
process of this user other than PID 1 and this helper is killed, so a
`sleep 999 &` or a `setsid` escape does not outlive the call. Background
processes therefore never survive a call; that is a deliberate limit.
"""
import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time

os.umask(0o022)
req = json.loads(sys.stdin.read())
cmd = req.get("command", "")
timeout = float(req.get("timeout", 60))
cap = int(req.get("max_output", 64_000))
STATE = "/home/agent/.codenode-shell"
WRAPPER = r'''
__cn_save() { __cn_rc=$?; mkdir -p "$CN_STATE"; pwd > "$CN_STATE/cwd"; export -p > "$CN_STATE/env"; declare -f > "$CN_STATE/funcs"; return $__cn_rc; }
trap __cn_save EXIT
if [ -f "$CN_STATE/cwd" ]; then cd "$(cat "$CN_STATE/cwd")" 2>/dev/null || cd /workspace; fi
[ -f "$CN_STATE/env" ] && . "$CN_STATE/env" 2>/dev/null
[ -f "$CN_STATE/funcs" ] && . "$CN_STATE/funcs" 2>/dev/null
__cn_cmd=$CN_CMD
unset CN_CMD
eval "$__cn_cmd"
'''
if req.get("restart"):
    subprocess.run(["rm", "-rf", STATE])
    print(json.dumps({"exit_code": 0, "timed_out": False, "output_b64": base64.b64encode(b"shell state cleared").decode(),
                      "dropped_bytes": 0, "killed_leftover_processes": 0,
                      "seconds": 0}))
    sys.exit(0)

buf = bytearray()
dropped = 0


def reader(stream):
    global dropped
    while True:
        chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
        if not chunk:
            break
        room = cap - len(buf)
        if room > 0:
            buf.extend(chunk[:room])
        dropped += max(0, len(chunk) - max(room, 0))


def kill_everything_else():
    me, ppid = os.getpid(), os.getppid()
    uid = os.getuid()
    killed = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in (1, me, ppid):
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError, FileNotFoundError):
            pass
    # wait until PID 1 has reaped them, so the next call does not hit the pids limit
    end = time.monotonic() + 3
    while killed and time.monotonic() < end:
        killed_alive = [p for p in killed if os.path.exists(f"/proc/{p}")]
        if not killed_alive:
            break
        time.sleep(0.05)
    return len(killed)


start = time.monotonic()
proc = subprocess.Popen(
    ["bash", "-c", WRAPPER], cwd="/workspace", stdin=subprocess.DEVNULL,
    env={**os.environ, "CN_CMD": cmd, "CN_STATE": STATE},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
)
t = threading.Thread(target=reader, args=(proc.stdout,), daemon=True)
t.start()
timed_out = False
try:
    code = proc.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    timed_out = True
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    code = proc.wait()
leftovers = kill_everything_else()
t.join(timeout=2)
print(json.dumps({
    "exit_code": code,
    "timed_out": timed_out,
    "output_b64": base64.b64encode(bytes(buf)).decode(),
    "dropped_bytes": dropped,
    "killed_leftover_processes": leftovers,
    "seconds": round(time.monotonic() - start, 3),
}))
