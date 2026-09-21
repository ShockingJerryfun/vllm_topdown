# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Exercise the real shell runner and HTTP client without a GPU or Linux PMU."""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE_VLLM = r"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen

def arg(name):
    return sys.argv[sys.argv.index(name) + 1]

if sys.argv[1] == "bench":
    request = Request(arg("--base-url") + "/v1/completions", b"{}")
    with urlopen(request) as response:
        response.read()
    print("Successful requests: 1")
    sys.exit(0)

import kperf_instrument as kperf
from scripts.parse_run import STAGES

# Only the kernel boundary is replaced; configuration and probes are real.
totals = {}
def open_event(event, *args):
    if event == 0xffff:
        raise OSError("unsupported test event")
    return os.open(os.devnull, os.O_RDONLY)

def read_group(fds, ids):
    totals[fds[0]] = totals.get(fds[0], 0) + 100
    return totals[fds[0]], totals[fds[0]], [17] * len(ids)

kperf.open_event = open_event
kperf.event_id = lambda fd: fd
kperf.fcntl.ioctl = lambda *args: None
kperf.read_group = read_group
paused = False

class Handler(BaseHTTPRequestHandler):
    def reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply({})

    def do_POST(self):
        global paused
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/pause?"):
            paused = True
            self.reply({"status": "paused"})
        elif self.path == "/resume":
            paused = False
            self.reply({"status": "resumed"})
        elif self.path == "/collective_rpc":
            assert paused
            assert payload["method"] == "configure_kperf"
            try:
                result = kperf.configure(**payload["kwargs"])
            except RuntimeError as error:
                self.reply({"error": str(error)}, 500)
            else:
                self.reply({"results": [result]})
        elif self.path == "/v1/completions":
            assert not paused
            print("TEST_REQUEST", flush=True)
            for token in range(3):
                span = "execute_model_to_sample_tokens"
                kperf.kperf_span_begin(span)
                for stage in STAGES:
                    if token == 0 and stage == "run_fullgraph":
                        continue
                    kperf.kperf_begin(stage)
                    sum(range(100))
                    kperf.kperf_finish(stage)
                kperf.kperf_span_finish(span)
            self.reply({})
        else:
            self.reply({}, 404)

print("TEST_SERVICE_START", flush=True)
HTTPServer(("127.0.0.1", int(arg("--port"))), Handler).serve_forever()
"""


@pytest.mark.parametrize("fail_switch", [False, True])
def test_session_reuses_service_and_cleans_up(
    tmp_path: Path,
    fail_switch: bool,
) -> None:
    for command in ("bash", "curl", "seq"):
        if shutil.which(command) is None:
            pytest.skip(f"Shell integration requires {command}")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "run_one.sh",
        "session.sh",
        "warmup.sh",
        "cooling.sh",
        "switch_pmu.py",
        "placement.sh",
        "placement.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    vllm = tmp_path / "fake_vllm"
    vllm.write_text(f"#!{sys.executable}\n" + FAKE_VLLM)
    vllm.chmod(0o755)
    setsid = tmp_path / "setsid"
    setsid.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "os.setsid()\nos.execvp(sys.argv[1], sys.argv[1:])\n"
    )
    setsid.chmod(0o755)
    perf = tmp_path / "perf"
    perf.write_text(
        f"#!{sys.executable}\n"
        "import json, signal, sys, time\nfrom pathlib import Path\n"
        "if sys.argv[1] == 'record':\n"
        "    output = Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "    output.write_bytes(b'test')\n"
        "    output.with_suffix('.args.json').write_text(json.dumps(sys.argv))\n"
        "    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
        "    while True: time.sleep(0.1)\n"
        "else: print('17.00% test_symbol')\n"
    )
    perf.chmod(0o755)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = (ROOT / "scripts/config.env").read_text()
    config += "\nPERSISTENT_BENCH_CLIENT=0\n"
    config += (
        f"\nVLLM_BIN={shlex.quote(str(vllm))}\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\nPORT={port}\n"
        "SERVICE_SETTLE_SECONDS=0\nREADY_CHECK_ATTEMPTS=15\n"
        "PERF_SETTLE_SECONDS=1\n"
        "READY_CHECK_INTERVAL=1\nSHUTDOWN_ATTEMPTS=2\nSHUTDOWN_INTERVAL=0.1\n"
    )
    (scripts / "config.env").write_text(config)
    runtime = tmp_path / "overlay"
    runtime.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    command = f"""
set -Eeuo pipefail
COMMON_DIR={shlex.quote(str(scripts))}
RUNTIME={shlex.quote(str(runtime))}
export RUN_ROOT={shlex.quote(str(results))}
export VLLM_PYTHONPATH={shlex.quote(str(ROOT))}
source "$COMMON_DIR/session.sh"
start_session
bash "$COMMON_DIR/run_one.sh" time
bash "$COMMON_DIR/run_one.sh" topdown 0x11 cycles
mkdir "$RUN_ROOT/end_to_end"
KPERF_TARGET=execute_model_to_sample_tokens KPERF_QUALIFIER=run_fullgraph \
    RUN_ROOT="$RUN_ROOT/end_to_end" bash "$COMMON_DIR/run_one.sh" time
bash "$COMMON_DIR/run_one.sh" hotspot
"""
    if fail_switch:
        command += 'bash "$COMMON_DIR/run_one.sh" bad 0xffff unsupported\n'
    command += "stop_session\n"
    result = subprocess.run(
        ["bash", "-c", command],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert (result.returncode != 0) == fail_switch, result.stderr + result.stdout
    service_log = (results / "service/server.log").read_text()
    assert service_log.count("TEST_SERVICE_START") == 1
    assert service_log.count("TEST_REQUEST") == 4
    assert not runtime.exists()
    pid = int((results / "service/ready").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    identities = []
    for name, mode, profile in (
        ("time", "time", "pipeline"),
        ("topdown", "pmu", "pipeline"),
        ("end_to_end/time", "time", "end_to_end"),
    ):
        run_dir = results / name
        ack = json.loads((run_dir / "switch.json").read_text())
        identities.append((ack["pid"], ack["tid"]))
        assert (run_dir / "server.log").read_text().count("TEST_REQUEST") == 1
        parse = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/parse_run.py"),
                str(run_dir),
                "--mode",
                mode,
                "--profile",
                profile,
                "--expected-calls",
                "2",
                "--event-names",
                "cycles" if mode == "pmu" else "",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert parse.returncode == 0, parse.stderr + parse.stdout
    assert len(set(identities)) == 1
    perf_args = json.loads((results / "hotspot/perf.args.json").read_text())
    assert perf_args[perf_args.index("-t") + 1] == str(identities[0][1])
    assert "KPERF_TIME," not in (results / "hotspot/measurement.log").read_text()
    assert "KPERF," not in (results / "hotspot/measurement.log").read_text()


@pytest.mark.parametrize("scope,expected", [("session", 1), ("per_group", 2)])
def test_warmup_policy_reuses_service_state(tmp_path, scope, expected):
    """Later PMU groups omit warmup only after this session completed it."""
    script = ROOT / "scripts/warmup.sh"
    command = r"""
set -eu
SESSION_DIR=$1
RUN_DIR=$1
WARMUP_SCOPE=$2
ROUND_WARMUPS=1
run_benchmark() { printf 'request\n' >> "$RUN_DIR/requests"; BENCH_RC=0; }
source "$3"
run_round_warmups
run_round_warmups
"""
    subprocess.run(
        ["bash", "-c", command, "test", str(tmp_path), scope, str(script)],
        check=True,
    )
    assert len((tmp_path / "requests").read_text().splitlines()) == expected


def test_failed_warmup_does_not_mark_session_ready(tmp_path):
    command = r"""
set -eu
SESSION_DIR=$1
RUN_DIR=$1
WARMUP_SCOPE=session
ROUND_WARMUPS=1
run_benchmark() { BENCH_RC=1; }
source "$2"
run_round_warmups
"""
    result = subprocess.run(
        ["bash", "-c", command, "test", str(tmp_path), str(ROOT / "scripts/warmup.sh")]
    )
    assert result.returncode == 5
    assert not (tmp_path / "warmup.completed").exists()


@pytest.mark.parametrize("temperature,expected", [("72", 0), ("N/A", 6)])
def test_cooling_finishes_before_warmup(
    tmp_path: Path, temperature: str, expected: int
) -> None:
    command = r"""
set -eu
RUN_DIR=$1
GROUP_START_TEMPERATURE=60
GPU_ID=0
nvidia-smi() { cat "$RUN_DIR/temperature"; }
sleep() {
    printf 'cooling\n' >> "$RUN_DIR/order"
    printf '59\n' > "$RUN_DIR/temperature"
}
run_benchmark() { printf 'warmup\n' >> "$RUN_DIR/order"; BENCH_RC=0; }
ROUND_WARMUPS=1
source "$2/cooling.sh"
source "$2/warmup.sh"
cool_before_group
run_round_warmups
"""
    (tmp_path / "temperature").write_text(temperature)
    result = subprocess.run(
        ["bash", "-c", command, "test", str(tmp_path), str(ROOT / "scripts")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert (tmp_path / "order").read_text().splitlines() == ["cooling", "warmup"]
        readings = (tmp_path / "cooling.tsv").read_text().splitlines()
        assert readings[-1].endswith("59\t60")
    else:
        assert not (tmp_path / "order").exists()
