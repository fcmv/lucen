-------------------------- MODULE PrivatizeCommit --------------------------
\* Privatize-and-commit (ADR 0008). See docs/formal/README.md.
EXTENDS Naturals

CONSTANTS N, K

ASSUME NKAssumption == N \in Nat /\ K \in 1 .. (N - 1)

Iters  == 0 .. (N - 1)
Chunks == {0, 1}
ChunkOf(i) == IF i < K THEN 0 ELSE 1

Bottom == "unset"
Val(i) == (i * i) + 1
SeqArray == [ i \in Iters |-> Val(i) ]

VARIABLES executed, slab, arr, phase
vars == << executed, slab, arr, phase >>

Init ==
  /\ executed = {}
  /\ slab = [ c \in Chunks |-> [ i \in Iters |-> Bottom ] ]
  /\ arr  = [ i \in Iters |-> Bottom ]
  /\ phase = "exec"

Execute(c) ==
  /\ phase = "exec"
  /\ c \notin executed
  /\ executed' = executed \cup {c}
  /\ slab' = [ slab EXCEPT
        ![c] = [ i \in Iters |-> IF ChunkOf(i) = c THEN Val(i) ELSE Bottom ] ]
  /\ UNCHANGED << arr, phase >>

Commit ==
  /\ phase = "exec"
  /\ executed = Chunks
  /\ arr' = [ i \in Iters |-> slab[ChunkOf(i)][i] ]
  /\ phase' = "done"
  /\ UNCHANGED << executed, slab >>

Next == (\E c \in Chunks : Execute(c)) \/ Commit
Spec == Init /\ [][Next]_vars

SequentialEquivalence == (phase = "done") => (arr = SeqArray)

=============================================================================
