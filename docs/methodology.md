# Methodology

## 1. Line rating

The corridor is rated with the IEEE 738-2012 steady-state heat balance. At
thermal equilibrium the ohmic heating plus absorbed sunlight equals what
convection and radiation carry away:

$$I^2 R(T_c) + q_s = q_c(T_c) + q_r(T_c)$$

Fixing $T_c$ at the design temperature and solving for $I$ gives the **ampacity**.
Fixing $I$ at the current the network is actually carrying and solving for $T_c$
gives the **conductor temperature** — the check that tells you whether a rating
was safe rather than merely permitted. Both directions are implemented
([`dlr.py`](../src/corridor_sim/dlr.py)); the inverse solve is a bisection on
$[T_{air}, 150\ ^\circ\mathrm{C}]$ to 0.1 °C.

Convection takes the larger of the two forced-convection correlations and the
natural-convection floor, so still air is handled without a discontinuity. The
wind angle of attack is folded onto [0°, 90°]: wind along the line cools far
less than wind across it, and on a real corridor that difference is worth more
than a few degrees of ambient.

### Three rating modes

| Mode | Air temperature | Irradiance | Wind speed | Wind angle |
|---|---|---|---|---|
| 0 static | reference | reference | reference | reference |
| 1 ambient-adjusted | measured | measured | fixed low value | perpendicular |
| 2 full weather | measured | measured | measured at conductor height | per zone |

Mode 0 is not a separate model. It is the *same* heat balance evaluated at the
conditions the static rating is declared for — a hot day, light perpendicular
wind, full sun. `dlr.calibration()` asserts this: the model reproduces the
declared static rating to within 0.4 %. That matters, because otherwise any
apparent DLR uplift could just be two models disagreeing.

Mode 1 is the conservative deployment: it needs only ambient temperature, and
holds wind at a low fixed value. Mode 2 needs a wind measurement or forecast and
is worth substantially more.

### Wind at conductor height

The reference wind field sits at 100 m; the conductor sits at 15 m. Speed is
brought down by a neutral-stability log law over a roughness length, and the
geometry is validated — an effective height at or below the roughness length
raises rather than silently returning nonsense.

The corridor is split into two rating zones with different mean bearings, so the
same wind gives each a different angle of attack. Each zone is rated on its own
weather; the governing limit for the export path is the lower of the two,
because it is one series thermal path.

## 2. Control

Three actuators share the corridor, deliberately separated by role rather than
all reacting to the same voltage at the same speed:

| Actuator | Role | Deadband | Move budget |
|---|---|---|---|
| MV shunt reactor | fine, primary | ±0.18 kV | 4 steps/interval |
| On-load tap changer | coarse, backup | ±0.50 kV | 3 taps/interval |
| Plant reactive droop | continuous | ±0.010 pu | — |

Giving the reactor and the tap changer the same deadband on the same bus
produces a limit cycle: both see the same error, both act, and they fight. Here
the reactor acts first, the network is re-solved, and only then is the tap
changer tested against the voltage that remains.

Both switched actuators freeze after reversing direction within one interval. An
actuator that has reversed has bracketed its setpoint, and further movement is
hunting, not control. A move-rate budget additionally caps how many operations a
15-minute interval can physically contain.

### Reactive droop, and why the loop is built the way it is

The droop characteristic absorbs above the deadband and injects below it, with
the full reactive range spanning a 4 % voltage error and saturating at the
plant's capability.

Two properties of the iteration are load-bearing:

1. **Re-solve after every reactive update.** If the network is only re-solved
   when a tap moves, then once the taps settle the measured voltage is
   byte-identical to the previous iteration, the stability test passes on that
   identity, and the loop exits reporting a reactive power the network never
   saw. Every downstream consumer — recorded flows, the constraint scan, the
   curtailment search — then reads a state that was never solved.

2. **A settled iteration is not a tracked setpoint.** Under-relaxation can drive
   the increment below tolerance while a unit is still far from its reference.
   Convergence therefore requires *both* a settled voltage *and* a closed
   residual, and the residual is written to every row
   (`q_tracking_error_mvar`) so the claim is checkable rather than trusted.

Each unit carries its own under-relaxation factor that halves whenever its step
reverses. The droop is steep relative to this corridor's dV/dQ, so a fixed
damping factor sits near the stability boundary and the margin moves with
loading; halving on reversal squeezes an oscillating unit onto its fixed point.

At extreme loading the droop has no fixed point at all — the gain far exceeds
the network's sensitivity, which is a genuine property of a weak corridor, not a
numerical artefact. The loop therefore **backtracks**: a step the power flow
cannot follow is undone, the damping is reduced, and the iteration resumes from
the last solved state. A timestep always ends on a solved network, so
curtailment can still act on it, and `reg_converged` records honestly that the
droop did not settle.

### Solving

Newton-Raphson, warm-started from the previous interval. A cold start at high
output can diverge even though a solution exists, because the corridor's
reactive absorption is not yet being met locally; the entry solve therefore
seeds the droop units at their capability in the supporting direction, and falls
back to a continuation that ramps injection in from a level that does converge.
The final solve is always at full injection, so the physics is unchanged. The
deep path is deliberately disabled inside the control and curtailment loops,
where a cheap failure is informative.

## 3. Constraint hierarchy

| Level | Constraint | Actionable |
|---|---|---|
| L1 | Over-voltage on any 110 kV bus | yes |
| L2 | Under-voltage on any 110 kV bus | **no** |
| L3 | Thermal, split by asset owner | yes |
| L4 | Export cap at the interface | yes |

L2 is recorded but never curtailed for: reducing active power *deepens* an
under-voltage across an inductive corridor, so curtailment is the wrong
instrument. The important consequence is that **all four levels are evaluated on
every scan**. Returning the first violated level would let a non-actionable
under-voltage hide a coincident thermal overload — the scan would report
"nothing to do" during exactly the hours that most need attention.

L3 is scanned in three ownership groups — corridor lines, distribution
transformers, plant assets — each with its own worst element and margin. Pooling
them into one number is how a plant-owned step-up transformer ends up setting a
published network hosting capacity while the corridor itself has headroom.

Every curtailed megawatt-hour is attributed to exactly one cause
(`curtail_cause`), so the energy adds up.

## 4. Curtailment

Curtailment is parametrised by one scalar: total megawatts removed, allocated
pro rata over the entry dispatch. Every actionable level is relieved
monotonically by injecting less, so the problem is one-dimensional and monotone.

The search brackets the feasibility boundary, then interpolates on a signed
normalised margin with regula falsi and an Illinois guard, and **applies the
smallest cut that clears** — not the last cut tried. The residual bracket width
is reported (`curtail_residual_mw`), so the remaining discretisation error is a
number in the output rather than an unknown.

Marching down in fixed steps and banking whatever was taken answers "the first
grid point past the boundary", which systematically overstates the curtailment a
constraint requires — and the overshoot is indistinguishable from a real
requirement once it is written to a results file.

Two properties the search depends on:

* **Trials evaluate what will be applied.** The droop is re-settled inside every
  trial. Freezing reactive power during the search and re-settling once
  afterwards means searching over one function and reporting another; the
  re-settle can push the result back over the limit the search just cleared.
* **Trials are path-independent.** Each restarts from the entry reactive power,
  so a trial is a pure function of the cut and the bracket stays meaningful.

A first guess is taken from the linear structure of the violation — corridor
current and interface export both scale with injection — which keeps the search
to a handful of trials rather than a doubling ladder from zero.

## 5. Storage

The battery is a market participant, not a corridor congestion device, and its
reach is asymmetric in both directions. It connects near the receiving end, so
it cannot relieve a thermal overload on the sections upstream of its tap — only
curtailment can. But it shares the final section into the interface with
generation, so discharging there competes with export for that segment; because
the battery is not curtailable, the plants are cut instead. In the 30-day
results this makes curtailment *worse* under a static rating, 20.9 % against
23.2 %. Grid charging additionally depresses the interface voltage. Both are
consequences of siting and charging strategy, and both are configurable rather
than assumed.

Dispatch runs in two phases: an intent set before the network is solved, and a
reconciliation against the export headroom the solved network actually leaves.
Generation has priority, so a delivery that would breach the cap is clamped and
the shortfall is recorded rather than absorbed.

State of charge integrates on realised power only. On a step that did not
converge nothing is integrated — there is no energy bookkeeping for power that
was never delivered. When a limit clamps the result, realised power is backed
out of the actual state-of-charge change so power and energy stay consistent.

Holding a reserve and delivering energy compete for the same asset. Every
interval records the reserve still available and whether it fell short, so the
conflict is a counted result rather than a hidden assumption.

## 6. What is deliberately not modelled

* No contingency (N-1) analysis; the corridor is studied intact.
* No protection, stability or electromagnetic transient behaviour — the study is
  quasi-static, and 15-minute steps say nothing about dynamics.
* A single weather point represents the whole corridor, and each rating zone
  carries one mean bearing. Real span-by-span bearings spread around that mean,
  so a per-span rating would be lower than a zone-mean rating.
* Sag and clearance are not computed. The design conductor temperature stands in
  for the clearance limit that governs a real line.
* No sub-hourly ramping constraints on the plants.
