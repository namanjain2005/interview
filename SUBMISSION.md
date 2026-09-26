# Luminal Compiler Take Home Submission

## Implementation

The compiler schedules operations with a dependency-aware list scheduler. It
builds data-dependency edges weighted by the producer's latency and memory-order
edges weighted by one cycle. Memory edges come from the machine's overlap rules,
so disjoint accesses can reorder while overlapping accesses retain their
required order.

Each cycle, the scheduler selects ready operations by their remaining weighted
dependency path. An operation's own latency is a lower bound on that priority,
including for leaf operations. When priorities tie, it favors the engine with
more ready work per available slot and then uses operation ID for a stable
ordering. The scheduler enforces each engine's issue limit and only issues an
operation after all dependencies have reached their required release cycle.

After scheduling, scratch allocation derives each value's live interval from
its write cycle (issue cycle plus result latency) through its final consumer's
issue cycle. Intervals are inclusive, so an address is reusable only when the
old value's final read is strictly before the new value's write. The allocator
uses best-fit free blocks: it splits ranges when allocating, coalesces adjacent
ranges when releasing, and preserves vector alignment. This allows released
middle ranges to be reused, including reuse of scalar ranges within previous
vector allocations when their lifetimes do not overlap.

## Public Results

Measured with `python3 score.py` against the provided serial baseline:

| Program | Cycles | Baseline cycles | Speedup | Scratch words | Scratch reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| scalar_pipeline | 14 | 18 | 1.286x | 6 | 2.500x |
| scalar_dual_chain | 8 | 12 | 1.500x | 3 | 3.000x |
| vector_axpy | 10 | 14 | 1.400x | 32 | 2.281x |
| vector_bitmix | 9 | 16 | 1.778x | 40 | 2.225x |
| mixed_broadcast | 9 | 11 | 1.222x | 24 | 1.750x |
| parallel_memory | 13 | 21 | 1.615x | 40 | 3.200x |
| scalar_selects | 9 | 20 | 2.222x | 6 | 2.667x |
| vector_reduction | 12 | 19 | 1.583x | 40 | 3.425x |
| **Geometric mean** | — | — | **1.550x** | — | **2.577x** |

**Public combined score: 1.999x.** This is the square root of cycle speedup
geometric mean multiplied by scratch reduction geometric mean.

## Validation and Tradeoffs

- `python3 -m unittest -v`: all 11 tests passed, including compiler validation
  and execution on every public program and case.
- `python3 score.py`: completed successfully with the results above.
- `git diff --check`: clean.

The scheduling heuristic is greedy rather than globally optimal. Critical-path
priorities and per-engine ready-work pressure are intended to make good local
choices while keeping the scheduler simple. The scratch allocator is also a
heuristic: best fit and coalescing reuse middle blocks but do not guarantee the
minimum possible footprint for every interval pattern. The measured score is
for the eight public programs; hidden-program performance has not been
measured.

## Time and Tool Assistance

Time spent: Approximately 5 hours.

Tool assistance: OpenAI Codex assisted with implementation and review. The
changes were checked with the provided unit tests, simulator-backed public
program correctness checks, and public benchmark score script.
