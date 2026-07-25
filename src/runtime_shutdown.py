"""Apagado coordinado y seguro del árbol de procesos de una instancia.

La sesión persistida es la señal cooperativa. Este módulo solo escala sobre procesos
cuya identidad registrada aún coincide; nunca busca ni mata intérpretes por nombre.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Protocol, Sequence


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    create_time: float
    command: tuple[str, ...]
    parent_pid: int = 0


@dataclass(frozen=True)
class OwnedProcess:
    module: str
    instance_id: str
    root: ProcessSnapshot
    class_dir: str = ""


@dataclass(frozen=True)
class StopPolicy:
    cooperative_timeout: float = 20.0
    terminate_timeout: float = 5.0
    kill_timeout: float = 2.0


@dataclass(frozen=True)
class StopResult:
    module: str
    pid: int
    status: str
    detail: str = ""
    remaining_pids: tuple[int, ...] = ()


class ProcessOps(Protocol):
    def snapshot(self, pid: int) -> ProcessSnapshot | None: ...

    def descendants(self, pid: int) -> Sequence[ProcessSnapshot]: ...

    def wait(self, snapshot: ProcessSnapshot, timeout: float) -> str: ...

    def terminate(self, snapshot: ProcessSnapshot) -> str: ...

    def kill(self, snapshot: ProcessSnapshot) -> str: ...


def _command_matches(snapshot: ProcessSnapshot, module: str) -> bool:
    module = str(module or "").strip().lower()
    parts = tuple(str(part).strip().lower() for part in snapshot.command)
    if not module or not parts:
        return False
    if module in parts:
        return True
    for index, part in enumerate(parts[:-1]):
        if part == "-m" and parts[index + 1] == module:
            return True
    leaf = module.rsplit(".", 1)[-1]
    return any(
        part.replace("\\", "/").rsplit("/", 1)[-1]
        in {leaf, f"{leaf}.py"}
        for part in parts
    )


def _same_process(expected: ProcessSnapshot,
                  current: ProcessSnapshot | None,
                  module: str) -> bool:
    if current is None or int(current.pid) != int(expected.pid):
        return False
    if abs(float(current.create_time) - float(expected.create_time)) > 0.01:
        return False
    return _command_matches(current, module)


def _still_same(ops: ProcessOps, expected: ProcessSnapshot,
                module: str) -> ProcessSnapshot | None:
    current = ops.snapshot(expected.pid)
    return current if _same_process(expected, current, module) else None


def _owned_descendants(child: OwnedProcess, ops: ProcessOps) -> list[ProcessSnapshot]:
    descendants = []
    for snapshot in ops.descendants(child.root.pid):
        if (_command_matches(snapshot, child.module)
                and float(snapshot.create_time) + 0.01
                >= float(child.root.create_time)):
            descendants.append(snapshot)
    # Los más profundos suelen ser devueltos al final por psutil; el PID descendente
    # se detiene antes del wrapper raíz para evitar dejar el intérprete base huérfano.
    return list(reversed(descendants))


def _wait_tracked(
        ops: ProcessOps,
        snapshots: Sequence[ProcessSnapshot],
        timeout: float,
) -> tuple[list[ProcessSnapshot], list[str]]:
    """Espera un conjunto con un único presupuesto, no un timeout por PID."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    remaining = []
    notes = []
    for expected in snapshots:
        available = max(0.0, deadline - time.monotonic())
        status = str(ops.wait(expected, available) or "error")
        if status in {"exited", "identity_lost"}:
            if status == "identity_lost":
                notes.append(f"pid={expected.pid}:identity_lost_during_wait")
            continue
        remaining.append(expected)
        if status != "timeout":
            notes.append(f"pid={expected.pid}:wait_{status}")
    return remaining, notes


def _signal_verified(
        ops: ProcessOps,
        snapshots: Sequence[ProcessSnapshot],
        module: str,
        action: str,
) -> tuple[list[ProcessSnapshot], list[str]]:
    remaining = []
    notes = []
    for expected in snapshots:
        current = _still_same(ops, expected, module)
        if current is None:
            probe = str(ops.wait(expected, 0.0) or "error")
            if probe in {"denied", "error", "timeout"}:
                remaining.append(expected)
                notes.append(f"pid={expected.pid}:verify_{probe}")
            elif probe == "identity_lost":
                notes.append(f"pid={expected.pid}:identity_lost_before_{action}")
            continue
        status = str(getattr(ops, action)(current) or "signaled")
        if status == "signaled":
            remaining.append(current)
        elif status in {"denied", "error"}:
            remaining.append(current)
            notes.append(f"pid={current.pid}:{action}_{status}")
        elif status == "identity_lost":
            notes.append(f"pid={current.pid}:identity_lost_during_{action}")
    return remaining, notes


def _stop_result(
        child: OwnedProcess,
        status: str,
        *,
        notes: Sequence[str] = (),
        remaining: Sequence[ProcessSnapshot] = (),
) -> StopResult:
    pids = tuple(sorted({int(snapshot.pid) for snapshot in remaining}))
    return StopResult(
        child.module,
        child.root.pid,
        status,
        "; ".join(dict.fromkeys(str(note) for note in notes if note)),
        pids,
    )


def stop_owned_process(child: OwnedProcess, ops: ProcessOps,
                       policy: StopPolicy, *, instance_id: str) -> StopResult:
    """Drena el árbol propio y escala sin actuar sobre una identidad ambigua."""
    if str(child.instance_id) != str(instance_id):
        return _stop_result(
            child, "ownership_lost", notes=("instance_id distinto",))
    if _still_same(ops, child.root, child.module) is None:
        probe = str(ops.wait(child.root, 0.0) or "error")
        if probe == "exited":
            return _stop_result(child, "already_stopped")
        if probe in {"denied", "error"}:
            return _stop_result(
                child,
                "unresolved",
                notes=(f"no se pudo verificar el proceso raíz: {probe}",),
                remaining=(child.root,),
            )
        return _stop_result(
            child,
            "ownership_lost",
            notes=("PID reutilizado o comando distinto",),
        )

    descendants = _owned_descendants(child, ops)
    tracked = [*descendants, child.root]
    remaining, notes = _wait_tracked(
        ops, tracked, policy.cooperative_timeout)
    if not remaining:
        return _stop_result(child, "cooperative", notes=notes)

    signaled, signal_notes = _signal_verified(
        ops, remaining, child.module, "terminate")
    notes.extend(signal_notes)
    remaining, wait_notes = _wait_tracked(
        ops, signaled, policy.terminate_timeout)
    notes.extend(wait_notes)
    if not remaining:
        return _stop_result(child, "terminated", notes=notes)

    signaled, signal_notes = _signal_verified(
        ops, remaining, child.module, "kill")
    notes.extend(signal_notes)
    remaining, wait_notes = _wait_tracked(
        ops, signaled, policy.kill_timeout)
    notes.extend(wait_notes)
    if not remaining:
        return _stop_result(child, "killed", notes=notes)
    notes.append("el árbol propio no terminó tras kill")
    return _stop_result(
        child, "unresolved", notes=notes, remaining=remaining)


_STOP_ORDER = {
    "src.captura_audio": 10,
    "src.captura_pantalla": 20,
    "src.transcriptor_vivo": 30,
    "src.escriba": 40,
}


def stop_owned_children(children: Sequence[OwnedProcess], ops: ProcessOps,
                        policy: StopPolicy, *, instance_id: str) -> list[StopResult]:
    ordered = sorted(
        children,
        key=lambda child: (_STOP_ORDER.get(child.module, 100), child.module),
    )
    results = []
    for child in ordered:
        try:
            results.append(stop_owned_process(
                child, ops, policy, instance_id=instance_id))
        except Exception as error:
            # Un hijo defectuoso no debe impedir intentar cerrar los restantes.
            results.append(_stop_result(
                child,
                "unresolved",
                notes=(f"{type(error).__name__}: {error}",),
                remaining=(child.root,),
            ))
    return results


@dataclass
class ShutdownTicket:
    accepted: bool
    completed: threading.Event
    reason: str
    close_app: bool


class ShutdownCoordinator:
    """Deduplica solicitudes concurrentes y conserva un único reporte terminal."""

    def __init__(self, runner: Callable[[str, bool], dict]):
        self._runner = runner
        self._lock = threading.Lock()
        self._completed = threading.Event()
        self._started = False
        self.report: dict = {}

    def request(self, reason: str, close_app: bool = False) -> ShutdownTicket:
        with self._lock:
            if self._started and not self._completed.is_set():
                return ShutdownTicket(
                    False, self._completed, str(reason), bool(close_app))
            if self._started:
                self._completed = threading.Event()
                self.report = {}
            self._started = True
            ticket = ShutdownTicket(
                True, self._completed, str(reason), bool(close_app))
            threading.Thread(
                target=self._run,
                args=(str(reason), bool(close_app)),
                name="clase-viva-shutdown",
                daemon=False,
            ).start()
            return ticket

    def _run(self, reason: str, close_app: bool) -> None:
        try:
            result = self._runner(reason, close_app)
            self.report = dict(result or {"status": "complete"})
        except BaseException as error:
            self.report = {
                "status": "error",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        finally:
            self._completed.set()


class PsutilProcessOps:
    """Adaptador Windows; psutil ya forma parte del entorno actual de ClaseViva."""

    @staticmethod
    def _psutil():
        import psutil
        return psutil

    def snapshot(self, pid: int) -> ProcessSnapshot | None:
        psutil = self._psutil()
        try:
            process = psutil.Process(int(pid))
            return ProcessSnapshot(
                pid=int(process.pid),
                create_time=float(process.create_time()),
                command=tuple(str(part) for part in process.cmdline()),
                parent_pid=int(process.ppid()),
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    def descendants(self, pid: int) -> list[ProcessSnapshot]:
        psutil = self._psutil()
        try:
            processes = psutil.Process(int(pid)).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return []
        snapshots = []
        for process in processes:
            snapshot = self.snapshot(process.pid)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def _verified_process(self, snapshot: ProcessSnapshot):
        """Conserva el mismo objeto psutil desde fingerprint hasta operación."""
        psutil = self._psutil()
        try:
            process = psutil.Process(int(snapshot.pid))
            create_time = float(process.create_time())
            command = tuple(str(part) for part in process.cmdline())
        except psutil.NoSuchProcess:
            return None, "gone"
        except psutil.AccessDenied:
            return None, "denied"
        except OSError:
            return None, "error"
        if abs(create_time - float(snapshot.create_time)) > 0.01:
            return None, "identity_lost"
        if snapshot.command and command != tuple(snapshot.command):
            return None, "identity_lost"
        return process, "matched"

    def wait(self, snapshot: ProcessSnapshot, timeout: float) -> str:
        psutil = self._psutil()
        process, status = self._verified_process(snapshot)
        if status == "gone":
            return "exited"
        if process is None:
            return status
        try:
            process.wait(timeout=max(0.0, float(timeout)))
            return "exited"
        except psutil.NoSuchProcess:
            return "exited"
        except psutil.TimeoutExpired:
            return "timeout"
        except psutil.AccessDenied:
            return "denied"
        except OSError:
            return "error"

    def _signal(self, snapshot: ProcessSnapshot, action: str) -> str:
        psutil = self._psutil()
        process, status = self._verified_process(snapshot)
        if process is None:
            return status
        try:
            getattr(process, action)()
            return "signaled"
        except psutil.NoSuchProcess:
            # Salió o psutil detectó reutilización antes de tocar el PID actual.
            return "gone"
        except psutil.AccessDenied:
            return "denied"
        except OSError:
            return "error"

    def terminate(self, snapshot: ProcessSnapshot) -> str:
        return self._signal(snapshot, "terminate")

    def kill(self, snapshot: ProcessSnapshot) -> str:
        return self._signal(snapshot, "kill")
