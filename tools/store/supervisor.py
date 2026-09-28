"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 supervisor.py                                                   │
│  Module: tools.store.supervisor                                     │
│  Role: Process supervisor for standalone plugin workers.            │
│        Handles zero-downtime hot-swap, health probing, and fault   │
│        isolation for native, Python, and Node worker processes.    │
│                                                                     │
│  模块职责：插件独立子进程守护器                                         │
│  · 管理外部独立 Worker 子进程（Native / Python / Node）              │
│  · Worker 状态机模型（STOPPED, STARTING, HEALTHY, DEGRADED, etc.）   │
│  · 本地健康检查（PID / TCP / UDS / 握手探测）                        │
│  · 零停机平滑热切（Graceful Hot-Swap）与失败自动回滚                  │
│  · 物理故障隔离（Fault Isolation）：崩溃捕获与结构化错误记录            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import enum
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

logger = logging.getLogger("cyrene.store.supervisor")


# ── State Machine & Domain Models ────────────────────────────────────
# 状态机与核心领域模型


class WorkerState(str, enum.Enum):
    """
    Lifecycle state machine for a supervised worker process.

    Worker 子进程生命周期状态机模型。
    """
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    CRASHED = "CRASHED"


class HealthCheckKind(str, enum.Enum):
    """
    Probing strategy for local health and readiness checks.

    本地健康与就绪检查探测策略。
    """
    PID = "PID"              # Process existence / alive check (PID 存活检查)
    TCP = "TCP"              # TCP socket connection probe (TCP 端口探测)
    UDS = "UDS"              # Unix Domain Socket connection probe (UDS 套接字探测)
    HANDSHAKE = "HANDSHAKE"  # Request-response handshake probe (交互式握手探测)


class SupervisorError(Exception):
    """Base exception for supervisor operations."""


class WorkerStartupError(SupervisorError):
    """Raised when a worker fails to spawn or start."""


class HealthCheckTimeoutError(SupervisorError):
    """Raised when a worker fails health checks within the deadline."""


class HotSwapError(SupervisorError):
    """Raised when a graceful hot-swap fails."""


@dataclass
class HealthCheckConfig:
    """
    Configuration specification for worker health and readiness checks.

    Worker 健康检查与就绪探测配置参数。
    """
    kind: HealthCheckKind = HealthCheckKind.PID
    host: str = "127.0.0.1"
    port: int | None = None
    socket_path: str | Path | None = None
    timeout_sec: float = 1.0
    interval_sec: float = 0.2
    initial_delay_sec: float = 0.05
    max_retries: int = 15
    handshake_request: bytes | None = None
    handshake_expected: bytes | None = None
    custom_checker: Callable[[WorkerProcess], bool] | None = None


@dataclass
class CrashReport:
    """
    Structured crash diagnostic report for an unexpectedly terminated worker.

    Worker 子进程意外崩溃的结构化故障报告。
    """
    worker_id: str
    name: str
    version: str
    pid: int
    exit_code: int
    signal_name: str | None
    reason: str
    stderr_tail: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert crash report to structured dictionary."""
        return {
            "worker_id": self.worker_id,
            "name": self.name,
            "version": self.version,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "signal_name": self.signal_name,
            "reason": self.reason,
            "stderr_tail": self.stderr_tail,
            "timestamp": self.timestamp,
        }


@dataclass
class WorkerSpec:
    """
    Immutable blueprint for launching an external independent worker process.

    用于拉起独立外部子进程 Worker 的配置定义。
    """
    worker_id: str
    name: str
    version: str
    cmd: list[str]
    cwd: str | Path | None = None
    env: dict[str, str] | None = None
    endpoint: str | None = None
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    grace_period: float = 3.0
    drain_signal: signal.Signals = signal.SIGTERM
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Worker Process Instance ──────────────────────────────────────────
# Worker 子进程封装与单体生命周期


class WorkerProcess:
    """
    Represents an individual supervised worker process instance.

    封装单个外部独立 Worker 子进程的生命周期、状态机与探测交互。
    """

    def __init__(self, spec: WorkerSpec) -> None:
        self.spec = spec
        self._state = WorkerState.STOPPED
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._state_callbacks: list[Callable[[WorkerProcess, WorkerState, WorkerState], None]] = []
        self._latest_crash: CrashReport | None = None
        self._intentional_shutdown = False

    @property
    def worker_id(self) -> str:
        return self.spec.worker_id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def version(self) -> str:
        return self.spec.version

    @property
    def endpoint(self) -> str | None:
        return self.spec.endpoint

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process else None

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    @property
    def latest_crash(self) -> CrashReport | None:
        with self._lock:
            return self._latest_crash

    @property
    def is_alive(self) -> bool:
        """Check if process is currently running at OS level."""
        with self._lock:
            if not self._process:
                return False
            return self._process.poll() is None

    @property
    def is_healthy(self) -> bool:
        """Check if worker state is HEALTHY and process is alive."""
        with self._lock:
            return self._state == WorkerState.HEALTHY and self.is_alive

    @property
    def is_degraded(self) -> bool:
        """Check if worker is in a degraded or crashed state."""
        with self._lock:
            return self._state in (WorkerState.DEGRADED, WorkerState.CRASHED)

    def add_state_listener(
        self, callback: Callable[[WorkerProcess, WorkerState, WorkerState], None]
    ) -> None:
        """Register a state change listener."""
        with self._lock:
            self._state_callbacks.append(callback)

    def _set_state(self, new_state: WorkerState) -> None:
        """Internal helper to mutate state and fire transition callbacks."""
        old_state = self._state
        if old_state == new_state:
            return
        self._state = new_state
        logger.debug(
            "Worker [%s:%s (pid=%s)] transitioned %s -> %s",
            self.name,
            self.version,
            self.pid,
            old_state.value,
            new_state.value,
        )
        for cb in list(self._state_callbacks):
            try:
                cb(self, old_state, new_state)
            except Exception as err:  # noqa: BLE001
                logger.warning("Error in worker state callback: %s", err)

    def start(self) -> None:
        """
        Spawn the underlying OS subprocess and transition to STARTING.

        拉起底层 OS 独立子进程并进入 STARTING 状态。
        """
        with self._lock:
            if self.is_alive:
                logger.warning("Worker [%s] already running with PID %s", self.name, self.pid)
                return

            self._intentional_shutdown = False
            self._latest_crash = None
            self._set_state(WorkerState.STARTING)

            spawn_env = os.environ.copy()
            if self.spec.env:
                spawn_env.update(self.spec.env)

            cwd_path = str(self.spec.cwd) if self.spec.cwd else None

            try:
                self._process = subprocess.Popen(
                    self.spec.cmd,
                    cwd=cwd_path,
                    env=spawn_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                logger.info(
                    "Spawned worker [%s:%s] with PID %s (cmd=%s)",
                    self.name,
                    self.version,
                    self._process.pid,
                    self.spec.cmd,
                )
            except Exception as err:
                self._set_state(WorkerState.STOPPED)
                logger.error("Failed to spawn worker [%s]: %s", self.name, err)
                raise WorkerStartupError(f"Failed to spawn process for {self.name}: {err}") from err

    def probe_health(self) -> bool:
        """
        Perform a single instant health probe against the worker.

        执行单次本地健康状态探测（PID / TCP / UDS / 握手）。
        """
        with self._lock:
            if not self.is_alive:
                return False

            cfg = self.spec.health_check

            # ── 1. Custom Checker override ────────────────────────────
            if cfg.custom_checker is not None:
                try:
                    return cfg.custom_checker(self)
                except Exception as err:  # noqa: BLE001
                    logger.debug("Custom health check failed for [%s]: %s", self.name, err)
                    return False

            # ── 2. Strategy evaluation ────────────────────────────────
            if cfg.kind == HealthCheckKind.PID:
                return self._check_pid_alive()

            elif cfg.kind == HealthCheckKind.TCP:
                return self._check_tcp(cfg)

            elif cfg.kind == HealthCheckKind.UDS:
                return self._check_uds(cfg)

            elif cfg.kind == HealthCheckKind.HANDSHAKE:
                return self._check_handshake(cfg)

            return False

    def _check_pid_alive(self) -> bool:
        """Verify process alive via PID and poll."""
        if not self._process or self._process.poll() is not None:
            return False
        try:
            # Signal 0 checks process existence without sending actual signal
            os.kill(self._process.pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _check_tcp(self, cfg: HealthCheckConfig) -> bool:
        """Probe TCP endpoint connectivity."""
        if cfg.port is None:
            return False
        try:
            with socket.create_connection((cfg.host, cfg.port), timeout=cfg.timeout_sec):
                return True
        except (OSError, TimeoutError):
            return False

    def _check_uds(self, cfg: HealthCheckConfig) -> bool:
        """Probe Unix Domain Socket connectivity."""
        if not cfg.socket_path:
            return False
        path_str = str(cfg.socket_path)
        if not hasattr(socket, "AF_UNIX") or not os.path.exists(path_str):
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(cfg.timeout_sec)
                sock.connect(path_str)
                return True
        except (OSError, TimeoutError):
            return False

    def _check_handshake(self, cfg: HealthCheckConfig) -> bool:
        """Execute request-response handshake verification over TCP or UDS."""
        req = cfg.handshake_request or b"PING\n"
        expected = cfg.handshake_expected or b"PONG\n"

        sock: socket.socket | None = None
        try:
            if cfg.socket_path and hasattr(socket, "AF_UNIX"):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(cfg.timeout_sec)
                sock.connect(str(cfg.socket_path))
            elif cfg.port is not None:
                sock = socket.create_connection((cfg.host, cfg.port), timeout=cfg.timeout_sec)
                sock.settimeout(cfg.timeout_sec)
            else:
                return False

            sock.sendall(req)
            resp = sock.recv(len(expected) + 64)
            return expected in resp
        except Exception as err:  # noqa: BLE001
            logger.debug("Handshake check failed for [%s]: %s", self.name, err)
            return False
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def wait_until_healthy(self, timeout_sec: float | None = None) -> bool:
        """
        Poll health check until the worker becomes HEALTHY or timeout expires.

        轮询健康检查直到 Worker 达到 HEALTHY 就绪状态或超时。
        """
        cfg = self.spec.health_check
        effective_timeout = timeout_sec or (cfg.max_retries * cfg.interval_sec + cfg.initial_delay_sec)
        deadline = time.time() + effective_timeout

        if cfg.initial_delay_sec > 0:
            time.sleep(cfg.initial_delay_sec)

        while time.time() < deadline:
            # Check if process terminated prematurely
            with self._lock:
                if self._process and self._process.poll() is not None:
                    exit_code = self._process.returncode
                    self._record_crash_or_exit(exit_code, "Worker process exited during startup")
                    return False

            if self.probe_health():
                with self._lock:
                    self._set_state(WorkerState.HEALTHY)
                return True

            time.sleep(cfg.interval_sec)

        logger.warning(
            "Worker [%s:%s] failed readiness probe within %.2fs deadline",
            self.name,
            self.version,
            effective_timeout,
        )
        return False

    def drain(self, grace_period_sec: float | None = None) -> None:
        """
        Transition worker into DRAINING state to reject new traffic and flush in-flight tasks.

        将 Worker 转入 DRAINING 排空状态，等待在途任务完成。
        """
        with self._lock:
            if self._state not in (WorkerState.HEALTHY, WorkerState.DEGRADED):
                return
            self._set_state(WorkerState.DRAINING)

        period = grace_period_sec if grace_period_sec is not None else self.spec.grace_period
        logger.info(
            "Worker [%s:%s] is draining (grace_period=%.2fs)...",
            self.name,
            self.version,
            period,
        )

    def stop(self, grace_period_sec: float | None = None, force: bool = False) -> None:
        """
        Gracefully stop worker process (SIGTERM -> wait -> SIGKILL).

        优雅终止 Worker 进程（发送 SIGTERM，等待退出；超时则发送 SIGKILL 强杀）。
        """
        with self._lock:
            self._intentional_shutdown = True
            if not self._process or self._process.poll() is not None:
                self._set_state(WorkerState.STOPPED)
                return

            self._set_state(WorkerState.STOPPING)
            proc = self._process

        period = 0.0 if force else (grace_period_sec if grace_period_sec is not None else self.spec.grace_period)

        try:
            if not force and period > 0:
                time.sleep(period)

            # Send SIGTERM
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                pass

            # Wait for exit
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                logger.warning("Worker [%s (pid=%s)] did not exit on SIGTERM; sending SIGKILL", self.name, proc.pid)
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except (OSError, ProcessLookupError):
                    pass

        finally:
            with self._lock:
                self._set_state(WorkerState.STOPPED)

    def check_and_handle_exit(self) -> CrashReport | None:
        """
        Inspect process exit status and handle fault isolation if termination was unexpected.

        检查子进程退出状态；若为非预期退出（崩溃/SIGSEGV/OOM），执行故障隔离并生成报告。
        """
        with self._lock:
            if not self._process:
                return None
            exit_code = self._process.poll()
            if exit_code is None:
                return None

            if self._state in (WorkerState.STOPPED, WorkerState.STOPPING) and self._intentional_shutdown:
                # Normal intentional shutdown
                return None

            if self._state == WorkerState.CRASHED and self._latest_crash:
                # Already handled
                return self._latest_crash

            # Unexpected crash or exit
            reason = self._classify_exit_code(exit_code)
            return self._record_crash_or_exit(exit_code, reason)

    def _record_crash_or_exit(self, exit_code: int, reason: str) -> CrashReport:
        """Record structured crash diagnostic report and set CRASHED/DEGRADED state."""
        stderr_tail = ""
        try:
            if self._process and self._process.stderr:
                stderr_tail = self._process.stderr.read() or ""
        except (OSError, ValueError):
            pass

        signal_name = None
        if exit_code < 0:
            try:
                signal_name = signal.Signals(-exit_code).name
            except (ValueError, AttributeError):
                signal_name = f"SIG{-exit_code}"
        elif exit_code in (137, 139, 143):
            # Common shell exit codes for 128 + signal (137=SIGKILL/OOM, 139=SIGSEGV)
            sig_map = {137: "SIGKILL/OOM", 139: "SIGSEGV", 143: "SIGTERM"}
            signal_name = sig_map.get(exit_code)

        report = CrashReport(
            worker_id=self.worker_id,
            name=self.name,
            version=self.version,
            pid=self.pid or -1,
            exit_code=exit_code,
            signal_name=signal_name,
            reason=reason,
            stderr_tail=stderr_tail.strip(),
        )

        self._latest_crash = report
        # Mark as CRASHED and DEGRADED
        self._set_state(WorkerState.CRASHED)

        logger.error(
            "Worker [%s:%s (pid=%s)] CRASHED! ExitCode=%s, Signal=%s, Reason=%s. ErrorLog:\n%s",
            self.name,
            self.version,
            report.pid,
            report.exit_code,
            report.signal_name,
            report.reason,
            report.stderr_tail,
        )
        return report

    @staticmethod
    def _classify_exit_code(code: int) -> str:
        """Classify exit code into human-readable fault category."""
        if code == -signal.SIGSEGV or code == 139:
            return "Segmentation Fault (SIGSEGV / Memory Access Violation)"
        elif code == -signal.SIGKILL or code == 137:
            return "Killed by OS / Out Of Memory (OOM Killer / SIGKILL)"
        elif code == -signal.SIGABRT or code == 134:
            return "Aborted by Process (SIGABRT)"
        elif code == 0:
            return "Premature Clean Exit (Exit Code 0)"
        else:
            return f"Non-zero exit with code {code}"


# ── Process Supervisor Engine ────────────────────────────────────────
# 进程守护器引擎：热插拔、故障隔离与健康看门狗


class ProcessSupervisor:
    """
    Supervisor managing standalone worker processes with zero-downtime hot-swap,
    local health probing, and fault physical isolation.

    面向独立子进程 Worker 的进程守护器。
    具备零停机平滑热切（Graceful Hot-Swap）、本地健康探测与物理故障隔离能力。
    """

    def __init__(
        self,
        name: str = "default_supervisor",
        watchdog_interval_sec: float = 0.5,
    ) -> None:
        self.name = name
        self.watchdog_interval_sec = watchdog_interval_sec

        self._lock = threading.RLock()
        self._active_worker: WorkerProcess | None = None
        self._all_workers: list[WorkerProcess] = []

        self._watchdog_thread: threading.Thread | None = None
        self._stop_watchdog_event = threading.Event()

        # Host Notification Hooks (宿主通知回调)
        self.on_crash: Callable[[WorkerProcess, CrashReport], None] | None = None
        self.on_state_change: Callable[[WorkerProcess, WorkerState, WorkerState], None] | None = None
        self.on_active_swapped: Callable[[WorkerProcess | None, WorkerProcess], None] | None = None

    # ── Active Routing Pointer Accessors ──────────────────────────────
    # 活跃路由指针访问器

    @property
    def active_worker(self) -> WorkerProcess | None:
        """Current active worker instance."""
        with self._lock:
            return self._active_worker

    @property
    def active_pid(self) -> int | None:
        """Active worker PID."""
        with self._lock:
            return self._active_worker.pid if self._active_worker else None

    @property
    def active_endpoint(self) -> str | None:
        """Active worker communication endpoint."""
        with self._lock:
            return self._active_worker.endpoint if self._active_worker else None

    @property
    def active_state(self) -> WorkerState:
        """Active worker state or STOPPED if no active worker."""
        with self._lock:
            return self._active_worker.state if self._active_worker else WorkerState.STOPPED

    # ── Lifecycle Management ──────────────────────────────────────────
    # 守护器生命周期

    def start(self) -> None:
        """Start supervisor background watchdog monitor thread."""
        with self._lock:
            if self._watchdog_thread and self._watchdog_thread.is_alive():
                return
            self._stop_watchdog_event.clear()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name=f"supervisor-watchdog-{self.name}",
                daemon=True,
            )
            self._watchdog_thread.start()
            logger.info("ProcessSupervisor [%s] watchdog started", self.name)

    def stop(self, grace_period_sec: float = 1.0) -> None:
        """Stop supervisor and terminate all managed workers."""
        logger.info("Stopping ProcessSupervisor [%s]...", self.name)
        self._stop_watchdog_event.set()

        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2.0)

        with self._lock:
            workers = list(self._all_workers)
            self._active_worker = None

        for worker in workers:
            try:
                worker.stop(grace_period_sec=grace_period_sec, force=False)
            except Exception as err:  # noqa: BLE001
                logger.warning("Error stopping worker [%s]: %s", worker.name, err)

        logger.info("ProcessSupervisor [%s] stopped successfully", self.name)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.stop()

    # ── Worker Launch & Registration ──────────────────────────────────
    # Worker 启动与注册

    def spawn_worker(self, spec: WorkerSpec) -> WorkerProcess:
        """
        Create, register, and start a worker process.

        创建、注册并拉起 Worker 实例。
        """
        worker = WorkerProcess(spec)
        worker.add_state_listener(self._handle_worker_state_change)

        with self._lock:
            self._all_workers.append(worker)

        # Launch underlying subprocess
        worker.start()
        return worker

    def launch_initial_worker(
        self, spec: WorkerSpec, readiness_timeout_sec: float | None = None
    ) -> WorkerProcess:
        """
        Launch the initial worker (e.g. V1) and designate it as the active worker upon healthy check.

        拉起初始 Worker（如 V1），并在通过健康检查后原子指派为当前活跃 Worker。
        """
        self.start()
        worker = self.spawn_worker(spec)

        # Readiness Probe
        if not worker.wait_until_healthy(timeout_sec=readiness_timeout_sec):
            err_msg = ""
            if worker.latest_crash:
                err_msg = f": {worker.latest_crash.reason} (stderr: {worker.latest_crash.stderr_tail})"
            worker.stop(force=True)
            with self._lock:
                if worker in self._all_workers:
                    self._all_workers.remove(worker)
            raise WorkerStartupError(
                f"Initial worker [{spec.name}:{spec.version}] failed health check{err_msg}"
            )

        with self._lock:
            self._active_worker = worker

        logger.info(
            "Initial worker [%s:%s (pid=%s)] activated successfully at endpoint %s",
            worker.name,
            worker.version,
            worker.pid,
            worker.endpoint,
        )
        return worker

    # ── Zero-Downtime Graceful Hot-Swap ────────────────────────────────
    # 零停机平滑热切与失败自动回滚

    def hot_swap(
        self,
        new_spec: WorkerSpec,
        drain_grace_period_sec: float | None = None,
        readiness_timeout_sec: float | None = None,
        async_drain: bool = True,
    ) -> WorkerProcess:
        """
        Perform zero-downtime graceful hot-swap from current worker to new worker (V1 -> V2).

        执行零停机平滑热切（V1 -> V2）：
        1. 在后台拉起 V2 Worker 进程并执行就绪检查；
        2. V2 就绪后，原子更新活跃路由指针（active PID / 通信端点）；
        3. 优雅排空（Drain）：向 V1 发送排空信号并在等待窗口（grace_period）后退出；
        4. 失败自动回滚：若 V2 启动失败或未能通过健康检查，立即销毁 V2，保持 V1 路由不变，宿主 0 停机。

        Args:
            new_spec: Specification for the replacement worker (V2).
            drain_grace_period_sec: Grace period for V1 in-flight requests drain.
            readiness_timeout_sec: Timeout waiting for V2 to reach HEALTHY state.
            async_drain: If True, drain and terminate V1 in background thread.

        Returns:
            The newly activated WorkerProcess (V2).

        Raises:
            HotSwapError: If V2 fails to become healthy. V1 remains untouched.
        """
        self.start()
        logger.info(
            "Initiating graceful hot-swap to [%s:%s]...",
            new_spec.name,
            new_spec.version,
        )

        # ── Step 1: Spawn replacement worker (V2) in background ──────────
        v2_worker = self.spawn_worker(new_spec)

        # ── Step 2: Readiness probe for V2 ───────────────────────────────
        ready = v2_worker.wait_until_healthy(timeout_sec=readiness_timeout_sec)

        # ── Step 3: Failure automatic rollback ───────────────────────────
        if not ready:
            logger.error(
                "Hot-swap failed: replacement worker [%s:%s] failed readiness check. Rolling back...",
                new_spec.name,
                new_spec.version,
            )
            # Destroy V2 immediately
            v2_worker.stop(force=True)
            with self._lock:
                if v2_worker in self._all_workers:
                    self._all_workers.remove(v2_worker)

            # Assert V1 is intact
            with self._lock:
                active = self._active_worker
                active_info = f"PID {active.pid}" if active else "None"

            raise HotSwapError(
                f"Hot-swap failed: replacement worker [{new_spec.name}:{new_spec.version}] "
                f"unhealthy. Active worker retained at {active_info}."
            )

        # ── Step 4: Atomic pointer switch ────────────────────────────────
        with self._lock:
            old_worker = self._active_worker
            self._active_worker = v2_worker

        logger.info(
            "Hot-swap atomic switch complete: active pointer moved from [%s] to [%s:%s (pid=%s)]",
            f"pid={old_worker.pid}" if old_worker else "none",
            v2_worker.name,
            v2_worker.version,
            v2_worker.pid,
        )

        if self.on_active_swapped:
            try:
                self.on_active_swapped(old_worker, v2_worker)
            except Exception as err:  # noqa: BLE001
                logger.warning("Error in on_active_swapped hook: %s", err)

        # ── Step 5: Graceful drain of previous worker (V1) ───────────────
        if old_worker and old_worker.is_alive:
            grace_period = (
                drain_grace_period_sec
                if drain_grace_period_sec is not None
                else old_worker.spec.grace_period
            )

            if async_drain:
                drain_thread = threading.Thread(
                    target=self._drain_and_stop_worker,
                    args=(old_worker, grace_period),
                    name=f"drain-{old_worker.name}-{old_worker.pid}",
                    daemon=True,
                )
                drain_thread.start()
            else:
                self._drain_and_stop_worker(old_worker, grace_period)

        return v2_worker

    def _drain_and_stop_worker(self, worker: WorkerProcess, grace_period: float) -> None:
        """Drain worker in-flight requests and stop subprocess."""
        try:
            worker.drain(grace_period_sec=grace_period)
            worker.stop(grace_period_sec=grace_period, force=False)
        except Exception as err:  # noqa: BLE001
            logger.error("Error during draining worker [%s]: %s", worker.name, err)
        finally:
            with self._lock:
                if worker in self._all_workers and not worker.is_alive:
                    self._all_workers.remove(worker)

    # ── Fault Physical Isolation & Watchdog ───────────────────────────
    # 物理故障隔离与后台看门狗轮询

    def _watchdog_loop(self) -> None:
        """
        Continuous background monitoring loop checking health and handling crashes.
        Fully isolates host: exceptions inside worker monitor will never crash host.

        后台持续监视循环：轮询健康状态、捕获异常崩溃。
        严格物理隔离：子进程崩溃与任何监控异常绝不波及宿主进程。
        """
        while not self._stop_watchdog_event.is_set():
            try:
                self._check_all_workers_once()
            except Exception as loop_err:  # noqa: BLE001
                logger.error("Unexpected error in supervisor watchdog loop: %s", loop_err)

            self._stop_watchdog_event.wait(timeout=self.watchdog_interval_sec)

    def _check_all_workers_once(self) -> None:
        """Single pass over all supervised workers."""
        with self._lock:
            workers_snapshot = list(self._all_workers)

        for worker in workers_snapshot:
            # 1. Check for unexpected crashes
            crash = worker.check_and_handle_exit()
            if crash:
                self._handle_worker_crashed(worker, crash)
                continue

            # 2. Periodic health check if worker is currently HEALTHY
            if worker.state == WorkerState.HEALTHY:
                if not worker.probe_health():
                    logger.warning(
                        "Worker [%s:%s (pid=%s)] failed periodic health probe; marking DEGRADED",
                        worker.name,
                        worker.version,
                        worker.pid,
                    )
                    worker._set_state(WorkerState.DEGRADED)
            elif worker.state == WorkerState.DEGRADED and worker.probe_health():
                # Attempt recovery check
                logger.info(
                    "Worker [%s:%s (pid=%s)] recovered from DEGRADED to HEALTHY",
                    worker.name,
                    worker.version,
                    worker.pid,
                )
                worker._set_state(WorkerState.HEALTHY)

    def _handle_worker_crashed(self, worker: WorkerProcess, crash: CrashReport) -> None:
        """Handle worker unexpected termination under fault isolation."""
        with self._lock:
            if self._active_worker is worker:
                logger.critical(
                    "Active worker [%s (pid=%s)] crashed! Active endpoint lost.",
                    worker.name,
                    crash.pid,
                )
                self._active_worker = None

        if self.on_crash:
            try:
                self.on_crash(worker, crash)
            except Exception as cb_err:  # noqa: BLE001
                logger.error("Error in on_crash notification callback: %s", cb_err)

    def _handle_worker_state_change(
        self, worker: WorkerProcess, old_state: WorkerState, new_state: WorkerState
    ) -> None:
        """Relay worker state transitions to host callback."""
        if self.on_state_change:
            try:
                self.on_state_change(worker, old_state, new_state)
            except Exception as err:  # noqa: BLE001
                logger.error("Error in on_state_change callback: %s", err)
