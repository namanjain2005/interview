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

    operations = program["operations"]

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

    # Bottom-level priority measures the remaining dependency delay from each
    # operation. Edge weights matter: data edges wait for producer latency,
    # while memory-order edges only require a later issue cycle. Ties favor
    # crowded engines, then source order.
    critical_path = [0] * len(operations)
    for op_id in range(len(operations) - 1, -1, -1):
        own_latency = machine.OP_SPECS[operations[op_id]["op"]]["latency"]
        critical_path[op_id] = max(
            own_latency,
            max(
                (delay + critical_path[succ] for succ, delay in successors[op_id]),
                default=0,
            ),
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

    scratch = _allocate_scratch(program, issue_cycle)
    return {"scratch": scratch, "bundles": bundles}


def _allocate_scratch(program: dict, issue_cycle: dict[int, int]) -> dict[str, int]:
    """Reuse best-fit free blocks as scheduled live intervals expire."""
    operations = program["operations"]
    lifetimes: dict[str, list[int]] = {}
    for operation in operations:
        if "dest" in operation:
            ready = (
                issue_cycle[operation["id"]]
                + machine.OP_SPECS[operation["op"]]["latency"]
            )
            lifetimes[operation["dest"]] = [ready, ready]
    for operation in operations:
        for arg in operation.get("args", []):
            lifetimes[arg][1] = max(lifetimes[arg][1], issue_cycle[operation["id"]])

    kinds = machine.result_kinds(program)
    values = sorted(lifetimes, key=lambda name: (lifetimes[name][0], -lifetimes[name][1]))

    scratch: dict[str, int] = {}
    free_blocks: list[tuple[int, int]] = []
    active: list[tuple[int, str, int, int]] = []
    cursor = 0

    def add_free(start: int, end: int) -> None:
        if start < end:
            free_blocks.append((start, end))
            free_blocks.sort()
            merged: list[tuple[int, int]] = []

            for block_start, block_end in free_blocks:
                if merged and block_start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], block_end))
                else:
                    merged.append((block_start, block_end))
            free_blocks[:] = merged

    def trim_top() -> None:
        nonlocal cursor
        while free_blocks and free_blocks[-1][1] == cursor:
            cursor = free_blocks.pop()[0]

    for name in values:
        start, end = lifetimes[name]
        still_active = []
        for active_end, active_name, base, width in active:
            if active_end < start:
                add_free(base, base + width)
            else:
                still_active.append((active_end, active_name, base, width))
        active = still_active
        trim_top()

        width = machine.VLEN if kinds[name] == "vector" else 1
        alignment = machine.VLEN if kinds[name] == "vector" else 1
        candidates = []
        for index, (block_start, block_end) in enumerate(free_blocks):
            base = machine.align_up(block_start, alignment)
            if base + width <= block_end:
                candidates.append((block_end - block_start - width, block_start, index, base))

        if candidates:
            _waste, block_start, block_index, base = min(candidates)
            block_start, block_end = free_blocks.pop(block_index)
            if block_start < base:
                add_free(block_start, base)
            if base + width < block_end:
                add_free(base + width, block_end)
        else:
            base = machine.align_up(cursor, alignment)
            if cursor < base:
                add_free(cursor, base)
            cursor = base + width
            if cursor > machine.SCRATCH_WORDS:
                raise machine.CompileError(
                    f"program requires {cursor} scratch words, limit is {machine.SCRATCH_WORDS}"
                )

        scratch[name] = base
        active.append((end, name, base, width))

    return scratch


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
