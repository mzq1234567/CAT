from datetime import datetime
from typing import List
import asyncio

from ..database import SessionLocal
from ..models.db import Assessment, Finding, InventoryItem
from .azure_client import AzureClient
from .inventory import collect_inventory
from .findings import evaluate_advisor_findings, run_all_inventory_findings


async def run_assessment(assessment_id: int, subscription_ids: List[str], token: str) -> None:
    """
    Background task that drives the full assessment lifecycle.
    Creates its own DB session — safe to run as a FastAPI BackgroundTask.
    """
    db = SessionLocal()
    try:
        assessment = db.get(Assessment, assessment_id)
        if not assessment:
            return

        assessment.status = "running"
        db.commit()

        client = AzureClient(token)

        # 1. Inventory via Resource Graph (all queries parallel)
        inventory = await collect_inventory(client, subscription_ids)

        # Persist inventory items
        for resource_type, items in inventory.items():
            for item in items:
                db.add(InventoryItem(
                    assessment_id=assessment_id,
                    subscription_id=item.get("subscriptionId", ""),
                    resource_id=item.get("id", ""),
                    resource_type=resource_type,
                    resource_name=item.get("name", ""),
                    location=item.get("location"),
                    resource_group=item.get("resourceGroup"),
                    data=item,
                ))
        db.commit()

        # 2. Azure Advisor cost recommendations — one call per subscription
        advisor_tasks = [
            client.get_advisor_cost_recommendations(sub_id)
            for sub_id in subscription_ids
        ]
        all_advisor_results = await asyncio.gather(*advisor_tasks, return_exceptions=True)
        all_advisor_recs: list = []
        for res in all_advisor_results:
            if isinstance(res, list):
                all_advisor_recs.extend(res)

        # 3. Evaluate findings
        findings_data = evaluate_advisor_findings(all_advisor_recs)
        findings_data += run_all_inventory_findings(inventory)

        # 4. Persist findings and roll up totals
        total_monthly = 0.0
        total_annual = 0.0
        for fd in findings_data:
            db.add(Finding(assessment_id=assessment_id, **fd))
            total_monthly += fd.get("estimated_savings_monthly", 0)
            total_annual += fd.get("estimated_savings_annual", 0)

        assessment.status = "completed"
        assessment.completed_at = datetime.utcnow()
        assessment.total_savings_monthly = round(total_monthly, 2)
        assessment.total_savings_annual = round(total_annual, 2)
        assessment.findings_count = len(findings_data)
        db.commit()

    except Exception as exc:
        db.rollback()
        try:
            assessment = db.get(Assessment, assessment_id)
            if assessment:
                assessment.status = "failed"
                assessment.error_message = str(exc)
                db.commit()
        except Exception:
            pass
        raise
    finally:
        db.close()
