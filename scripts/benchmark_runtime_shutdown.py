"""Benchmark sintético de cierre de un árbol de procesos propio.

No inspecciona ni termina procesos por nombre. Cada PID se crea en esta ejecución y se
verifica por create_time + comando antes del cierre y del cleanup defensivo.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime_shutdown import (  # noqa: E402
    OwnedProcess,
    ProcessSnapshot,
    PsutilProcessOps,
    StopPolicy,
    stop_owned_process,
)


def _same_snapshot(ops: PsutilProcessOps, expected: ProcessSnapshot) -> bool:
    current = ops.snapshot(expected.pid)
    return bool(
        current is not None
        and abs(current.create_time - expected.create_time) <= 0.01
        and current.command == expected.command
    )


def _cleanup_owned(
        ops: PsutilProcessOps,
        snapshots: list[ProcessSnapshot],
) -> None:
    for snapshot in snapshots:
        if not _same_snapshot(ops, snapshot):
            continue
        ops.kill(snapshot)
        ops.wait(snapshot, 2.0)


def _spawn_tree(marker: str) -> tuple[subprocess.Popen, list[ProcessSnapshot]]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child_code = "import time; time.sleep(60)"
    root_code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]);"
        "print(p.pid,flush=True);"
        "time.sleep(60)"
    )
    root = subprocess.Popen(
        [sys.executable, "-c", root_code, marker, child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=flags,
    )
    line = root.stdout.readline().strip() if root.stdout is not None else ""
    if root.stdout is not None:
        root.stdout.close()
    if not line.isdigit():
        root.kill()
        root.wait(timeout=3)
        raise RuntimeError("el proceso raíz no publicó el PID descendiente")

    ops = PsutilProcessOps()
    root_snapshot = ops.snapshot(root.pid)
    child_snapshot = ops.snapshot(int(line))
    if root_snapshot is None or child_snapshot is None:
        known = [item for item in (child_snapshot, root_snapshot) if item is not None]
        _cleanup_owned(ops, known)
        if root.poll() is None:
            root.kill()
            root.wait(timeout=3)
        raise RuntimeError("no se pudo obtener el fingerprint del árbol sintético")
    if marker not in root_snapshot.command or marker not in child_snapshot.command:
        _cleanup_owned(ops, [child_snapshot, root_snapshot])
        raise RuntimeError("el marcador sintético no aparece en ambos comandos")
    return root, [child_snapshot, root_snapshot]


def run_benchmark(iterations: int) -> dict:
    if iterations <= 0:
        raise ValueError("iterations debe ser mayor que cero")
    ops = PsutilProcessOps()
    latencies_ms = []
    statuses = Counter()
    exceptions = []
    residuals = []

    for index in range(iterations):
        marker = f"clase_viva_shutdown_bench_{uuid.uuid4().hex}"
        root = None
        snapshots: list[ProcessSnapshot] = []
        started = time.perf_counter()
        try:
            root, snapshots = _spawn_tree(marker)
            owned = OwnedProcess(
                module=marker,
                instance_id=marker,
                root=snapshots[-1],
            )
            result = stop_owned_process(
                owned,
                ops,
                StopPolicy(
                    cooperative_timeout=0.0,
                    terminate_timeout=3.0,
                    kill_timeout=1.0,
                ),
                instance_id=marker,
            )
            statuses[result.status] += 1
            alive = [
                snapshot.pid for snapshot in snapshots
                if _same_snapshot(ops, snapshot)
            ]
            if alive:
                residuals.append({"iteration": index, "pids": alive})
        except Exception as error:  # el benchmark debe informar, no ocultar
            exceptions.append({
                "iteration": index,
                "type": type(error).__name__,
                "message": str(error),
            })
        finally:
            _cleanup_owned(ops, snapshots)
            if root is not None and root.poll() is None:
                # Handle propio creado arriba; no hay búsqueda global por PID/nombre.
                root.kill()
                root.wait(timeout=3)
            latencies_ms.append((time.perf_counter() - started) * 1000.0)

    ordered = sorted(latencies_ms)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    gate_passed = (
        not exceptions
        and not residuals
        and sum(statuses.values()) == iterations
        and set(statuses).issubset({"terminated", "killed"})
    )
    return {
        "schema_version": 1,
        "workload": {
            "kind": "owned_process_tree",
            "iterations": iterations,
            "processes_per_iteration": 2,
            "policy_seconds": {
                "cooperative": 0.0,
                "terminate": 3.0,
                "kill": 1.0,
            },
        },
        "result": {
            "completed_iterations": sum(statuses.values()),
            "status_counts": dict(sorted(statuses.items())),
            "exceptions": exceptions,
            "residual_iterations": residuals,
            "residual_processes": sum(len(item["pids"]) for item in residuals),
            "latency_ms_p50": statistics.median(latencies_ms),
            "latency_ms_p95": ordered[p95_index],
            "latency_ms_max": max(latencies_ms),
        },
        "gate": {
            "passed": gate_passed,
            "requirements": [
                "sin excepciones",
                "cero procesos propios residuales",
                "todas las iteraciones terminan por terminate/kill",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = run_benchmark(args.iterations)
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({
            "schema_version": 1,
            "gate": {"passed": False},
            "error": {"type": type(error).__name__, "message": str(error)},
        }, ensure_ascii=False))
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
