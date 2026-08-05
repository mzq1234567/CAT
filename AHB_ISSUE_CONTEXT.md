# Azure Hybrid Benefit (AHB) Savings — Investigation & Context Report

> **Purpose of this document:** capture, with full context, an issue observed in an internal
> Azure Cost Assessment Tool where the Azure Hybrid Benefit (AHB) saving for a 24×7 production
> VM was reported as **₹1,407/year** when the Azure Pricing Calculator shows the Windows licence
> cost for that same VM as **₹12,847.71/month (≈ ₹154,000/year)**. This file is written to be
> self-contained so it can be handed to another assistant (e.g. ChatGPT) for a second opinion.
> Four screenshots referenced below (SS1–SS4) are attached separately by the user.

---

## 1. Background — what the tool is and how it computes AHB savings

**The tool (internal name "CAT" — Cost Assessment Tool):** a read-only Azure cost-optimization
web app. It logs in with the user's Azure credentials (Reader-level), inventories resources via
Azure Resource Graph, pulls per-resource cost from Azure Cost Management, live-fetches prices from
the Azure Retail Prices API, and produces savings findings (rightsizing, idle VMs, Reserved
Instances, Azure Hybrid Benefit, etc.).

**Core design principle ("grounding"):** every saving is **grounded in, and capped at, the
resource's actual billed cost**. The tool deliberately never uses list price for a headline number.
The rule was adopted specifically so the tool can never over-state savings in front of a client's
executives (e.g. it must never claim "you'll save ₹X" that is larger than what the resource
actually costs).

**How the AHB saving is computed (the relevant logic):**

Azure charges a Windows Server VM as **compute (hardware) + Windows Server licence**, both bundled
into one hourly rate. A same-size **Linux** VM is the identical hardware **without** the licence.
So the licence cost = `Windows price − Linux price`.

The tool:

1. For every eligible running Windows VM, fetches the Windows monthly price and the Linux
   (compute-only) monthly price from the Retail Prices API.
2. Estimates the **per-vCore Windows licence** as the **median** of `(Windows − Linux) / vCPU`
   across the whole fleet. (Median is used to neutralize an occasional bad per-SKU price fetch —
   e.g. some B-series SKUs return a Windows price barely above Linux, which would understate them.)
3. For each VM: `licence = vCPU × per-vCore-licence`, and `licence_fraction = licence / Windows_price`.
4. **Applies that licence fraction to the VM's ACTUAL billed cost:**

```python
# Simplified from the actual implementation:
lic_fraction = licence / windows_effective_price      # e.g. 0.489 for D4s v3
if cost_available:
    monthly_saving = actual_billed_cost * lic_fraction # grounded in real spend
    grounded = True
else:
    monthly_saving = licence                           # fallback: retail run-rate
    grounded = False
```

The finding description shown to the user reads:
*"N Windows VMs pay Azure's Windows Server licence on top of compute. Azure Hybrid Benefit removes
that charge if you already own Windows Server licences with Software Assurance — the saving shown is
that licence portion."*

---

## 2. The observed symptom (SCREENSHOTS)

### SS1 — the tool's AHB finding
Header: **"12 VMS ELIGIBLE FOR AZURE HYBRID BENEFIT"**
Row: **CRA-VM** — `Standard_D4s_v3 · eastus` — **₹1,407 / yr**

(CRA-VM is the top-listed / largest saving in the list of 12, because the list is sorted
highest-saving first.)

### SS2 — Azure Pricing Calculator, the same VM
- Service: Virtual Machines
- Region: **East US**, Operating system: **Windows**, Type: (OS Only), Tier: Standard
- Instance: **D4s v3: 4 vCPUs, 16 GB RAM, 32 GB temp storage, ₹35.964/hour**
- Quantity: **1 virtual machine × 730 hours** (i.e. full-time / 24×7 for a month)

### SS3 — Pricing Calculator, "License included" (paying for the Windows licence)
Under **Savings Options → OS (Windows) → "License included"** selected:
- **Compute (D4s v3): ₹13,406.30 / month** (average per month)
- **OS (Windows): ₹12,847.71 / month** (average per month)  ← this is the Windows licence cost
- **Total: ₹26,254.01 / month**

### SS4 — Pricing Calculator, "Azure Hybrid Benefit" (licence brought by customer)
Under **Savings Options → OS (Windows) → "Azure Hybrid Benefit"** selected:
- **Compute (D4s v3): ₹13,406.30 / month**
- **OS (Windows): ₹0.00 / month**  ← licence charge removed by AHB
- **Total: ₹13,406.30 / month**

**Therefore, per the calculator, applying AHB to this D4s v3 saves ₹12,847.71/month
(₹26,254.01 − ₹13,406.30) = ~₹154,172/year — if the VM runs full-time.**

### The discrepancy
- Pricing Calculator (full-time): AHB saves **₹12,847.71 / month ≈ ₹154,000 / year**.
- The tool (SS1): AHB saves **₹1,407 / year** (≈ ₹117 / month).
- The tool's number is ~**100× smaller** than the calculator's.

The user's original question: *"if we have the licence, this VM should save ₹12,847.71/month —
but the tool only shows ₹1,407/year. What is this?"*

---

## 3. The math trace — reconciling the two numbers

The licence fraction from the calculator:
```
lic_fraction = OS(Windows) / Total = 12,847.71 / 26,254.01 = 0.4894  (≈ 49% of the Windows bill is licence)
```

The tool's per-vCore estimate matches the calculator exactly:
```
₹12,847.71 licence / 4 vCPU = ₹3,211.93 per vCore  →  the tool's median per-vCore ≈ ₹3,212. ✔ identical
```

So the tool's **licence math is correct to the rupee.** Now reverse-engineer the tool's ₹1,407/yr:
```
₹1,407 / yr ÷ 12          = ₹117.25 / month  (tool's AHB saving)
₹117.25 / 0.4894          = ₹239.6 / month   (the ACTUAL billed cost the tool used for CRA-VM)
₹239.6 / ₹26,254 (24×7)   = 0.91%             (implied utilization ≈ 6.7 running hours in the month)
```

**Conclusion of the trace:** the tool believed CRA-VM only cost **~₹240 for the month**, i.e. that
it ran less than 1% of the time. It then correctly took the 49% licence slice of that tiny cost →
₹117/month → ₹1,407/year. The pricing math is right; **the cost basis it was fed is wrong.**

---

## 4. Why "how much the VM ran" matters for AHB (a key clarification)

A natural objection: *"AHB is just the licensing portion — why does the VM's runtime matter at all?
The licence has nothing to do with the hourly compute cost."*

**In Azure, this is not correct: the Windows Server licence IS an hourly charge, billed only while
the VM is running, and it is ₹0 when the VM is deallocated.** Microsoft's documentation, verbatim:

> "When you create a Windows Server virtual machine in Azure, the cost of the Windows Server licence
> is already included in the hourly pay of the VM… **You will only be billed for the time the VM is
> running. If you stop the VM you won't [be] charged for the compute resources or the Windows Server
> licence.**"
> — Microsoft Q&A, *Windows licence in Azure VM Billing*
> https://learn.microsoft.com/answers/a/1895515

> "The potential savings for deallocating the VM… would be if it is a **Windows VM without Hybrid
> Benefit — in that case you would not be paying the hourly licence fee when the VM is deallocated.**
> For a free-licence Linux VM there would be no savings since there is no hourly licence fee."
> — Microsoft Q&A, *VM Payment Options*
> https://learn.microsoft.com/answers/a/2023222

> Deallocated VMs "do not incur compute charges" (the licence is part of the compute hourly rate);
> only disk/network charges remain.
> — *States and billing status of Azure Virtual Machines*
> https://learn.microsoft.com/azure/virtual-machines/states-billing#power-states-and-billing

So mathematically:
```
AHB saving = licence_per_hour × (hours the VM actually ran)
```
The tool's "fraction × actual cost" method is algebraically identical to this. **The saving genuinely
scales with runtime** — you cannot save a per-hour licence charge for hours the machine was switched
off. So if CRA-VM had genuinely run only ~7 hours, ₹1,407/year would be the correct AHB saving.

**But CRA-VM runs 24×7.** So the ~₹240 monthly cost the tool used is not real — which points to the
cost *data*, not the VM.

---

## 5. ROOT CAUSE — partial / unrepresentative billing window after a subscription migration

The tool grounds savings in the **last complete billing month** (with a fallback to month-to-date
for brand-new subscriptions). The environment under assessment has a specific history:

- The workload was **migrated from an old subscription to a new sponsorship subscription on
  15 July 2026.**
- Resources only began accruing cost **in the new subscription from 15 July onward.**
- The assessment was run in **early August 2026.**
- **CRA-VM runs 24×7** (confirmed by the environment owner).

Consequences that make the cost basis unrepresentative:

1. **No complete billing month exists in the new subscription yet.** "Last complete month" (July)
   is only a partial ~16-day window (15–31 July); month-to-date (August) is only a few days old.
2. **Azure posts costs with a 1–3 day delay**, so the most recent days are not fully settled.
3. **On a mid-month migration, July's spend is split** — 1–14 July billed to the *old* subscription,
   15–31 July to the *new* one — and migrated resources' costs can take additional days to fully
   reconcile onto the new subscription.

Net effect: the `actual` billed cost the tool read for CRA-VM (~₹240) is a **partial, still-settling
fragment** of its true monthly cost. Because AHB — **and every other grounded saving (Reserved
Instances, rightsizing, etc.)** — is scaled off that same `actual`, **all absolute savings figures in
this particular assessment run are understated.** This is not specific to AHB; AHB just made it
visible because the calculator gave an easy cross-check.

**This is a real weakness in the tool, not just a quirk of this environment:** grounding in "last
complete month" silently under-reports for *any* subscription that has not yet lived a full billing
month — subscription migrations, brand-new subscriptions, and recently-provisioned resources all
trigger it.

---

## 6. The number that should have been reported

CRA-VM runs 24×7, so its representative monthly cost is the **full-month run-rate**, and the correct
AHB saving equals the calculator's:
```
4 vCPU × ₹3,211.93/vCore = ₹12,847.71 / month  ≈  ₹154,172 / year
```
The tool's own licence estimate already computes this figure; it was only scaled down by the bad
(partial-window) cost basis.

---

## 7. Proposed fix

**Detect that a subscription has no complete billing month yet, and switch the saving basis from
"actual billed cost" to the Azure Retail Prices API run-rate — clearly flagged**, e.g.:
*"Estimated at list run-rate; this subscription has less than one complete billing month
(migrated/created recently). Re-run after a full billing month for billed-actual figures."*

- When a full, representative billing month **exists** → keep the current grounded (actual-cost) basis.
- When it **does not** (young/migrated subscription) → use the run-rate, flagged as an estimate.
- The signal needed to detect this is already available: the tool's 6-month cost-history query shows
  the subscription has only a single partial month.
- The same fix applies identically to Reserved Instance savings and any other grounded finding.

**Validation path (independent of the code fix):** re-run the assessment after **31 August 2026**,
once August is a complete billing month and the migration has fully settled. At that point the
grounded numbers should line up with the run-rate, confirming both the tool and the fix.

---

## 8. Specific questions for a second opinion (ChatGPT)

1. Is the analysis correct that the Windows Server licence in Azure is billed **per running hour**
   and is **₹0 when the VM is deallocated**, making AHB savings scale with runtime? (We believe yes,
   per the Microsoft docs cited in §4.)
2. Given a subscription with **no complete billing month** (migrated on 15 July, assessed in early
   August), is switching the saving basis to the **Retail Prices API run-rate (flagged as an
   estimate)** the right approach — or is there a more accurate method (e.g. normalizing the partial
   billed period to a daily run-rate × days-in-month, or using Azure Monitor runtime hours)?
3. Are there **edge cases** where the run-rate fallback would over-state savings — e.g. a VM that is
   *running but part-time* (say 12 hours/day) in a young subscription, where neither a full billing
   month nor a reliable duty-cycle is known? How should the tool handle "running but not necessarily
   24×7" when it has no complete month to measure against?
4. For AHB specifically, is it defensible to present the **full-month licence run-rate** as the
   headline saving for a VM confirmed to run 24×7, given the tool's guiding rule is "never over-state
   / always cap at actual cost"? Where is the right line between *grounded* (conservative, can
   under-state) and *run-rate* (matches the calculator, can over-state)?

---

## Appendix — quick-reference numbers (D4s v3, East US, INR)

| Quantity | Value |
|---|---|
| Compute-only (Linux) monthly, 24×7 | ₹13,406.30 |
| Windows licence monthly, 24×7 (OS portion) | ₹12,847.71 |
| Windows total (compute + licence) monthly, 24×7 | ₹26,254.01 |
| Per-vCore Windows licence (12,847.71 ÷ 4) | ₹3,211.93 |
| Licence fraction of Windows bill | 0.4894 (≈ 49%) |
| Correct AHB saving @ 24×7 | ₹12,847.71/mo ≈ ₹154,172/yr |
| Tool-reported AHB saving (SS1) | ₹1,407/yr (≈ ₹117/mo) |
| Implied `actual` cost the tool used | ~₹240/mo (≈ 0.9% utilization) |
| Root cause | Partial/unsettled billing window after 15 Jul 2026 subscription migration |
