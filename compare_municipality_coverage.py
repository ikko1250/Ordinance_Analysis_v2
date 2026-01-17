#!/usr/bin/env python3
import csv
import sqlite3
from collections import Counter, defaultdict


def load_csv_municipalities(csv_path):
    municipalities = set()
    muni_flags = defaultdict(lambda: {"has_ordinance": False, "has_rule": False})
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            muni = row["自治体"].strip()
            category = row["区分"].strip()
            municipalities.add(muni)
            if category == "条例":
                muni_flags[muni]["has_ordinance"] = True
            elif category == "施行規則":
                muni_flags[muni]["has_rule"] = True
    return municipalities, muni_flags


def load_db_municipalities(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT m.prefecture_name, m.municipality_name
        FROM municipalities m
        INNER JOIN ordinances o ON o.municipality_id = m.id
        GROUP BY m.prefecture_name, m.municipality_name
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def build_prefecture_prefixes(prefecture_names):
    suffixes = ("都", "道", "府", "県")
    prefixes = set()
    for name in prefecture_names:
        if name.endswith(suffixes):
            prefixes.add(name)
        else:
            for suffix in suffixes:
                prefixes.add(name + suffix)
    return sorted(prefixes, key=len, reverse=True)


def normalize_municipality(name, prefecture_prefixes):
    normalized = name.strip()
    for prefix in prefecture_prefixes:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    if normalized.startswith(("都", "道", "府", "県")):
        normalized = normalized[1:]
    return normalized.strip()


def main():
    csv_path = "main4.3_2026.csv"
    db_path = "data/ordinance_data.db"

    csv_munis, muni_flags = load_csv_municipalities(csv_path)
    db_rows = load_db_municipalities(db_path)

    prefecture_names = sorted({pref for pref, _ in db_rows})
    prefecture_prefixes = build_prefecture_prefixes(prefecture_names)

    csv_munis_norm = {normalize_municipality(name, prefecture_prefixes) for name in csv_munis}
    db_munis_norm = [normalize_municipality(name, prefecture_prefixes) for _, name in db_rows]
    db_muni_set = set(db_munis_norm)

    dup_counts = Counter(db_munis_norm)
    dup_names = sorted([name for name, count in dup_counts.items() if count > 1])

    csv_only = sorted(csv_munis_norm - db_muni_set)
    db_only = sorted(db_muni_set - csv_munis_norm)

    csv_flags_norm = defaultdict(lambda: {"has_ordinance": False, "has_rule": False})
    for muni, flags in muni_flags.items():
        norm = normalize_municipality(muni, prefecture_prefixes)
        if flags["has_ordinance"]:
            csv_flags_norm[norm]["has_ordinance"] = True
        if flags["has_rule"]:
            csv_flags_norm[norm]["has_rule"] = True

    missing_with_rule = []
    missing_without_rule = []
    missing_only_rule = []
    missing_only_ordinance = []

    for muni in csv_only:
        has_rule = csv_flags_norm[muni]["has_rule"]
        has_ordinance = csv_flags_norm[muni]["has_ordinance"]
        if has_rule:
            missing_with_rule.append(muni)
        else:
            missing_without_rule.append(muni)
        if has_rule and not has_ordinance:
            missing_only_rule.append(muni)
        if has_ordinance and not has_rule:
            missing_only_ordinance.append(muni)

    csv_rule_counts = Counter()
    for muni in csv_munis_norm:
        flags = csv_flags_norm[muni]
        if flags["has_rule"] and flags["has_ordinance"]:
            csv_rule_counts["both"] += 1
        elif flags["has_rule"]:
            csv_rule_counts["rule_only"] += 1
        elif flags["has_ordinance"]:
            csv_rule_counts["ordinance_only"] += 1
        else:
            csv_rule_counts["neither"] += 1

    print("CSV municipalities (normalized):", len(csv_munis_norm))
    print("DB municipalities (normalized, ordinances table):", len(db_muni_set))
    print("CSV only (missing in DB):", len(csv_only))
    print("DB only (missing in CSV):", len(db_only))
    print()
    print("CSV municipality coverage by category:")
    for key in ["both", "rule_only", "ordinance_only", "neither"]:
        print(f"  {key}: {csv_rule_counts[key]}")
    print()
    print("Missing municipalities (CSV only) by rule presence:")
    print("  with 施行規則:", len(missing_with_rule))
    print("  without 施行規則:", len(missing_without_rule))
    print("  only 施行規則:", len(missing_only_rule))
    print("  only 条例:", len(missing_only_ordinance))
    print()
    print("Duplicate municipality names in DB across prefectures:", len(dup_names))
    if dup_names:
        print("  Examples:", ", ".join(dup_names[:10]))
    print()
    if csv_only:
        print("CSV-only municipalities (first 20):", ", ".join(csv_only[:20]))
    if db_only:
        print("DB-only municipalities (first 20):", ", ".join(db_only[:20]))


if __name__ == "__main__":
    main()
