# Lucen: Correctness-First Parallelization of Ordinary Python via Comment Pragmas

**The Lucen Authors**

## Abstract

Parallelizing existing Python code is disproportionately hard: the reference
interpreter serializes bytecode execution under a global lock, and the available
tools ask the programmer to restructure a program around explicit workers,
pools, and serialization. We present Lucen, a source-to-source compiler that
parallelizes ordinary Python `for` loops marked with two comment pragmas.
Lucen is declarative: the programmer states *where* parallelism is permitted,
and the compiler decides *whether* and *how* to realize it. Its defining
constraint is a single guarantee with no tier and no opt-out, that a parallel
run is bit-identical to the same source executed as plain sequential Python,
including floating-point reduction bits and container insertion order. We
describe the design that makes this guarantee hold by construction: a
privatize-and-commit execution model with a join-time write-set audit, a
reduction fold that preserves sequential association order, and a
level-synchronous scheduler for recognized dependency shapes. We show that
backend selection should be decided by the data shape of a loop rather than by
the interpreter, a conclusion driven by the observation that threads lose to
processes on shared-container workloads even on a free-threaded interpreter with
no global lock. We report a cross-version evaluation showing three to four times
speedups on compute-bound loops with bit-identical output, and we describe a
verification methodology combining differential property testing, whole-program
fuzzing, and machine-checked formal specifications of the two core concurrency
protocols.

## 1. Introduction

Two facts shape the practice of parallelizing Python. First, the reference
implementation, CPython, has historically executed bytecode under a global
interpreter lock, so thread-level parallelism does not accelerate CPU-bound pure
Python. Second, the established remedies are imperative frameworks: the standard
library's process pools, and third-party task and dataflow systems, all require
the programmer to factor the computation into worker functions, manage a pool
lifecycle, and make the data cross a serialization boundary. The cost of that
restructuring is often high enough that a loop which is embarrassingly parallel
in principle remains sequential in practice.

Lucen targets that gap. A programmer marks a loop with two comment lines and
activates an import hook once; the compiler parallelizes the loop if, and only
if, it can prove the transformation safe and estimate it profitable. Because the
markers are comments, a file with Lucen absent, deactivated, or uninstalled
runs identically to one where the markers were never written. We call this the
Comment Invariant, and it makes adoption reversible and low-risk: the worst case
of marking a loop is the program the programmer already had.

The contribution of Lucen is not raw speed; hand-written process-pool code
achieves comparable speedups. The contribution is delivering that speed under a
guarantee that hand-written code does not offer and, in our measurements, often
violates: the result is bit-identical to sequential execution. This paper
describes how that guarantee is made to hold by construction, verified
continuously, and reconciled with useful performance.

## 2. The comment-pragma model

A marked block is a single `for` loop over a sized iterable, delimited by
`# LUCEN START` and `# LUCEN END`. Optional clauses on the start line tune
or assert; every clause trades a compiler-held proof for a programmer-held
assertion, or exactness for speed, and never silently changes the result. The
compiler is a five-stage pipeline. A tokenizer-based scanner recovers the marked
regions and parses clauses. An AST rewriter classifies every name in the block
(loop-local, read-only, reduction accumulator, indexed write, cross-iteration
read) and recognizes dependency shapes analytically. A selector routes each
block to a backend or to sequential execution, with a recorded reason. A code
generator emits two functions per block: a chunk function for workers and a
sequential twin that is also the fallback path, so the sequential behavior is
the programmer's original loop by construction. A dispatcher executes chunks,
audits and commits their results, and folds reductions.

Anything the compiler cannot prove safe runs as the sequential Python the
programmer wrote, and the reason is recorded in a structured fallback report
rather than raised. This quiet-fallback default is what makes the never-
disruptive property hold; hard-failure and per-block strict modes are available
for continuous integration, but the safe default is to keep running.

## 3. Correctness by construction

### 3.1 Privatize and commit

The unit of dispatch is a chunk, a contiguous sub-range of the iteration space.
Each chunk writes into a private slab, never into the shared container, so no
two workers mutate shared state during execution. At the join point, a write-set
audit checks that the chunks wrote disjoint locations, and the slabs commit to
the shared container in chunk order, and within a chunk in iteration order.
Because chunks are contiguous and commit in order, the global order of applied
writes is exactly the sequential order. Two consequences follow that per-write
concurrent schemes do not provide: container insertion order is identical to
sequential, and a mid-block error leaves the container in exactly the
sequential-prefix state, because chunks below the failure commit and the rest
are discarded.

### 3.2 Reductions preserve association order

Floating-point addition is not associative, so a tree-combine of per-worker
partial sums produces different bits than the left-to-right fold that sequential
Python performs. Lucen therefore folds per-element contributions in
sequential order by default, at a cost proportional to the number of chunks
rather than the number of elements. A parallel reduction is bit-identical to
sequential on every backend. Re-associating modes exist but are opt-in and
declared, so choosing different bits is a conscious act.

### 3.3 Level-synchronous scheduling for recognized shapes

Some loops carry real cross-iteration dependencies of a recognized shape, for
example an index depending on `i div c` for a constant `c > 1`. Rather than a
blocking work-stealing scheduler, Lucen executes such a loop in levels, where
level `k` is the set of indices at dependency-depth `k`; levels run in order
with a barrier and commit between them, and within a level indices run
concurrently. A dependency is always in a strictly earlier, already-committed
level, so every read is of a committed value and no task waits on another task.
Deadlock-freedom is therefore a property of the level structure rather than an
induction over blocking tasks.

### 3.4 Machine-checked specifications

The two protocols above are specified in TLA+ and checked by the TLC model
checker. The privatize-and-commit specification proves that the committed array
equals the sequential result for every interleaving of chunk execution. The
wavefront specification proves dependency-safety as a safety invariant and
termination as a liveness property. Each protocol is also encoded as an
executable bounded model checker that exhaustively explores the same state space
and runs in continuous integration without external tooling.

## 4. Interpreter-independent backend routing

The intuitive expectation is that removing the global lock makes threads the
fast path. Measurement on a free-threaded build contradicts this for the loop
shapes Lucen parallelizes. Reading a shared container contends on its
reference count, and writing a shared list serializes on that list's per-object
mutation lock; on a shared-container workload the thread backend measured well
below sequential even at high thread counts. The process backend has neither
problem, because each worker owns its data. Lucen therefore routes maps and
reductions to the process backend on both a locked and a free-threaded build,
and reserves the thread backend for the cases where a process copy would lose a
by-reference effect or ship an entire container per chunk. The cost model costs
the backend that will run, not the interpreter it runs on. This is a design
input rather than a limitation to be surprised by: Lucen routes around the
free-threaded contention ceiling rather than promising to remove it.

## 5. The profitability gate

A trivial loop over a small input loses to dispatch overhead, and parallelizing
it makes the program slower while keeping the result correct. Lucen screens
this statically and then measures it: a runtime probe executes the first chunk
as a sequential prefix, so the probe is real work that also serves as the timing
measurement, never wasted or repeated. If the projected parallel time beats
sequential, the remaining chunks dispatch; otherwise the loop finishes
sequentially and the decision is reported. The gate is a bounded-cost estimator,
not a promise of speedup; a wrong prediction costs at most about one chunk of
suboptimal scheduling and never changes the result.

## 6. Implementation

Lucen is implemented in Python with an optional native core written in Rust
and exposed through the PyO3 bindings. The native core accelerates two
orchestration primitives, the write-set audit and the reduction fold, the latter
operating by reference through the interpreter's own numeric protocol so that
operand semantics, including unbounded integers and user-defined operators, are
preserved exactly. Every native operation has an identical-semantics
pure-Python fallback, and the library is correct with the extension absent. The
native core ships as a single stable-ABI wheel that loads across locked builds
from one binary; free-threaded and alternative interpreters install a
pure-Python wheel and run the fallback.

## 7. Evaluation

On a twelve-core machine, compute-bound loops parallelize by three to four times
over their own sequential execution: a heavy map by 3.8 times, a heavy reduction
by 3.2 times, and a nested compute loop by 3.1 times, with output bit-identical
to sequential in every case. Loops too light to benefit are kept sequential by
the gate at effectively no overhead. Across seven interpreters, eight workloads,
and every execution pathway, each parallel result is bit-identical to plain
Python. In the same measurement, hand-written process-pool code generated to an
expert standard achieves comparable speedups but produces different floating-
point bits than sequential in its parallel reductions on every interpreter
tested, which quantifies the guarantee Lucen adds over the imperative
alternative.

Correctness is not established by benchmarks but by a layered verification
methodology. Differential property testing generates loop bodies, input data,
and structural parameters and asserts bit-identity against sequential execution,
comparing floating-point results bit for bit. Whole-program fuzzing generates
complete modules with several marked blocks of varied shape and asserts the
rewritten module matches plain execution. Front-end fuzzing drives the scanner
and clause parser with adversarial input and requires that no exception escapes
other than the sanctioned clause error. The native core is checked for undefined
behavior under an interpreter-level tool, and the two core protocols are
model-checked as described above.

## 8. Related work

Lucen sits among three families of prior work. Imperative parallel runtimes,
including the standard library's process pools and third-party task and dataflow
frameworks, provide the execution machinery Lucen also uses but require the
programmer to restructure the program and manage the parallel form explicitly;
Lucen is declarative over that machinery. Compilation-based accelerators
compile numeric Python to native code, trading generality for speed on a typed
subset; Lucen parallelizes arbitrary loop bodies without compiling them and
treats native body compilation as future work. Directive-based parallelism, in
the tradition of compiler pragmas for parallel loops in other languages,
inspires the comment-pragma surface; Lucen differs in refusing to parallelize
what it cannot prove safe rather than trusting the directive, and in guaranteeing
bit-identity rather than only a correct-under-assumptions result. The
verification methodology draws on established practice in compiler testing, in
particular randomized differential testing of compilers and formal verification
of compiler transformations.

## 9. Limitations and future work

Lucen parallelizes the loop but does not compile the loop body; each
iteration runs interpreted bytecode, so the speedup on a body of pure
interpreter work is bounded by the core count and the interpreter. Compiling a
provably-typed numeric subset of loop bodies to native kernels, behind the same
prove-or-fallback gate, is the principal direction of future work and the
largest remaining performance lever. The correctness guarantee has a documented
boundary: it holds provided helper callables are pure with respect to hidden
state that the analyzer cannot read, and provided objects serialize faithfully;
divergence within that boundary requires code the compiler cannot see inside,
and is inventoried rather than hidden.

## 10. Conclusion

Lucen shows that automatic parallelization of ordinary Python can be
correctness-first without being useless: by proving safety, preserving
sequential order in commits and reductions, routing by data shape rather than by
interpreter, and declining loudly when it cannot win, it delivers the speedups of
hand-written parallel code under a guarantee that hand-written code does not
provide. The result is a tool that a programmer can adopt by writing two
comments, with the assurance that the worst case is the program they already had.

## References

1. Python Software Foundation. *The CPython Global Interpreter Lock*. CPython
   documentation.
2. S. Gross. *PEP 703: Making the Global Interpreter Lock Optional in CPython*.
   Python Enhancement Proposals.
3. OpenMP Architecture Review Board. *OpenMP Application Programming Interface*.
   Specification for directive-based parallelism.
4. X. Yang, Y. Chen, E. Eide, and J. Regehr. *Finding and Understanding Bugs in
   C Compilers*. Programming Language Design and Implementation, 2011.
5. N. P. Lopes, J. Lee, C.-K. Hur, Z. Liu, and J. Regehr. *Alive2: Bounded
   Translation Validation for LLVM*. Programming Language Design and
   Implementation, 2021.
6. L. Lamport. *Specifying Systems: The TLA+ Language and Tools for Hardware and
   Software Engineers*. Addison-Wesley, 2002.
7. D. R. MacIver et al. *Hypothesis: A New Approach to Property-Based Testing*.
   Journal of Open Source Software.
8. The Lucen Authors. *Lucen Technical Specification* and
   *Lucen Benchmark Report*. Project documentation.
