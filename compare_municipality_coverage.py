#!/usr/bin/env python3
import csv
import sqlite3
from collections import Counter, defaultdict


def load_csv_rows(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            muni = row["自治体"].strip()
            category = row["区分"].strip()
            rows.append((muni, category))
    return rows


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


def normalize_name(name):
    normalized = name.strip()
    return normalized.replace("ヶ", "ケ").replace("ヵ", "ケ")


def build_prefecture_prefixes(plqrefecture_names):
    suffixes = ("都", "道", "府", "県")
    prefix_info = {}
    for name in prefecture_names:
        normalized = normalize_name(name)
        prefix_info[normalized] = (normalized, False)
        if normalized.endswith(suffixes):
            suffix = normalized[-1]
            base = normalized[:-1]
            if suffix in ("都", "府", "県"):
                prefix_info.setdefault(base, (normalized, True))
        else:
            for suffix in suffixes:
                prefix_info.setdefault(normalized + suffix, (normalized, False))
    prefixes = sorted(prefix_info.keys(), key=len, reverse=True)
    return prefixes, prefix_info


def split_prefecture_municipality(name, prefecture_prefixes, prefix_info):
    normalized = normalize_name(name)
    for prefix in prefecture_prefixes:
        if normalized.startswith(prefix):
            pref_name, is_suffixless = prefix_info[prefix]
            muni = normalized[len(prefix) :].strip()
            if not muni:
                return None, normalized
            if is_suffixless and len(muni) <= 1:
                # Avoid misclassifying municipalities like "大阪市" as prefixed.
                continue
            return pref_name, muni
    return None, normalized


def main():
    csv_path = "data/main4.3_2026.csv"
    db_path = "data/ordinance_data.db"

    csv_rows = load_csv_rows(csv_path)
    db_rows = load_db_municipalities(db_path)

    prefecture_names = sorted({normalize_name(pref) for pref, _ in db_rows})
    prefecture_prefixes, prefix_info = build_prefecture_prefixes(prefecture_names)

    db_set = {(normalize_name(pref), normalize_name(muni)) for pref, muni in db_rows}
    db_muni_set = {muni for _, muni in db_set}

    dup_counts = Counter([muni for _, muni in db_set])
    dup_names = sorted([name for name, count in dup_counts.items() if count > 1])

    csv_with_pref = set()
    csv_without_pref = set()
    csv_muni_names = set()

    csv_flags_with_pref = defaultdict(lambda: {"has_ordinance": False, "has_rule": False})
    csv_flags_without_pref = defaultdict(lambda: {"has_ordinance": False, "has_rule": False})
    csv_flags_by_name = defaultdict(lambda: {"has_ordinance": False, "has_rule": False})

    for muni_raw, category in csv_rows:
        pref, muni = split_prefecture_municipality(
            muni_raw, prefecture_prefixes, prefix_info
        )
        csv_muni_names.add(muni)
        if pref:
            key = (pref, muni)
            csv_with_pref.add(key)
            flags = csv_flags_with_pref[key]
        else:
            key = muni
            csv_without_pref.add(key)
            flags = csv_flags_without_pref[key]

        name_flags = csv_flags_by_name[muni]
        if category == "条例":
            flags["has_ordinance"] = True
            name_flags["has_ordinance"] = True
        elif category == "施行規則":
            flags["has_rule"] = True
            name_flags["has_rule"] = True

    missing_with_pref = sorted(csv_with_pref - db_set)
    missing_without_pref = sorted(csv_without_pref - db_muni_set)
    db_only_by_name = sorted([(pref, muni) for pref, muni in db_set if muni not in csv_muni_names])

    missing_with_rule_pref = []
    missing_without_rule_pref = []
    missing_only_rule_pref = []
    missing_only_ordinance_pref = []

    for pref, muni in missing_with_pref:
        flags = csv_flags_with_pref[(pref, muni)]
        has_rule = flags["has_rule"]
        has_ordinance = flags["has_ordinance"]
        if has_rule:
            missing_with_rule_pref.append((pref, muni))
        else:
            missing_without_rule_pref.append((pref, muni))
        if has_rule and not has_ordinance:
            missing_only_rule_pref.append((pref, muni))
        if has_ordinance and not has_rule:
            missing_only_ordinance_pref.append((pref, muni))

    missing_with_rule_no_pref = []
    missing_without_rule_no_pref = []
    missing_only_rule_no_pref = []
    missing_only_ordinance_no_pref = []

    for muni in missing_without_pref:
        has_rule = csv_flags_without_pref[muni]["has_rule"]
        has_ordinance = csv_flags_without_pref[muni]["has_ordinance"]
        if has_rule:
            missing_with_rule_no_pref.append(muni)
        else:
            missing_without_rule_no_pref.append(muni)
        if has_rule and not has_ordinance:
            missing_only_rule_no_pref.append(muni)
        if has_ordinance and not has_rule:
            missing_only_ordinance_no_pref.append(muni)

    csv_rule_counts = Counter()
    for flags in csv_flags_by_name.values():
        if flags["has_rule"] and flags["has_ordinance"]:
            csv_rule_counts["both"] += 1
        elif flags["has_rule"]:
            csv_rule_counts["rule_only"] += 1
        elif flags["has_ordinance"]:
            csv_rule_counts["ordinance_only"] += 1
        else:
            csv_rule_counts["neither"] += 1

    print("CSV municipalities (with prefecture):", len(csv_with_pref))
    print("CSV municipalities (without prefecture):", len(csv_without_pref))
    print("DB municipalities (prefecture + municipality):", len(db_set))
    print("CSV-only with prefecture (missing in DB):", len(missing_with_pref))
    print("CSV-only without prefecture (missing by name in DB):", len(missing_without_pref))
    print("DB-only by name (missing in CSV):", len(db_only_by_name))
    print()
    print("CSV municipality coverage by category:")
    for key in ["both", "rule_only", "ordinance_only", "neither"]:
        print(f"  {key}: {csv_rule_counts[key]}")
    print()
    print("Missing municipalities with prefecture (CSV only) by rule presence:")
    print("  with 施行規則:", len(missing_with_rule_pref))
    print("  without 施行規則:", len(missing_without_rule_pref))
    print("  only 施行規則:", len(missing_only_rule_pref))
    print("  only 条例:", len(missing_only_ordinance_pref))
    print()
    print("Missing municipalities without prefecture (CSV only) by rule presence:")
    print("  with 施行規則:", len(missing_with_rule_no_pref))
    print("  without 施行規則:", len(missing_without_rule_no_pref))
    print("  only 施行規則:", len(missing_only_rule_no_pref))
    print("  only 条例:", len(missing_only_ordinance_no_pref))
    print()
    print("Duplicate municipality names in DB across prefectures:", len(dup_names))
    if dup_names:
        print("  Examples:", ", ".join(dup_names[:10]))
    print()
    if missing_with_pref:
        formatted = [f"{pref}{muni}" for pref, muni in missing_with_pref[:20]]
        print("CSV-only with prefecture (first 20):", ", ".join(formatted))
    if missing_without_pref:
        print("CSV-only without prefecture (first 20):", ", ".join(missing_without_pref[:20]))
    if db_only_by_name:
        formatted = [f"{pref}{muni}" for pref, muni in db_only_by_name[:20]]
        print("DB-only by name (first 20):", ", ".join(formatted))


if __name__ == "__main__":
    main()
