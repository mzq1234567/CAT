"""
Reconcile a completed assessment - the audit + validation harness.

Prints every finding's FULL derivation (inputs -> formula -> result) so each number can be hand-checked
against the Azure portal / pricing calculator, then runs integrity checks (savings <= spend, no
double-count, data-source sanity). This is how you turn "looks right" into "verified".

Usage (from backend/):
    python -m scripts.reconcile_assessment            # newest assessment
    python -m scripts.reconcile_assessment 32         # a specific assessment id
"""
from __future__ import annotations

import sys

from app.database import SessionLocal
from app.models.db import Assessment, Finding


def _f(v, dec=2):
    return f"{(v or 0):,.{dec}f}"


def reconcile(assessment_id: int | None = None) -> int:
    db = SessionLocal()
    try:
        q = db.query(Assessment).order_by(Assessment.id.desc())
        a = db.get(Assessment, assessment_id) if assessment_id else q.first()
        if not a:
            print("No assessment found.")
            return 1
        cur = (a.currency or "USD")
        findings = db.query(Finding).filter(Finding.assessment_id == a.id).all()

        print(f"\n=== Assessment #{a.id} - reconciliation ===")
        print(f"Client: {a.tenant_display_name or '-'} | Currency: {cur} | "
              f"Cost data: {'YES' if a.cost_data_available else 'NO (list-price fallback!)'}")
        if a.current_annual_spend:
            pct = round((a.total_savings_annual or 0) / a.current_annual_spend * 100)
            print(f"Last-month spend: {cur} {_f(a.current_monthly_spend)}/mo ({cur} {_f(a.current_annual_spend)}/yr)")
            print(f"Identified savings: {cur} {_f(a.total_savings_monthly)}/mo "
                  f"({cur} {_f(a.total_savings_annual)}/yr) = {pct}% of spend")
        else:
            print(f"Identified savings: {cur} {_f(a.total_savings_annual)}/yr "
                  f"(NO SPEND DATA - figures are list-price upper bounds)")

        by_cat: dict = {}
        for f in findings:
            by_cat.setdefault(f.category, []).append(f)

        for f in findings:
            d = f.details or {}
            if f.category == "windows_ahb":
                print(f"\n--- Windows Azure Hybrid Benefit - {cur} {_f(f.estimated_savings_annual)}/yr ---")
                print(f"  {'VM':<18}{'SKU':<18}{'Windows':>10}{'Compute':>10}{'Licence':>10}"
                      f"{'Actual':>10}{'Save/mo':>10}")
                for v in d.get("eligible_vms", []):
                    print(f"  {str(v.get('name'))[:17]:<18}{str(v.get('sku'))[:17]:<18}"
                          f"{_f(v.get('windows_price')):>10}{_f(v.get('compute_only_price')):>10}"
                          f"{_f(v.get('licence_charge')):>10}"
                          f"{(_f(v.get('actual_monthly_cost')) if v.get('actual_monthly_cost') else 'list'):>10}"
                          f"{_f(v.get('monthly_savings')):>10}")
                print("  -> saving/mo = (actual cost, or Windows price if no billing) x licence fraction")
            elif f.category in ("ri_vm", "savings_plan_vm") or f.category.endswith("_reserved_capacity"):
                kind = d.get("kind", "Commitment")
                print(f"\n--- {kind}s - {cur} {_f(f.estimated_savings_annual)}/yr (best case, 3-yr) ---")
                print(f"  1-yr total {cur} {_f((d.get('total_1yr_monthly') or 0) * 12)}/yr  |  "
                      f"source: {d.get('source')}")
                print(f"  {'VM/SKU':<22}{'Base':>10}{'1yr%':>7}{'3yr%':>7}{'1yr/mo':>9}{'3yr/mo':>9}")
                for it in d.get("reservation_items", []):
                    print(f"  {str(it.get('name') or it.get('sku'))[:21]:<22}"
                          f"{_f(it.get('compute_base') or it.get('ondemand')):>10}"
                          f"{(_f((it.get('discount_1yr') or 0) * 100, 0)):>6}%"
                          f"{(_f((it.get('discount_3yr') or 0) * 100, 0)):>6}%"
                          f"{_f(it.get('monthly_savings')):>9}"
                          f"{_f(it.get('monthly_savings_3yr')):>9}")
                print("  -> saving/mo = compute_base x discount%  (base strips Windows licence so no AHB overlap)")
            elif f.category == "oversized_vms":
                print(f"\n--- Right-size: {f.resource_name} - {cur} {_f(f.estimated_savings_annual)}/yr ---")
                print(f"  {d.get('current_sku')} ({cur} {_f(d.get('current_monthly_price'))}/mo) -> "
                      f"{d.get('recommended_sku')} ({cur} {_f(d.get('recommended_monthly_price'))}/mo); "
                      f"peak CPU {d.get('max_cpu')}%, peak mem {d.get('peak_memory_used_pct')}%")

        # Non-commitment/AHB/right-size findings, one line each.
        misc = [f for f in findings if f.category not in
                ("windows_ahb", "ri_vm", "savings_plan_vm", "oversized_vms")
                and not f.category.endswith("_reserved_capacity")]
        if misc:
            print("\n--- Other findings ---")
            for f in misc:
                print(f"  [{f.severity:<8}] {f.display_name:<32} {f.resource_name or '-':<28} "
                      f"{cur} {_f(f.estimated_savings_annual)}/yr")

        # -- Integrity checks --
        print("\n--- CHECKS ---")
        ok = True

        def check(label, passed, detail=""):
            nonlocal ok
            ok = ok and passed
            print(f"  [{'OK ' if passed else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")

        raw = round(sum(f.estimated_savings_annual or 0 for f in findings), 2)
        check("findings sum to the stored total", abs(raw - (a.total_savings_annual or 0)) < 1.0,
              f"sum findings={_f(raw)} vs total={_f(a.total_savings_annual)}")
        if a.current_annual_spend:
            check("savings <= spend", (a.total_savings_annual or 0) <= a.current_annual_spend + 1,
                  f"{_f(a.total_savings_annual)} vs {_f(a.current_annual_spend)}")
        # No resource id appears in more than one grounded finding.
        seen: dict = {}
        dupe = None
        for f in findings:
            rid = (f.resource_id or "").lower()
            if rid and rid in seen:
                dupe = f.resource_name
            elif rid:
                seen[rid] = f.category
        check("no resource double-counted across findings", dupe is None, f"e.g. {dupe}" if dupe else "")
        ri = by_cat.get("ri_vm", [])
        if ri:
            src = (ri[0].details or {}).get("source")
            check("reservations from Azure's engine (not retail estimate)",
                  src == "azure_reservation_recommendations",
                  f"source={src} - check server log for why the Consumption API returned nothing"
                  if src != "azure_reservation_recommendations" else "")
        if not a.cost_data_available:
            check("cost data present (grounded, not list price)", False,
                  "billing data missing - re-run; do NOT present these figures")

        print("\n" + ("ALL CHECKS PASSED - figures are internally consistent." if ok
                      else "SOME CHECKS FAILED - investigate before presenting.") + "\n")
        return 0 if ok else 2
    finally:
        db.close()


if __name__ == "__main__":
    aid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(reconcile(aid))
