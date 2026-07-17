------------------------------ MODULE Wavefront ------------------------------
\* Level-synchronous wavefront (ADR 0009). See docs/formal/README.md.
EXTENDS Naturals

CONSTANTS N, C

ASSUME NCAssumption == N \in Nat /\ C \in 2 .. N

Iters == 0 .. (N - 1)
Dep(i) == i \div C

RECURSIVE LevelOf(_)
LevelOf(i) == IF i = 0 THEN 0 ELSE LevelOf(i \div C) + 1

MaxLevel == LevelOf(N - 1)
LevelIndices(k) == { i \in Iters : LevelOf(i) = k }

VARIABLES committed, curLevel
vars == << committed, curLevel >>

Init ==
  /\ committed = {}
  /\ curLevel = 0

Execute(i) ==
  /\ curLevel <= MaxLevel
  /\ LevelOf(i) = curLevel
  /\ i \notin committed
  /\ (i = 0 \/ Dep(i) \in committed)
  /\ committed' = committed \cup {i}
  /\ UNCHANGED curLevel

Advance ==
  /\ curLevel <= MaxLevel
  /\ LevelIndices(curLevel) \subseteq committed
  /\ curLevel' = curLevel + 1
  /\ UNCHANGED committed

Next == (\E i \in Iters : Execute(i)) \/ Advance
Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

DependencySafety == \A i \in committed : (i = 0) \/ (Dep(i) \in committed)
Termination == <>(curLevel = MaxLevel + 1)

=============================================================================
