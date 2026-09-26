"""Standalone live check of every MCP tool against your Zepp account.
Run: uv run python test_connection.py"""

import json
from datetime import date, timedelta

import server as S


def show(name: str, value, limit: int = 1200) -> None:
    print(f"\n== {name}\n{json.dumps(value, indent=1, ensure_ascii=False)[:limit]}")


def main() -> None:
    show("zepp_status", S.zepp_status())
    show("get_devices", S.get_devices())
    workouts = S.list_workouts(limit=3)
    show("list_workouts (latest 3)", workouts, 2000)
    show("summarize_workouts (by month)", S.summarize_workouts(group_by="month")[:6])
    if workouts:
        tid = workouts[0]["trackid"]
        show(f"get_workout_detail({tid})", S.get_workout_detail(tid), 2000)
        show(f"get_workout_track({tid}, max_points=5)", S.get_workout_track(tid, max_points=5))
    show("get_daily_summary (last 7 days)", S.get_daily_summary(), 1500)
    three_days_ago = (date.today() - timedelta(days=2)).isoformat()
    show("get_health_metrics (last 3 days)", S.get_health_metrics(from_date=three_days_ago), 2000)


if __name__ == "__main__":
    main()
