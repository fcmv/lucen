# 0014. Native orchestration folds by reference

## Context

The optional Rust core exists to accelerate the orchestration loops that are hot
at Python level. The reduction fold is one of them. An early native fold copied
the slab's floating-point values across the Python-to-Rust boundary as unboxed
`f64`, summed them in Rust, and returned the result. Measured against CPython's
own C-speed float addition, this was a net loss: marshalling a million boxed
floats across the boundary cost more than the interpreter loop it replaced. The
native fold was, at that point, slower than doing nothing.

## Decision

The native fold operates *by reference* through CPython's own protocol. It walks
the slabs in the exact sequential order (outer element index, inner site order)
and combines adjacent values by calling the same C-level operation the
interpreter calls for the operator: `PyNumber_Add` for `+`, `PyNumber_Multiply`
for `*`, `PyObject_RichCompare` for `min`/`max`, and so on. No operand is
converted or copied. Only the per-element bytecode dispatch is eliminated.

Because it calls the interpreter's own operation, the result is what the
sequential loop would produce for any operand type: float bits, unbounded
integers with no wrapping, and user-defined operators, all preserved exactly.
The write-set audit crossed into the core the same way, as one native call over
all chunks rather than a native method call per index. A native element-wise
slab commit was also built, measured slower than CPython's `zip` plus
specialized list stores, and deliberately kept in Python, with the measurement
recorded at the call site so nobody re-attempts it blind.

## Consequences

- The native fold is faster than the pure-Python fold and semantically identical
  to it, verified by parity tests (bignum folds do not wrap, float folds are
  bit-identical) and by a direct differential against the pure-Python twin.
- The rule for the core is now explicit: an operation earns a place only if it
  is measured faster than its Python twin on representative data and passes the
  parity tests. The two retired attempts (the marshalling fold, the native
  commit) are the evidence that "native" is not automatically faster.
- The not-handled signal from the seam is a distinct sentinel, not `None`, so a
  fold over user objects whose operator legally returns `None` never triggers a
  second fold.
- This moves the conductor, not the orchestra: the loop body itself is still
  interpreted Python. Compiling the body is a separate, larger decision tracked
  in the roadmap.

Spec: technical specification section 5.7.5.
