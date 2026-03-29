from typing import Optional

from fastapi import APIRouter, Depends

from app.core.dependencies import get_current_user
from app.db import get_db

router = APIRouter(prefix="/savings", tags=["savings"])


@router.get("")
def get_savings(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    cur.execute("SELECT * FROM user_savings WHERE user_id = %s", (current_user["id"],))
    row = cur.fetchone()
    # Compute weekly trends (last 8 weeks)
    cur2 = db.cursor()
    cur2.execute("""
        SELECT
            TO_CHAR(DATE_TRUNC('week', logged_at), 'Mon DD') AS week,
            SUM(CASE WHEN action = 'used' THEN estimated_value ELSE 0 END) AS saved,
            SUM(CASE WHEN action = 'tossed' THEN estimated_value ELSE 0 END) AS wasted
        FROM waste_log
        WHERE user_id = %s AND logged_at >= NOW() - INTERVAL '8 weeks'
        GROUP BY DATE_TRUNC('week', logged_at)
        ORDER BY DATE_TRUNC('week', logged_at)
    """, (current_user["id"],))
    weekly_trends = [
        {"week": r["week"], "saved": float(r["saved"] or 0), "wasted": float(r["wasted"] or 0)}
        for r in cur2.fetchall()
    ]

    if row is None:
        return {
            "totalSavedThisMonth": 0.0,
            "totalSavedAllTime": 0.0,
            "totalWastedThisMonth": 0.0,
            "lastMonthSaved": 0.0,
            "weeklyTrends": weekly_trends,
        }
    return {
        "totalSavedThisMonth": float(row["saved_this_month"] or 0),
        "totalSavedAllTime": float(row["total_saved"] or 0),
        "totalWastedThisMonth": float(row["wasted_this_month"] or 0),
        "lastMonthSaved": 0.0,  # no prior-month view yet
        "weeklyTrends": weekly_trends,
    }


@router.get("/log")
def get_savings_log(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
    limit: int = 20,
    cursor: Optional[str] = None,
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    if cursor:
        cur.execute("""
            SELECT id, item_name, action, estimated_value, logged_at
            FROM waste_log
            WHERE user_id = %s AND logged_at < %s::timestamptz
            ORDER BY logged_at DESC
            LIMIT %s
        """, (uid, cursor, limit + 1))
    else:
        cur.execute("""
            SELECT id, item_name, action, estimated_value, logged_at
            FROM waste_log
            WHERE user_id = %s
            ORDER BY logged_at DESC
            LIMIT %s
        """, (uid, limit + 1))

    rows = cur.fetchall()
    has_more = len(rows) > limit
    entries = rows[:limit]
    next_cursor = entries[-1]["logged_at"].isoformat() if has_more and entries else None

    return {
        "entries": [
            {
                "id": str(r["id"]),
                "itemName": r["item_name"],
                "action": r["action"],
                "estimatedValue": float(r["estimated_value"] or 0),
                "date": r["logged_at"].isoformat().replace("+00:00", "Z"),
            }
            for r in entries
        ],
        "next_cursor": next_cursor,
    }
