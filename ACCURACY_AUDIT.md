# Azure Cost Assessment Tool — Accuracy Audit

**Purpose of this document:** A brutally honest, undefended audit of the cost-assessment
engine's accuracy. It answers, for every optimisation module, whether the numbers we show
are backed by authoritative Microsoft data or by tool-side estimates/assumptions — and where
a better Microsoft API exists, it names it and describes the correct implementation.

**Guiding principles this tool must obey:**
- Never fabricate numbers.
- Never assume discounts.
- Never estimate savings when authoritative Microsoft data is unavailable.
- Never infer optimisation eligibility from guesses.
- Every recommendation must be backed by real Azure data.
- If Microsoft does not expose enough information, show **"Recommendation unavailable"**
  instead of generating a number.

**Context for the reader (ChatGPT):** This is a multi-tenant SaaS web app. Backend is
FastAPI + SQLAlchemy + SQLite; frontend is React + Vite + MUI + Recharts. The tool connects
to a customer's Azure tenant with a *delegated* ARM token (`user_impersonation`) and reads:
- **Azure Resource Graph** (`Microsoft.ResourceGraph/resources`) — inventory & resource state
- **Cost Management** (`Microsoft.CostManagement/query`) — actual billed cost per resource
- **Azure Monitor metrics** (`microsoft.insights/metrics`) — CPU / memory / disk IOPS
- **Azure Advisor** (`Microsoft.Advisor/recommendations`) — Microsoft's own recommendations
- **Consumption reservation recommendations**
  (`Microsoft.Consumption/reservationRecommendations`) — usage-based RI advice
- **Azure Retail Prices API** (`https://prices.azure.com/api/retail/prices`) — public list prices

The core engine lives in `backend/app/services/findings.py`; pricing in `pricing.py`;
reservation parsing in `reservations.py`; hardcoded fallbacks in `estimates.py`.

---

## VERDICT UP FRONT

The tool is **mostly** grounded in real Azure data, but it has **one architectural mistake**
and **a cluster of hardcoded cost estimates** that violate the "no fabrication" rule.

- The single worst problem: **Reserved Instances are sourced from the wrong Microsoft API.**
  The Retail Prices API does not publish reservation prices for most VM SKUs, so the tool
  either fabricated a discount (old behaviour, now removed) or recommends **nothing** for the
  most common enterprise VM series. The authoritative source
  (`Microsoft.Consumption/reservationRecommendations`) is already fetched by the tool but is
  only applied to non-VM resources.
- A recurring pattern: several detectors fall back to a **hardcoded, dated cost table**
  (`estimates.py`) or show **₹0 / $0** when the *actual billed cost is already available* in
  Cost Management data the tool has in memory. This directly violates "show unavailable, don't
  estimate."

Everything else is defensible: it uses real Azure Monitor metrics, real Cost Management
billed costs, real Resource Graph state, and live Retail Prices — but some modules apply
tool-chosen thresholds and target SKUs that should be disclosed as assumptions.

---

## SEVERITY-RANKED FINDINGS

### 🔴 CRITICAL 1 — Reserved Instances use the wrong Microsoft API

**What we do today.** VM RI recommendations are produced by `detect_vm_commitments`, which
computes savings from the **Azure Retail Prices API**: it filters for
`priceType eq 'Reservation'` and derives a discount ratio (reservation price ÷ pay-as-you-go).

**Why this is wrong.** The public Retail Prices API **does not publish reservation prices for
most VM SKUs.** Verified live: `Standard_D2s_v3`, `Standard_D4s_v3`, `Dv3`, `Dsv3` return
**zero** reservation rows in every currency and region tested. M-series returns rows; the
common D/E-series does not. Consequences:
- **Old behaviour:** the tool applied a hardcoded default discount (≈40% for 1yr, ≈60% for
  3yr) when no retail row existed. That was **fabrication** and has been removed.
- **Current behaviour:** with the fabrication removed, the tool now **skips any VM whose RI
  price it can't find** — which means it recommends **no RI at all** for Dsv3/D-series, the
  backbone of most enterprise fleets. That is a half-complete tool.

**The authoritative source we already have but don't use for VMs.**
`Microsoft.Consumption/reservationRecommendations`. Azure simulates the customer's **actual
hourly usage** over a 7/30/60-day lookback at their **real effective prices** and returns:
- exactly which SKUs are worth reserving,
- the recommended quantity,
- the projected **savings** and the reservation cost,
- for precisely the SKUs Microsoft supports reservations on.

The tool **already calls this API and parses it** (`reservations.py` maps
`virtualmachines → ri_vm`), but `commitments_from_recommendations` **deliberately skips the
`ri_vm` bucket**, sending VMs to the retail-estimate path instead. This was an earlier project
decision that is the root cause of the whole RI problem.

**Other authoritative Microsoft sources for RIs:**
- **Azure Advisor** independently emits RI purchase recommendations with dollar savings
  (`Microsoft.Advisor/recommendations`, category = Cost).
- **`Microsoft.Capacity/…/calculatePrice`** returns the exact purchase price of a specific
  reservation (SKU + quantity + term) — the authoritative "what does this reservation cost"
  call.

**Proper enterprise implementation.**
Make **`reservationRecommendations` the source of truth for VM RIs** (real usage-based
savings, real eligibility, only reservable SKUs). Un-skip `ri_vm` in
`commitments_from_recommendations`; retire the retail-estimate discount path for VMs (or keep
it *only* as a supplement when a genuine retail reservation price exists). Optionally
cross-check against Advisor's RI recommendations.

**Honest caveat the client must understand.** Azure's engine only recommends an RI where the
customer's usage actually justifies one. It will **not** flag every production VM. That is
*correct* — recommending an RI for a VM that doesn't run enough hours would itself be a
fabricated saving. So "recommend an RI for every prod VM like the Pricing Calculator does" is
not the right goal; "recommend exactly what Azure's own engine says is worthwhile, with its
real numbers" is.

---

### 🔴 CRITICAL 2 — Snapshot & GRS-vault costs are hardcoded, but the real cost is in hand

**What we do today.**
- `orphaned_snapshots` saving = `diskSizeGB × $0.05/GB` (hardcoded
  `SNAPSHOT_PER_GB_MONTHLY_USD` in `estimates.py`).
- `backup_redundancy` (geo-redundant vault → locally redundant) saving = a **flat $25/month**
  nominal figure (`GRS_VAULT_MONTHLY_USD`), with an admitted confidence of 0.4 and a comment
  that says "nominal; real cost depends on backup volume."

**Why this is wrong.**
- Snapshots bill on **incremental used storage**, not on the parent disk's **provisioned**
  `diskSizeGB`. Using provisioned size **over-states** the cost/saving.
- The **actual billed cost of each snapshot and each vault is already in Cost Management**,
  which the tool pulls into an in-memory `cost_map` keyed by resource ID. We are estimating a
  number we already have the real value for.

**Proper enterprise implementation.**
Ground both in the resource's **actual billed cost** (`cost_map[resource_id]`). For GRS→LRS
the saving is a real fraction of the real vault bill. If Cost Management returns nothing for
that resource, show **"cost unavailable"** — never a hardcoded estimate.

---

### 🟠 HIGH 3 — SQL Server AHB uses a hardcoded licence rate

**What we do today.** `detect_sql_ahb` values the SQL licence at a hardcoded
**$112 / vCore / month** (`SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD`, dated 2026-07-31).

**Why this is inconsistent and risky.** Windows AHB is done *correctly* — it uses each VM's
**live** `Windows price − Linux price` retail delta from the Retail Prices API (so a burstable
B-series VM correctly carries a much smaller licence value than a D-series VM). SQL AHB, by
contrast, **never queries the API**: it applies one static number to every tier
(General Purpose / Business Critical / Hyperscale) regardless of SKU, and it drifts over time.

**Proper enterprise implementation.** Derive the SQL licence delta from the **Retail Prices
API** — the difference between the SQL vCore meter *with* the licence included and the AHB /
base-compute meter — the same pattern Windows AHB uses. If the retail meters don't cleanly
separate the licence, at minimum date-guard the constant and label it explicitly as an
estimate. Investigate the meters before shipping.

---

### 🟠 HIGH 4 — Paused SQL DB / Stopped SQL Managed Instance show $0 savings

**What we do today.** `detect_paused_sql_databases` and
`detect_stopped_sql_managed_instances` emit findings with a **savings value of 0.0**. They
correctly identify real waste (a paused DB still accrues storage; a stopped MI still bills
vCores) but **do not quantify it**, so they appear in the report as "opportunities" worth
nothing.

**Why this is wrong.** A $0 line in a savings report is noise, and the *actual* billed cost of
the paused DB / stopped MI is available in Cost Management.

**Proper enterprise implementation.** Ground the saving in the resource's **actual billed
cost** (`cost_map`). If it's unavailable, **don't present it as a quantified opportunity** (or
mark it clearly "cost unavailable, informational only").

---

## MEDIUM — real data, but tool-chosen eligibility / thresholds (defensible, but assumptions)

These modules use **real Azure Monitor metrics** and **real Retail/Cost-Management prices**,
so the *dollar figures* are grounded. What is tool-side is the **eligibility judgment** — the
threshold that decides a resource is idle/oversized, and the **target SKU** the tool picks.
None of these fabricate money, but the assumptions should be disclosed so they're defensible.

### 5 — VM / App Service Plan / SQL DB / SQL MI rightsizing
- **Data:** real peak CPU/memory over a metric window (Azure Monitor), real prices for current
  and target SKU (Retail Prices API), saving = real price delta capped at the resource's
  actual billed cost.
- **Assumptions:** a **70% headroom ceiling** (`DOWNSIZE_HEADROOM_CEILING`) decides whether
  the next size down is safe, and the tool **chooses the target SKU** off an internal ladder
  (e.g. `_SQL_VCORE_LADDER`). **Azure Advisor also performs rightsizing** and is arguably the
  more authoritative recommender.
- **Proper approach:** keep the real price delta, **disclose the 70% ceiling and the metric
  window** in the finding's rationale, and where Advisor has a matching rightsizing rec,
  prefer / cross-check against it.

### 6 — Disk rightsizing (Premium SSD → Standard SSD)
- **Data:** real peak disk IOPS + throughput (Azure Monitor), real prices for Premium vs
  Standard, saving capped at actual billed disk cost. Requires **both** IOPS and throughput
  signals — a disk with no metrics is left alone. Disks on registered SQL VMs are excluded
  (they need Premium's low latency even at trivial IOPS).
- **Assumptions:** hardcoded Standard SSD baselines of **500 IOPS / 60 MB/s**
  (`_STANDARD_SSD_BASELINE_IOPS`, `_STANDARD_SSD_BASELINE_MBPS`) and the same 70% ceiling.
- **Proper approach:** disclose the baselines and the "verify workload isn't latency-sensitive"
  caveat (already present in the recommendation text).

### 7 — Idle VMs
- **Data:** real peak CPU / memory (Azure Monitor), saving = the VM's **real** billed cost.
- **Assumptions:** idle defined as **≤5% peak CPU / ≤10% memory** (`IDLE_MAX_CPU`,
  `IDLE_MAX_MEMORY_PCT`).
- **Proper approach:** disclose the thresholds; consider cross-checking Advisor's shutdown/
  low-usage recs.

### 8 — Windows Azure Hybrid Benefit (ownership)
- **Data:** eligibility from Resource Graph (`licenseType`), price from the **live per-VM
  Windows−Linux retail delta** (authoritative and correct).
- **Unknowable:** whether the customer actually **owns** the Windows Server licences with
  Software Assurance needed to realise the benefit — no Azure API exposes this. The tool
  correctly presents this as **conditional** ("you can save this much *if* you hold the
  licences").
- **Proper approach:** keep it conditional and clearly labelled; never fold an unverifiable
  benefit into headline savings without the caveat.

---

## LOW — fallbacks and tool-computed non-headline numbers

### 9 — Load Balancer / NAT Gateway / Bastion (empty/idle)
- **Data:** state from Resource Graph (authoritative), price fetched **live** from the Retail
  Prices API in the billing currency.
- **Risk:** if the live fetch fails, the code falls back to a **dated hardcoded estimate**
  (`LOAD_BALANCER_MONTHLY_USD = 18`, `NAT_GATEWAY_MONTHLY_USD = 32`, `BASTION_MONTHLY_USD =
  138`). The fallback is a last resort but could surface a stale number.
- **Proper approach:** prefer Cost Management actual cost first, then live retail, then
  **hide** — drop the dated fallback.

### 10 — Confidence score
- Tool-computed heuristic from metric datapoint count and freshness (`metrics_confidence`).
  Not Microsoft data. Now largely internal (severity is savings-magnitude only; the confidence
  chip was removed from the client UI). Fine to keep as an internal signal; don't present it as
  an Azure-sourced number.

### 11 — Growth projection (report)
- Tool-computed least-squares regression over the customer's **real** historical spend. It's a
  legitimate projection, not a fabricated cost — but it *is* an extrapolation and should be
  labelled as a projection, not a fact.

---

## FULLY AUTHORITATIVE — keep as-is (no fabrication)

- **Advisor findings** — Microsoft's own recommendations *and* Microsoft's own savings numbers,
  passed through. Source of truth = Azure Advisor.
- **Reserved-capacity / RI for non-VM** — driven by
  `Microsoft.Consumption/reservationRecommendations` (real, usage-based). This is exactly the
  engine VMs should also use.
- **Deallocated VMs** — state from Resource Graph (`powerState`), saving = the still-billing
  attached disks' **real** cost from Cost Management. Drops out if no cost data (no fabrication).
- **Unattached disks** — `diskState` from Resource Graph + **live** retail disk price.
- **Orphaned public IPs** — Resource Graph state + **live** retail IP price.

---

## PER-MODULE SCORECARD

| Module | Authoritative? | Current source of truth | Better/best source | Tool computes | Fabrication risk | Proper fix |
|---|---|---|---|---|---|---|
| **RI (VM)** | ❌ No | Retail Prices (incomplete) | **Consumption reservationRecommendations** (already fetched, not used for VM) | discount ratio | Was yes; now under-covers | Route VMs through the reservation engine |
| RI / reserved (non-VM) | ✅ Yes | Consumption reservationRecommendations | same | none | none | Keep |
| Windows AHB | ✅ price / ⚠ ownership | Retail (Win−Linux delta) + ARG `licenseType` | same | licence fraction | none (flagged conditional) | Keep; ownership can't be API-verified |
| **SQL AHB** | ⚠ Hardcoded rate | `estimates.py` $112/vCore | Retail SQL meters | yes | drift / over-state | Fetch licence delta from Retail |
| VM rightsizing | ⚠ real data, tool judgment | Azure Monitor peak + Retail | + **Advisor** | target SKU + 70% ceiling | none (real prices) | Disclose ceiling; cross-check Advisor |
| ASP / SQL DB / SQL MI rightsizing | ⚠ same | Monitor + Retail | + Advisor | target vCore + 70% ceiling | none | same |
| Disk Premium→Standard | ⚠ same | Disk metrics + Retail | — | 500 IOPS / 60 MB/s + 70% | none | Disclose baselines; keep SQL-VM exclusion |
| Idle VMs | ✅ mostly | Monitor peak + real VM cost | + Advisor | 5% CPU / 10% mem threshold | none | Disclose thresholds |
| Deallocated VMs | ✅ Yes | ARG `powerState` + Cost Mgmt disk cost | same | none | none | Keep |
| Unattached disks | ✅ Yes | ARG `diskState` + live Retail | same | none | none | Keep |
| Orphaned public IPs | ✅ Yes | ARG + live Retail | same | none | none | Keep |
| Empty LB / NAT / Bastion | ✅ live / ⚠ fallback | ARG + live Retail (→ dated fallback) | Cost Mgmt actual | none | fallback = dated | Prefer Cost Mgmt; drop dated fallback |
| **Snapshots** | ❌ Estimate | $0.05/GB × provisioned | **Cost Management actual** | yes | over-states | Ground in Cost Mgmt |
| **GRS vault** | ❌ Estimate | flat $25 | **Cost Management actual** | yes | guess | Ground in Cost Mgmt |
| **Paused SQL / Stopped MI** | ⚠ $0 shown | ARG state; no cost | **Cost Management actual** | none | unquantified | Ground or hide |
| Advisor findings | ✅ Yes | Azure Advisor (savings included) | same | none | none | Keep |
| Confidence score | ⚠ tool heuristic | datapoints / freshness | — | yes | none (internal) | Keep internal only |
| Growth projection | ⚠ tool regression | least-squares on real spend | — | yes | it's a projection | Keep, labelled as projection |

---

## THE TWO SYSTEMIC ROOT CAUSES

1. **RI sources the wrong API.** The Retail Prices API is not the reservation authority.
   `Microsoft.Consumption/reservationRecommendations` is — and the tool already has it, but
   only uses it for non-VM resources.
2. **"Estimate when unavailable" instead of "ground in the actual bill, or hide."** The tool
   already pulls per-resource billed cost into `cost_map`, yet snapshots, GRS vaults, and
   paused/stopped SQL fall back to hardcoded numbers or $0 instead of the real billed cost.
   The stated rule is *"show unavailable, don't estimate"* — the code violates it in
   `estimates.py` and these detectors.

---

## PROPOSED FIX PLAN (priority order)

1. **RI → reservation engine for VMs** (Critical 1). Un-skip the `ri_vm` bucket in
   `commitments_from_recommendations`; make `reservationRecommendations` the VM RI source of
   truth; retire the retail-estimate discount path for VMs. *Highest-impact, most-defensible
   change and the exact issue that triggered this audit.*
2. **Ground snapshots, GRS vaults, paused SQL, stopped MI in Cost Management actual cost**
   (Critical 2 + High 4). If a resource has no billed cost, mark **"cost unavailable"** instead
   of estimating / showing $0.
3. **SQL AHB from the Retail Prices API meters** instead of the $112 constant (High 3).
4. **Disclose the assumptions** that drive eligibility (70% headroom ceiling, 5% idle CPU,
   500 IOPS / 60 MB/s disk baseline, metric window) inside each finding's rationale, so they're
   defensible rather than hidden.
5. **Kill `estimates.py` as a silent fallback** — anywhere a live or actual price is missing,
   prefer Cost Management actual, then hide; never a dated table.

Each item should be implemented and verified independently (with tests), not shipped as one
large risky change.

---

## KEY CODE REFERENCES (for anyone verifying this audit)

- Engine & all detectors: `backend/app/services/findings.py`
  - RI (VM): `detect_vm_commitments`
  - RI (non-VM) / reservation engine: `commitments_from_recommendations` (+ `reservations.py`)
  - Windows/SQL AHB: `detect_windows_ahb`, `detect_sql_ahb`
  - Rightsizing: `detect_disk_rightsizing`, VM/ASP/SQL rightsizing helpers
  - Orphans / snapshots / vaults: `ORPHAN_RULES`, `detect_orphans`
  - Deallocated VMs: `detect_deallocated_vms`
  - Paused/stopped SQL: `detect_paused_sql_databases`, `detect_stopped_sql_managed_instances`
  - Thresholds: `IDLE_MAX_CPU = 5.0`, `IDLE_MAX_MEMORY_PCT = 10.0`,
    `DOWNSIZE_HEADROOM_CEILING = 70.0`, `_STANDARD_SSD_BASELINE_IOPS = 500`,
    `_STANDARD_SSD_BASELINE_MBPS = 60`
- Live pricing + sanity band + fallback: `backend/app/services/pricing.py`
- Hardcoded estimates (audit candidates): `backend/app/services/estimates.py`
  - `SNAPSHOT_PER_GB_MONTHLY_USD = 0.05`, `GRS_VAULT_MONTHLY_USD = 25.0`,
    `SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD = 112.0`, `LOAD_BALANCER_MONTHLY_USD = 18.0`,
    `NAT_GATEWAY_MONTHLY_USD = 32.0`, `BASTION_MONTHLY_USD = 138.0`,
    `APP_SERVICE_PLAN_MONTHLY_USD` (per-SKU dict)
