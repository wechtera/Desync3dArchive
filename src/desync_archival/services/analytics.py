"""
services/analytics.py - Library continuity, release grouping, and catalog statistical metrics.
Instrumented with dynamic topic-based subsystem logging ('sql').
"""

import logging
import math
import re
import sqlite3
from datetime import datetime
from typing import Any, Optional

from desync_archival.services.logger import log_layer

MONTH = re.compile(r"\d{4}-\d{2}")


def calculate_month_sequence(start_month: str, end_month: str) -> list[str]:
    """Computes a contiguous list of YYYY-MM formatted calendar months."""
    start_dt = datetime.strptime(start_month, "%Y-%m")
    end_dt = datetime.strptime(end_month, "%Y-%m")
    seq = []
    cur_y, cur_m = start_dt.year, start_dt.month
    end_y, end_m = end_dt.year, end_dt.month
    while (cur_y, cur_m) <= (end_y, end_m):
        seq.append(f"{cur_y:04d}-{cur_m:02d}")
        cur_m += 1
        if cur_m > 12:
            cur_m = 1
            cur_y += 1
    return seq


def run_startup_analytics(conn: sqlite3.Connection):
    """
    Augments the models table with release months and bonus designations,
    then recalculates release pack groupings and probable Patreon/Tribe bundles.
    """
    log_layer("sql", logging.INFO, "Starting catalog continuity calculation and monthly release clustering...")
    with conn:
        conn.execute("""
            UPDATE collections 
            SET release_month = substr(published_at, 1, 7)
            WHERE (release_month IS NULL OR release_month = '') 
              AND published_at IS NOT NULL;
        """)

        conn.execute("""
            UPDATE collections
            SET is_bonus = 1
            WHERE LOWER(name) LIKE '%reward%'
               OR LOWER(name) LIKE '%bonus%'
               OR id IN (
                   SELECT model_id FROM collection_tags 
                   WHERE LOWER(tag) LIKE '%reward%' OR LOWER(tag) LIKE '%bonus%'
               );
        """)

        conn.execute("DELETE FROM releases;")

        cluster_query = """
            SELECT 
                COALESCE(m.designer_id, 0) || '_' || m.release_month AS release_id,
                m.designer_name,
                m.designer_id,
                m.release_month,
                COUNT(DISTINCT CASE WHEN m.is_bonus = 0 THEN m.id END) AS regular_model_count,
                COUNT(DISTINCT CASE WHEN m.is_bonus = 1 THEN m.id END) AS bonus_model_count,
                COUNT(f.file_id) AS total_files,
                COALESCE(SUM(f.size_bytes), 0) AS total_size_bytes,
                MAX(m.primary_image_url) AS sample_image_url,
                GROUP_CONCAT(DISTINCT t.tag) AS all_tags,
                GROUP_CONCAT(m.description) AS all_descriptions
            FROM collections m
            LEFT JOIN model_files f ON m.id = f.model_id
            LEFT JOIN collection_tags t ON m.id = t.model_id
            WHERE m.is_active = 1 AND m.release_month IS NOT NULL AND m.release_month != ''
            GROUP BY m.designer_name, m.release_month;
        """
        clusters = conn.execute(cluster_query).fetchall()
        bundle_keywords = {"patreon", "tribe", "tribes", "welcome pack", "bundle", "release", "heroes", "pack"}

        release_rows = []
        for c in clusters:
            reg_count = c["regular_model_count"] or 0
            bonus_count = c["bonus_model_count"] or 0
            if reg_count == 0 and bonus_count == 0:
                continue

            tags_str = c["all_tags"] or ""
            desc_str = (c["all_descriptions"] or "").lower()
            combined_text = f"{tags_str.lower()} {desc_str}"

            is_bundle = 1 if (reg_count >= 4 or any(kw in combined_text for kw in bundle_keywords)) else 0
            top_tags = ",".join(list(dict.fromkeys(tags_str.split(",")))[:5]) if tags_str else ""

            release_rows.append((
                c["release_id"],
                c["designer_name"] or "Unknown Designer",
                c["designer_id"],
                c["release_month"],
                reg_count,
                bonus_count,
                c["total_files"],
                c["total_size_bytes"],
                is_bundle,
                c["sample_image_url"],
                top_tags,
            ))

        conn.executemany("""
            INSERT INTO releases (
                release_key, designer_name, designer_id, release_month,
                model_count, bonus_count, total_files, total_size_bytes,
                is_likely_bundle, sample_image_url, common_tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, release_rows)

    log_layer("sql", logging.INFO, f"Catalog analytics populated {len(release_rows)} distinct monthly release packs.")


def compute_catalog_audit(conn: sqlite3.Connection, designer: Optional[str] = None) -> dict[str, Any]:
    """
    Computes library or creator continuity metrics:
    expected calendar months, gaps, volume averages, and statistical outliers.
    """
    current_month = datetime.now().strftime("%Y-%m")
    log_layer("sql", logging.DEBUG, f"Computing continuity audit for: {designer or 'Entire Library'}")

    if designer:
        rows = conn.execute("""
            SELECT release_month, model_count, bonus_count 
            FROM releases 
            WHERE designer_name = ? 
            ORDER BY release_month ASC;
        """, (designer,)).fetchall()
        bonus_row = conn.execute(
            "SELECT COUNT(*) FROM collections WHERE designer_name = ? AND is_bonus = 1 AND is_active = 1;",
            (designer,)
        ).fetchone()
        total_bonus = bonus_row[0] if bonus_row else 0
    else:
        rows = conn.execute("""
            SELECT release_month, SUM(model_count) AS model_count, SUM(bonus_count) AS bonus_count
            FROM releases 
            GROUP BY release_month
            ORDER BY release_month ASC;
        """).fetchall()
        bonus_row = conn.execute("SELECT COUNT(*) FROM collections WHERE is_bonus = 1 AND is_active = 1;").fetchone()
        total_bonus = bonus_row[0] if bonus_row else 0

    if not rows:
        return {"designer": designer, "has_data": False}

    # Undated linked releases hold "Unassigned", which has no place in a calendar of gaps.
    month_data = {
        r["release_month"]: int(r["model_count"]) for r in rows if MONTH.fullmatch(r["release_month"] or "")
    }
    present_months = sorted(month_data.keys())
    if not present_months:
        return {"designer": designer, "has_data": False}

    oldest_month = present_months[0]
    latest_month = present_months[-1]
    target_end = max(latest_month, current_month)

    expected_all = calculate_month_sequence(oldest_month, target_end)
    missing = [m for m in expected_all if m not in month_data]

    counts = list(month_data.values())
    n = len(counts)
    avg_count = sum(counts) / n if n > 0 else 0.0
    variance = sum((c - avg_count) ** 2 for c in counts) / (n - 1) if n > 1 else 0.0
    std_dev = math.sqrt(variance)
    sensitivity = max(1.5 * std_dev, 3.0)

    high_outliers = []
    low_outliers = []
    for m, cnt in month_data.items():
        if cnt > avg_count + sensitivity:
            high_outliers.append({"month": m, "count": cnt, "diff": round(cnt - avg_count, 1)})
        elif (cnt < avg_count - sensitivity) or (cnt <= (avg_count * 0.5) and (avg_count - cnt) >= 3.0):
            low_outliers.append({"month": m, "count": cnt, "diff": round(avg_count - cnt, 1)})

    log_layer("sql", logging.DEBUG, f"Continuity audit complete: {len(present_months)}/{len(expected_all)} months present ({len(missing)} missing).")

    return {
        "designer": designer or "Entire Library",
        "has_data": True,
        "oldest_month": oldest_month,
        "latest_month": latest_month,
        "current_month": current_month,
        "expected_months_count": len(expected_all),
        "actual_months_count": len(present_months),
        "missing_count": len(missing),
        "missing_months": missing,
        "coverage_pct": round((len(present_months) / len(expected_all)) * 100, 1) if expected_all else 100.0,
        "avg_models": round(avg_count, 1),
        "std_dev": round(std_dev, 1),
        "total_bonus_models": total_bonus,
        "high_outliers": high_outliers,
        "low_outliers": low_outliers,
    }
