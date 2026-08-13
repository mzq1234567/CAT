# Two Cost-Accuracy Issues in the Azure Cost Assessment Tool

**Purpose of this document:** Hand-off context for ChatGPT (or any engineer) to review two related
accuracy problems we discovered while running the tool against real Azure subscriptions. Both come
down to the **same root question — "what represents this environment's real cost right now?"** — and
both currently produce misleading numbers.

Nothing here has been changed in code yet; this is a design/decision write-up. The tool's non-
negotiable principles (below) must be respected by any fix.

---

## 0. Background you need to understand this

**What the tool is:** A client-facing, self-service Azure cost-assessment web app. A customer connects
their Azure subscription; the tool reads inventory, utilisation metrics, and billing, then produces
cost-optimisation recommendations (Reserved Instances, Azure Hybrid Benefit, right-sizing, orphaned
resources, etc.) with a rupee/dollar saving on each.

**Non-negotiable principles (must not be violated by any fix):**
- Never fabricate a number.
- Never present a saving the customer cannot actually realise.
- **No single finding's saving may exceed the customer's measured spend** (a hard cap already enforces
  this in code).
- A smaller honest number is better than a large theoretical one.
- If the required data isn't available, show "unavailable" / a clearly-labelled estimate — never a guess
  dressed up as fact.

**How savings are grounded today:** Each finding's saving is anchored to the resource's **actual billed
cost** from Azure Cost Management (`cost_map`), and capped at that cost. The billing basis used is the
**last complete calendar month** (per resource), falling back to **month-to-date (MTD)** when there is no
complete month yet (a brand-new/recently-migrated subscription).

**How Azure Hybrid Benefit (AHB) works:** Azure charges a **Windows Server licence fee for every hour a
VM is powered on**, on top of the compute cost. AHB removes that licence fee **if the customer already
owns eligible Windows Server licences** (with Software Assurance / qualifying subscription licences). So
AHB is:
- **conditional** (only real if the customer owns the licences), and
- **proportional to how many hours the VM actually runs** (the licence is billed hourly, like compute).

The tool computes, per VM, from the **live Azure Retail Prices API**:
- `windows_price` = full Windows VM monthly list price
- `compute_only_price` = same VM as Linux (no licence)
- `licence_charge` = windows_price − compute_only_price  (the full monthly licence, if run 24×7)
- `licence_fraction` = licence_charge / windows_price  (≈ 0.49 for D-series)

Current AHB saving formula: **`actual_billed_cost × licence_fraction`**, capped so it can never exceed
the VM's own cost.

---

## ISSUE 1 — AHB savings are wrong on partial-billing subscriptions (understated), and were previously wrong the other way (overstated)

### Symptom (real data, assessment #72, currency INR)
- Subscription is on **partial billing** (`partial_billing = true`, less than one complete billing month
  — recently created/migrated).
- AHB finding total: **₹735.55/mo** across 11 eligible VMs (4 more excluded because they'd billed
  nothing yet).

Example VM (CRA-VM, Standard_D4s_v3, East US), exactly as stored:

| Field | Value |
|---|---|
| `windows_price` (full Windows list/mo) | ₹25,909 |
| `compute_only_price` (Linux list/mo) | ₹13,230 |
| `licence_charge` (full monthly licence) | **₹12,679** |
| `licence_fraction` | 0.4894 |
| `actual_monthly_cost` (billed **so far** this partial month) | **₹239.61** |
| **AHB saving shown** (`239.61 × 0.4894`) | **₹117.26/mo** |

The math is internally consistent and grounded — **but ₹239.61 is not a month.** It's a few hours/days
of billing on a new subscription. So the AHB saving is ~**100× too low**: the VM's real monthly Windows
licence is ₹12,679, but the tool shows ₹117 because it grounded in a billing fragment.

### The history (why it's like this)
This is the **flip side of a bug we already fixed.** Earlier, on the same kind of partial subscription,
AHB was priced at the **full retail licence** for every VM (`Σ licence_charge`, i.e. assuming every VM
runs 24×7). On a fleet that produced **₹74,034/mo = ₹888,410/yr** of "savings" against a subscription
spending only ~₹3,700/mo — a ~20× overstatement (the "₹888K bug"). We removed that and grounded AHB in
actual billed cost. The pendulum swung to the opposite error: now it **understates** on partial billing.

So there are three numbers, and only one is right:

| Method | Per CRA-VM | Fleet /mo | /yr | Verdict |
|---|---|---|---|---|
| Full retail licence (old bug) — `Σ licence_charge`, assumes 24×7 | ₹12,679 | ₹74,034 | ₹888,410 | **Over** (fantasy 24×7) |
| Raw partial billing (now) — `actual_fragment × fraction` | ₹117 | ₹735 | ₹8,826 | **Under** (fragment) |
| **Actual runtime × licence** (proposed) | ~₹234* | ~₹1,470* | ~₹17,640* | **Correct-ish, under spend** |

\* estimated using the observed run-rate (this subscription has ~half a month billed, factor ≈ 2×).

### Root cause
AHB should scale with **how many hours the VM actually runs** (the licence is billed hourly). Grounding
in a partial billing fragment under-counts the hours; assuming 24×7 over-counts them. Neither reflects
real runtime.

### Why it's hard here
The subscription has **no complete billing month**, so the bill can't tell us the monthly hours directly.

### Proposed approach — measure how much each VM actually runs, without needing a full billing month
"Running" here means **powered-on hours (uptime)**, **NOT CPU utilisation.** A VM left on 24×7 at 2% CPU
still pays 100% of its Windows licence, so AHB must scale by uptime, not busy-ness. (This is a different
signal from the right-sizing detectors, which correctly use CPU.)

Formula: **AHB monthly saving = per-hour Windows licence × monthly running hours.**

Sources for "monthly running hours" (most → least authoritative), none needing a complete billing month:
1. **Actual billed usage hours** from Cost Management — the VM's compute meter reports *quantity (hours)*,
   not just cost. Microsoft's own record of exactly how long it ran. Best, but partial on a new sub.
2. **Azure Monitor "VM Availability" metric** (`VmAvailabilityMetric`) — 1 while up, 0 while down;
   averages to exact uptime % over any window. Purpose-built; not queried today.
3. **Metric coverage (already collected)** — a VM emits CPU/memory metrics only while allocated, so the
   fraction of the window with metric data ≈ uptime %. The tool already fetches `cpu_datapoints` +
   window; the signal is already in hand.
4. **Activity Log** `start`/`deallocate`/`stop` events (~90-day retention) — reconstruct the exact
   on/off timeline.

Then: `monthly running hours = uptime% × 730`, and `AHB = (windows_hourly − linux_hourly) × running_hours`.

### Why this stays safe (can't recreate ₹888K)
Because it's still `licence_rate × running_hours`, and the licence is always a **fraction** (~49%) of the
VM's per-hour cost, each VM's AHB is always **less than that VM's own cost** → the fleet total can't
exceed compute spend, let alone total spend. The ₹888K only existed under the false "runs 24×7"
assumption. The existing hard cap (no finding exceeds measured spend) is a second line of defence.

### Caveats (must be honoured)
- Measure uptime against the VM's **observed lifetime**, not a fixed 30 days (a 10-day-old VM up the
  whole 10 days = 100% uptime, not 33%).
- It's a **projection** from a short window → label it a run-rate **estimate**, re-confirm after a full
  billing month.
- Very short window (VM 1–2 days old) → weak signal → low confidence or "confirm after a full month",
  not a precise figure.
- AHB stays **conditional** on licence ownership regardless — always presented as *potential*.

---

## ISSUE 2 — The cost basis ("last complete month") is stale/unrepresentative for bursty environments

### Symptom (real data, "LABS – Subscription", currency INR, today = Aug 12)
Azure Cost Analysis, same subscription, two adjacent periods:

- **July 2026 (full month): ₹6,507** (21 resources)
- **Aug 1–12 (12 days so far): ₹31,294** (24 resources)

Per-resource comparison (same resources both months):

| Resource | Type | July (full mo) | Aug 1–12 (12 days) |
|---|---|---|---|
| ms-training-db | SQL server | ₹2,901 | ₹13,628 |
| mstraining-staging | SQL server | ₹1,976 | ₹9,276 |
| turn-vm | Virtual machine | ₹613 | ₹2,837 |
| asp-portalrg-8a95 | App Service plan | ₹467 | ₹2,192 |
| fd-techplusskills | Front Door | ₹256 | ₹1,228 |
| **Total** | | **₹6,507** | **₹31,294** |

Daily rate: **July ≈ ₹210/day, August ≈ ₹2,608/day → ~12× higher, uniformly across ~every resource.**
Projected August month ≈ **₹80,844** vs July's ₹6,507.

### Diagnosis (what it is — and isn't)
- **Same resources** in both months (not new resources).
- **No one-time purchase** (no reservation buy, no one-off charge) — checked the line items; it's all
  recurring usage (SQL, VM, App Service, Front Door).
- The increase is **uniform (~12×) across everything**, which means the whole environment "woke up." This
  is a **training/labs subscription** (names: ms-training, techplusskills, LABS) — usage is **bursty**:
  **quiet in July (labs idle), active in August (training running).** The SQL servers and VM are simply
  switched on and working far more in August.

### Why this breaks the tool
The tool grounds everything in the **last complete month = July = the quiet one**. So it:
- reports "current spend ≈ ₹6.5k/mo" when the customer is actually running at ~**₹80k/month** pace, and
- **understates every saving (AHB, right-sizing, etc.) by roughly 12×**, because it prices them off the
  quiet month.

There is **no single "true" monthly cost** for a bursty environment — July (quiet) and August (busy) are
both real. Trusting one arbitrary month is the flaw.

### Proposed approach
The tool **already pulls 6 months of history** and **already computes, per resource, a mean cost and a
"stable vs erratic" flag** (coefficient of variation) — it just doesn't *use* them for grounding. So:
1. For **erratic/bursty** resources, ground in a **representative figure** — a recent multi-month average
   or the current run-rate — **not one quiet month.**
2. **Flag the variability** to the client ("spend varies ~12× month-to-month; treat as a range"), rather
   than hiding it behind one number.
3. Continue to **strip one-time purchases** (Cost Management tags each charge `Usage` vs `Purchase`;
   only recurring `Usage` should drive savings). None here, but the guard should exist in general.

---

## THE COMMON THEME (why these are really one problem)

Both issues reduce to: **base the numbers on how much things have actually been running recently — not on
one arbitrary or incomplete month.**

- AHB (Issue 1): use the VM's **actual running hours** (uptime) instead of a billing fragment or a 24×7
  assumption.
- Cost basis (Issue 2): use a **representative recent cost** (average / run-rate, one-time charges
  stripped) instead of a single stale month.

A single mechanism — "estimate each resource's representative recent run-rate, grounded in real usage,
labelled as an estimate, with one-time charges removed and variability flagged" — fixes both.

---

## OPEN QUESTIONS FOR REVIEW

1. **AHB uptime source:** prefer billed usage-hours (authoritative but partial), the VM Availability
   metric (clean, needs a new query), or metric-coverage (already collected, slightly indirect)? Or a
   fallback chain of all three?
2. **Cost basis for bursty environments:** trailing multi-month average, or current-month run-rate, or
   the max of representative months? For a training lab that's idle half the year, which is "fair" to the
   client?
3. **One-time charge handling:** where exactly to split `Usage` vs `Purchase`, and whether reservations
   already owned should be surfaced separately.
4. **Labelling:** how prominently to flag "estimate / partial billing / high variability" so the client
   is never misled, without burying the headline number.
5. **Interaction with the hard cap:** the "no finding exceeds measured spend" cap uses *measured* spend —
   if we move to a run-rate basis, the cap should use the same representative spend figure, not the stale
   month, so it stays consistent.

---

## KEY FILES (for anyone implementing)
- `backend/app/services/findings.py` — `detect_windows_ahb` (AHB), `detect_vm_utilisation_findings`
  (right-sizing), `_finding` (grounding + caps).
- `backend/app/services/cost_management.py` — `get_cost_map_and_consistency` (last-month basis + MTD
  fallback), `cost_consistency` (per-resource mean + stable/erratic flag — already computed, not yet used
  for grounding).
- `backend/app/services/assessment.py` — orchestration; `_gather_spend_baseline` (run-rate spend card),
  passes `measured_monthly_spend` into the engine for the hard cap.
- `backend/app/services/pricing.py` — live Retail Prices (Windows vs Linux per-VM prices for the licence
  delta).
