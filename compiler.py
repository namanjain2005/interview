#!/usr/bin/env python3
"""Luminal Compiler Take Home — compiler engineering candidate implementation.

The starter is intentionally conservative: it allocates every SSA value once
and emits at most one operation per bundle. Improve compile_program without
changing its input or output contract. Reuse scratch for values whose scheduled
lifetimes do not overlap to improve the scratch-footprint component of the score.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

import machine


def compile_program(program: dict) -> dict:
    """Compile one validated IR program into scratch allocations and bundles."""

    # A simple non-overlapping allocation. Vectors are placed first so their
    # alignment does not create holes between scalar values.
    scratch: dict[str, int] = {}
    cursor = 0
    operations = program["operations"]

    for result_kind in ("vector", "scalar"):
        for operation in operations:
            spec = machine.OP_SPECS[operation["op"]]
            if spec["result"] != result_kind:
                continue
            dest = operation["dest"]
            if result_kind == "vector":
                cursor = machine.align_up(cursor, machine.VLEN)
                scratch[dest] = cursor
                cursor += machine.VLEN
            else:
                scratch[dest] = cursor
                cursor += 1

    if cursor > machine.SCRATCH_WORDS:
        raise machine.CompileError(
            f"program requires {cursor} scratch words, limit is {machine.SCRATCH_WORDS}"
        )

    # Build precedence edges. A data edge carries the producer latency; memory
    # edges carry one cycle because ordered accesses must issue separately.
    producer = machine.producer_map(program)
    successors: list[list[tuple[int, int]]] = [[] for _ in operations]
    dependency_edges: list[list[tuple[int, int]]] = [[] for _ in operations]
    for operation in operations:
        op_id = operation["id"]
        predecessors = {}
        for arg in operation.get("args", []):
            pred_id = producer[arg]
            predecessors[pred_id] = machine.OP_SPECS[operations[pred_id]["op"]]["latency"]
        for pred_id in machine.memory_predecessors(program, op_id):
            predecessors[pred_id] = max(predecessors.get(pred_id, 0), 1)
        for pred_id, delay in predecessors.items():
            successors[pred_id].append((op_id, delay))
            dependency_edges[op_id].append((pred_id, delay))

    # Critical-path priority keeps long latency chains moving. Ties favor
    # engines with more ready work per available slot, then source order.
    critical_path = [0] * len(operations)
    for op_id in range(len(operations) - 1, -1, -1):
        latency = machine.OP_SPECS[operations[op_id]["op"]]["latency"]
        critical_path[op_id] = latency + max(
            (critical_path[succ] for succ, _ in successors[op_id]), default=0
        )

    unscheduled = set(range(len(operations)))
    issue_cycle: dict[int, int] = {}
    bundles: list[dict[str, list[int]]] = []
    remaining = len(operations)
    cycle = 0
    while remaining:
        ready = {
            op_id for op_id in unscheduled
            if all(pred_id in issue_cycle and issue_cycle[pred_id] + delay <= cycle
                   for pred_id, delay in dependency_edges[op_id])
        }
        bundle: dict[str, list[int]] = defaultdict(list)
        ready_by_engine: dict[str, int] = defaultdict(int)
        for op_id in ready:
            engine = machine.OP_SPECS[operations[op_id]["op"]]["engine"]
            ready_by_engine[engine] += 1
        ordered = sorted(
            ready,
            key=lambda op_id: (
                -critical_path[op_id],
                -(ready_by_engine[machine.OP_SPECS[operations[op_id]["op"]]["engine"]]
                  / machine.ENGINE_LIMITS[machine.OP_SPECS[operations[op_id]["op"]]["engine"]]),
                op_id,
            ),
        )
        issued = []
        for op_id in ordered:
            engine = machine.OP_SPECS[operations[op_id]["op"]]["engine"]
            if len(bundle[engine]) >= machine.ENGINE_LIMITS[engine]:
                continue
            bundle[engine].append(op_id)
            issued.append(op_id)
        if not issued:
            bundles.append({})
            cycle += 1
            continue

        bundles.append(dict(bundle))
        for op_id in issued:
            unscheduled.remove(op_id)
            issue_cycle[op_id] = cycle
            remaining -= 1
        cycle += 1

    return {"scratch": scratch, "bundles": bundles}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 compiler.py <program.json>", file=sys.stderr)
        return 2

    program = machine.load_program(argv[0])
    compilation = compile_program(program)
    machine.check_compilation(program, compilation)
    json.dump(compilation, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
