"""
stats_export.py
================

Exports your GATE CSE Tracker progress from the local SQLite database
into a single JSON file that the companion website (site/) reads.

This mirrors exactly what the desktop app's Dashboard / Analytics /
Activity tabs compute (same streak logic, same "weakest subject first"
sort, same 5+ L3 "urgent" flag) so the website's numbers always match
what you see in the app.

Run this from the same folder as app.py / database.py:

    python stats_export.py
    python stats_export.py --out site/data/stats.json
    python stats_export.py --db "C:\\Users\\you\\.gate_tracker\\gate_tracker.db" --out ..\\gate-track\\data\\stats.json

Then commit + push the updated stats.json (inside your gate-track repo)
to publish the new numbers on GitHub Pages.
"""

import argparse
import json
import os
from datetime import date, timedelta, timezone, datetime

from database import Database


def _streaks(daily):
    """Same logic as the desktop app's Activity tab: current streak
    (counting back from today, or yesterday if nothing's marked yet
    today), longest streak ever, active days, total questions marked."""
    active_dates = {d["date"] for d in daily}

    today = date.today()
    cursor = today if today.isoformat() in active_dates else today - timedelta(days=1)
    current = 0
    while cursor.isoformat() in active_dates:
        current += 1
        cursor -= timedelta(days=1)

    longest = 0
    run = 0
    prev_date = None
    for ds in sorted(active_dates):
        d = date.fromisoformat(ds)
        run = run + 1 if (prev_date is not None and (d - prev_date).days == 1) else 1
        longest = max(longest, run)
        prev_date = d

    return {
        "current_streak": current,
        "longest_streak": longest,
        "active_days": len(active_dates),
        "total_marked": sum(d["total"] for d in daily),
    }


def _pct_done(stats):
    t = stats["total"] or 1
    return (stats["L1"] + stats["L2"] + stats["L3"]) / t


def _chapter_list(stats_by_chapter):
    """Same shape as the Analytics tab's table: sorted weakest-first
    (lowest %done first), each row flagged 'urgent' at 5+ L3s."""
    out = []
    for name, stats in stats_by_chapter.items():
        pct = _pct_done(stats)
        out.append({
            "chapter_name": name,
            "total": stats["total"],
            "NONE": stats["NONE"],
            "L1": stats["L1"],
            "L2": stats["L2"],
            "L3": stats["L3"],
            "pct_done": round(pct * 100, 1),
            "urgent": stats["L3"] >= 5,
        })
    out.sort(key=lambda r: r["pct_done"])
    return out


def build_export(db_path=None):
    db = Database(db_path)
    try:
        sources = db.list_sources()
        question_counts = {
            s["id"]: sum(c[2] for c in db.get_chapters(s["id"])) for s in sources
        }

        unified_daily = db.get_daily_activity(None)

        by_source = {}
        for s in sources:
            sid = s["id"]
            daily = db.get_daily_activity(sid)
            by_source[str(sid)] = {
                "filename": s["filename"],
                "added_at": s["added_at"],
                "last_opened": s["last_opened"],
                "question_count": question_counts.get(sid, 0),
                "stats": db.get_stats(sid),
                "by_chapter": _chapter_list(db.get_stats_by_chapter(sid)),
                "activity": {**_streaks(daily), "daily": daily},
            }

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": [
                {
                    "id": s["id"],
                    "filename": s["filename"],
                    "added_at": s["added_at"],
                    "question_count": question_counts.get(s["id"], 0),
                }
                for s in sources
            ],
            "unified": {
                "stats": db.get_unified_stats(),
                "by_chapter": _chapter_list(db.get_unified_stats_by_chapter()),
                "activity": {**_streaks(unified_daily), "daily": unified_daily},
            },
            "by_source": by_source,
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=None,
        help="Path to gate_tracker.db (defaults to ~/.gate_tracker/gate_tracker.db)",
    )
    parser.add_argument(
        "--out", default="site/data/stats.json",
        help="Where to write the JSON export (default: site/data/stats.json)",
    )
    args = parser.parse_args()

    payload = build_export(args.db)

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    total = payload["unified"]["stats"]["total"]
    print(f"Wrote {args.out} \u2014 {total} questions across {len(payload['sources'])} volume(s).")


if __name__ == "__main__":
    main()
