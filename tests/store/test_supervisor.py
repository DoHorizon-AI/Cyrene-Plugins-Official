"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_supervisor.py                                              │
│  Module: tests.store.test_supervisor                                │
│  Role: Unit tests for Cyrene Store ProcessSupervisor.              │
│        Verifies process launch, health checks (PID/TCP/UDS/handshake)│
│        graceful zero-downtime hot-swap, automatic rollback, and     │
│        fault isolation on unexpected crashes.                       │
│                                                                     │
│  模块职责：进程守护器单元测试                                           │
│  · 测试进程拉起与各种健康检查（PID / TCP / UDS / 握手）                 │
│  · 测试从 V1 平滑热切到 V2（零停机与在途排空）                           │
│  · 测试 V2 启动失败时的自动回滚与 V1 保持健康                            │
│  · 测试 Worker 异常崩溃（SIGSEGV / OOM 退出）时的故障物理隔离与状态捕获   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

from tools.store.supervisor import (
    CrashReport,
    HealthCheckConfig,
    HealthCheckKind,
    HotSwapError,
    ProcessSupervisor,
    WorkerProcess,
    WorkerSpec,
    WorkerStartupError,
    WorkerState,
)


def _find_free_port() -> int:
    """Helper to allocate a currently free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Test Suite 1: Launch & Health Check Probing ──────────────────────
# 测试组 1：进程拉起与多策略健康检查


def test_worker_launch_and_pid_health_check() -> None:
    """Test process launch and basic PID-alive health checking."""
    supervisor = ProcessSupervisor(name="test_pid_sup")
    try:
        spec = WorkerSpec(
            worker_id="worker-pid-1",
            name="pid_mock_plugin",
            version="1.0.0",
            cmd=[sys.executable, "-c", "import time; time.sleep(15)"],
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.PID,
                interval_sec=0.05,
                initial_delay_sec=0.01,
            ),
        )

        worker = supervisor.launch_initial_worker(spec, readiness_timeout_sec=2.0)

        assert worker.is_alive
        assert worker.is_healthy
        assert worker.state == WorkerState.HEALTHY
        assert worker.pid is not None
        assert supervisor.active_pid == worker.pid
        assert supervisor.active_worker is worker
    finally:
        supervisor.stop()


def test_worker_tcp_health_check() -> None:
    """Test process launch and TCP listening port health probing."""
    port = _find_free_port()
    tcp_server_code = f"""
import socket, sys, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', {port}))
s.listen(5)
while True:
    try:
        conn, _ = s.accept()
    except Exception:
        break
    try:
        conn.close()
    except Exception:
        pass

"""

    supervisor = ProcessSupervisor(name="test_tcp_sup")
    try:
        spec = WorkerSpec(
            worker_id="worker-tcp-1",
            name="tcp_mock_plugin",
            version="1.0.0",
            cmd=[sys.executable, "-c", tcp_server_code],
            endpoint=f"tcp://127.0.0.1:{port}",
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.TCP,
                host="127.0.0.1",
                port=port,
                interval_sec=0.05,
                initial_delay_sec=0.01,
                max_retries=20,
            ),
        )

        worker = supervisor.launch_initial_worker(spec, readiness_timeout_sec=3.0)

        assert worker.is_alive
        assert worker.state == WorkerState.HEALTHY
        assert supervisor.active_endpoint == f"tcp://127.0.0.1:{port}"
    finally:
        supervisor.stop()


def test_worker_uds_health_check(tmp_path: Path) -> None:
    """Test process launch and Unix Domain Socket (UDS) health probing."""
    sock_path = str(tmp_path / "test_uds.sock")
    uds_server_code = f"""
import socket, os, sys, time
path = {sock_path!r}
if os.path.exists(path):
    os.unlink(path)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(path)
s.listen(5)
while True:
    try:
        conn, _ = s.accept()
        conn.close()
    except Exception:
        break
"""

    supervisor = ProcessSupervisor(name="test_uds_sup")
    try:
        spec = WorkerSpec(
            worker_id="worker-uds-1",
            name="uds_mock_plugin",
            version="1.0.0",
            cmd=[sys.executable, "-c", uds_server_code],
            endpoint=f"unix://{sock_path}",
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.UDS,
                socket_path=sock_path,
                interval_sec=0.05,
                initial_delay_sec=0.02,
                max_retries=20,
            ),
        )

        worker = supervisor.launch_initial_worker(spec, readiness_timeout_sec=3.0)

        assert worker.is_alive
        assert worker.state == WorkerState.HEALTHY
        assert supervisor.active_endpoint == f"unix://{sock_path}"
    finally:
        supervisor.stop()


def test_worker_handshake_health_check() -> None:
    """Test request-response handshake verification over socket."""
    port = _find_free_port()
    handshake_server_code = f"""
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', {port}))
s.listen(5)
while True:
    try:
        conn, _ = s.accept()
    except Exception:
        break
    try:
        data = conn.recv(1024)
        if b"PING" in data:
            conn.sendall(b"PONG\\n")
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

"""

    supervisor = ProcessSupervisor(name="test_handshake_sup")
    try:
        spec = WorkerSpec(
            worker_id="worker-handshake-1",
            name="handshake_mock_plugin",
            version="1.0.0",
            cmd=[sys.executable, "-c", handshake_server_code],
            endpoint=f"tcp://127.0.0.1:{port}",
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.HANDSHAKE,
                host="127.0.0.1",
                port=port,
                handshake_request=b"PING\n",
                handshake_expected=b"PONG\n",
                interval_sec=0.05,
                initial_delay_sec=0.01,
                max_retries=20,
            ),
        )

        worker = supervisor.launch_initial_worker(spec, readiness_timeout_sec=3.0)

        assert worker.is_alive
        assert worker.state == WorkerState.HEALTHY
    finally:
        supervisor.stop()


# ── Test Suite 2: Zero-Downtime Graceful Hot-Swap (V1 -> V2) ─────────
# 测试组 2：零停机平滑热切与优雅排空


def test_graceful_hot_swap_v1_to_v2() -> None:
    """
    Test upgrading worker from V1 to V2:
    - Launches V2 in background
    - Atomically updates active routing pointer to V2
    - Drains V1 and stops it cleanly after grace period
    """
    supervisor = ProcessSupervisor(name="test_hotswap_sup", watchdog_interval_sec=0.1)

    swapped_events: list[tuple[WorkerProcess | None, WorkerProcess]] = []
    supervisor.on_active_swapped = lambda old, new: swapped_events.append((old, new))

    try:
        v1_spec = WorkerSpec(
            worker_id="worker-v1",
            name="calc_plugin",
            version="1.0.0",
            cmd=[sys.executable, "-c", "import time; time.sleep(15)"],
            endpoint="ipc://v1",
            grace_period=0.3,
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.PID,
                interval_sec=0.05,
            ),
        )

        v1_worker = supervisor.launch_initial_worker(v1_spec, readiness_timeout_sec=2.0)
        v1_pid = v1_worker.pid

        assert supervisor.active_worker is v1_worker
        assert supervisor.active_pid == v1_pid
        assert supervisor.active_endpoint == "ipc://v1"
        assert v1_worker.state == WorkerState.HEALTHY

        # ── Trigger Hot-Swap to V2 ─────────────────────────────────────
        v2_spec = WorkerSpec(
            worker_id="worker-v2",
            name="calc_plugin",
            version="2.0.0",
            cmd=[sys.executable, "-c", "import time; time.sleep(15)"],
            endpoint="ipc://v2",
            grace_period=0.2,
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.PID,
                interval_sec=0.05,
            ),
        )

        v2_worker = supervisor.hot_swap(
            v2_spec,
            drain_grace_period_sec=0.2,
            readiness_timeout_sec=2.0,
            async_drain=True,
        )

        # Immediate assertion: active pointer has atomically switched to V2
        assert supervisor.active_worker is v2_worker
        assert supervisor.active_pid == v2_worker.pid
        assert supervisor.active_pid != v1_pid
        assert supervisor.active_endpoint == "ipc://v2"
        assert v2_worker.state == WorkerState.HEALTHY

        # Verify swapped hook fired
        assert len(swapped_events) == 1
        assert swapped_events[0][0] is v1_worker
        assert swapped_events[0][1] is v2_worker

        # Verify V1 state entered DRAINING or STOPPED
        assert v1_worker.state in (WorkerState.DRAINING, WorkerState.STOPPING, WorkerState.STOPPED)

        # Wait for drain grace period to elapse and verify V1 reaches STOPPED
        time.sleep(0.5)
        assert v1_worker.state == WorkerState.STOPPED
        assert not v1_worker.is_alive

        # V2 remains active and HEALTHY
        assert supervisor.active_worker is v2_worker
        assert v2_worker.is_healthy
    finally:
        supervisor.stop()


# ── Test Suite 3: Hot-Swap Failure Automatic Rollback ─────────────────
# 测试组 3：热切失败自动回滚，宿主零停机


def test_hot_swap_failure_automatic_rollback() -> None:
    """
    Test failure automatic rollback:
    - V1 is active and healthy
    - V2 fails during startup (exits prematurely or fails health check)
    - V2 is immediately destroyed
    - V1 remains active and healthy with 0 downtime
    """
    supervisor = ProcessSupervisor(name="test_rollback_sup", watchdog_interval_sec=0.1)
    try:
        # 1. Launch V1
        v1_spec = WorkerSpec(
            worker_id="worker-stable-v1",
            name="gateway_plugin",
            version="1.0.0",
            cmd=[sys.executable, "-c", "import time; time.sleep(15)"],
            endpoint="ipc://gateway-v1",
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.PID,
                interval_sec=0.05,
            ),
        )

        v1_worker = supervisor.launch_initial_worker(v1_spec, readiness_timeout_sec=2.0)
        v1_pid = v1_worker.pid

        assert supervisor.active_worker is v1_worker
        assert supervisor.active_pid == v1_pid
        assert v1_worker.state == WorkerState.HEALTHY

        # 2. Attempt Hot-Swap with broken V2 (fails instantly with exit code 1)
        broken_v2_spec = WorkerSpec(
            worker_id="worker-broken-v2",
            name="gateway_plugin",
            version="2.0.0-broken",
            cmd=[sys.executable, "-c", "import sys; sys.stderr.write('Fatal boot crash\\n'); sys.exit(1)"],
            endpoint="ipc://gateway-v2",
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.PID,
                interval_sec=0.05,
                max_retries=5,
            ),
        )

        with pytest.raises(HotSwapError) as exc_info:
            supervisor.hot_swap(broken_v2_spec, readiness_timeout_sec=1.0)

        assert "Hot-swap failed" in str(exc_info.value)
        assert f"PID {v1_pid}" in str(exc_info.value)

        # 3. Rollback assertion: V1 routing pointer remains 100% active and healthy
        assert supervisor.active_worker is v1_worker
        assert supervisor.active_pid == v1_pid
        assert supervisor.active_endpoint == "ipc://gateway-v1"
        assert v1_worker.is_alive
        assert v1_worker.state == WorkerState.HEALTHY
    finally:
        supervisor.stop()


def test_hot_swap_unhealthy_probe_rollback() -> None:
    """
    Test rollback when V2 process stays alive but fails network health check.
    """
    supervisor = ProcessSupervisor(name="test_unhealthy_rollback_sup")
    try:
        v1_spec = WorkerSpec(
            worker_id="worker-v1-ok",
            name="db_plugin",
            version="1.0.0",
            cmd=[sys.executable, "-c", "import time; time.sleep(15)"],
            endpoint="tcp://127.0.0.1:9000",
            health_check=HealthCheckConfig(kind=HealthCheckKind.PID, interval_sec=0.05),
        )
        v1_worker = supervisor.launch_initial_worker(v1_spec, readiness_timeout_sec=2.0)

        # V2 points to an unopened TCP port
        dead_port = _find_free_port()
        v2_spec = WorkerSpec(
            worker_id="worker-v2-bad-port",
            name="db_plugin",
            version="2.0.0",
            cmd=[sys.executable, "-c", "import time; time.sleep(15)"],
            endpoint=f"tcp://127.0.0.1:{dead_port}",
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.TCP,
                host="127.0.0.1",
                port=dead_port,
                interval_sec=0.05,
                max_retries=4,
            ),
        )

        with pytest.raises(HotSwapError):
            supervisor.hot_swap(v2_spec, readiness_timeout_sec=0.3)

        # Active worker remains V1
        assert supervisor.active_worker is v1_worker
        assert v1_worker.state == WorkerState.HEALTHY
    finally:
        supervisor.stop()


# ── Test Suite 4: Fault Physical Isolation & Crash Handling ──────────
# 测试组 4：故障物理隔离与异常崩溃捕获


def test_fault_isolation_on_worker_sigsegv_crash() -> None:
    """
    Test worker crashing with SIGSEGV (segmentation fault):
    - Subprocess dies unexpectedly from SIGSEGV
    - Supervisor watchdog captures crash
    - Generates structured CrashReport with signal info
    - Marks state as CRASHED/DEGRADED
    - Host process and Supervisor remain healthy and completely unaffected
    """
    supervisor = ProcessSupervisor(name="test_segv_sup", watchdog_interval_sec=0.05)

    crashes: list[CrashReport] = []
    supervisor.on_crash = lambda worker, crash: crashes.append(crash)

    try:
        # Worker sleeps 0.2s then sends SIGSEGV to itself
        crash_code = """
import time, os, signal, sys
time.sleep(0.2)
sys.stderr.write("Memory corruption fault simulated\\n")
sys.stderr.flush()
os.kill(os.getpid(), signal.SIGSEGV)
"""
        spec = WorkerSpec(
            worker_id="segv-worker-1",
            name="native_c_worker",
            version="1.0.0",
            cmd=[sys.executable, "-c", crash_code],
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.PID,
                interval_sec=0.02,
                initial_delay_sec=0.01,
            ),
        )

        worker = supervisor.launch_initial_worker(spec, readiness_timeout_sec=2.0)
        assert worker.state == WorkerState.HEALTHY

        # Wait for watchdog to detect crash
        deadline = time.time() + 2.0
        while time.time() < deadline and not crashes:
            time.sleep(0.05)

        assert len(crashes) == 1
        report = crashes[0]
        assert report.worker_id == "segv-worker-1"
        assert report.name == "native_c_worker"
        assert report.pid == worker.pid
        assert "SIGSEGV" in (report.signal_name or "")
        assert "Segmentation Fault" in report.reason
        assert "Memory corruption fault simulated" in report.stderr_tail

        # Worker state is CRASHED and DEGRADED
        assert worker.state == WorkerState.CRASHED
        assert worker.is_degraded
        assert not worker.is_alive
        assert not worker.is_healthy

        # Host process is completely unaffected, supervisor is still operational
        assert supervisor.active_worker is None
    finally:
        supervisor.stop()


def test_fault_isolation_on_worker_oom_exit() -> None:
    """
    Test worker terminating with exit code 137 (OOM / SIGKILL):
    - Subprocess exits with 137
    - Structured error log and crash report recorded
    - Supervisor host process remains stable
    """
    supervisor = ProcessSupervisor(name="test_oom_sup", watchdog_interval_sec=0.05)

    crashes: list[CrashReport] = []
    supervisor.on_crash = lambda worker, crash: crashes.append(crash)

    try:
        oom_code = """
import time, sys
time.sleep(0.2)
sys.stderr.write("Out of memory error encountered\\n")
sys.stderr.flush()
sys.exit(137)
"""
        spec = WorkerSpec(
            worker_id="oom-worker-1",
            name="heavy_llm_worker",
            version="1.0.0",
            cmd=[sys.executable, "-c", oom_code],
            health_check=HealthCheckConfig(
                kind=HealthCheckKind.PID,
                interval_sec=0.02,
            ),
        )

        worker = supervisor.launch_initial_worker(spec, readiness_timeout_sec=2.0)
        assert worker.state == WorkerState.HEALTHY

        deadline = time.time() + 2.0
        while time.time() < deadline and not crashes:
            time.sleep(0.05)

        assert len(crashes) == 1
        report = crashes[0]
        assert report.exit_code == 137
        assert "OOM" in (report.signal_name or "") or "SIGKILL" in (report.signal_name or "")
        assert "out of memory" in report.reason.lower() or "137" in report.reason
        assert "Out of memory error encountered" in report.stderr_tail

        assert worker.state == WorkerState.CRASHED
        assert worker.is_degraded
    finally:
        supervisor.stop()


def test_initial_worker_failed_startup_handling() -> None:
    """
    Test that launching an initial worker with invalid executable raises WorkerStartupError
    without crashing host.
    """
    supervisor = ProcessSupervisor(name="test_bad_bin_sup")
    try:
        spec = WorkerSpec(
            worker_id="bad-bin-worker",
            name="non_existent",
            version="0.0.1",
            cmd=["/path/to/definitely/non_existent_binary_xyz_123"],
        )
        with pytest.raises(WorkerStartupError):
            supervisor.launch_initial_worker(spec)

        assert supervisor.active_worker is None
    finally:
        supervisor.stop()
