#!/usr/bin/env python3
import argparse
import csv
import os
import re
import sys
import unicodedata
import zlib
from difflib import SequenceMatcher

try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz
except Exception:
    rapidfuzz_fuzz = None


def normalize_text(text, strip_punct=False):
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\s+", " ", value).strip()
    if strip_punct:
        value = re.sub(r"[、。・,.;:!?「」『』（）()［］\[\]{}<>]", "", value)
    return value


def adler32_hash(value):
    return zlib.adler32(value.encode("utf-8")) & 0xFFFFFFFF


def minhash_signature(text, ngram_size, signature_size):
    if not text:
        return [0] * signature_size
    if len(text) < ngram_size:
        return [adler32_hash(text)] * signature_size
    grams = {text[i : i + ngram_size] for i in range(len(text) - ngram_size + 1)}
    hashes = sorted(adler32_hash(g) for g in grams)
    if not hashes:
        return [0] * signature_size
    if len(hashes) < signature_size:
        repeats = (signature_size + len(hashes) - 1) // len(hashes)
        hashes = (hashes * repeats)[:signature_size]
        return hashes
    return hashes[:signature_size]


def similarity_ratio(a, b):
    if rapidfuzz_fuzz is not None:
        return rapidfuzz_fuzz.ratio(a, b) / 100.0
    return SequenceMatcher(None, a, b).ratio()


def load_rows(path, encoding=None):
    encodings = [encoding] if encoding else ["utf-8-sig", "utf-8", "cp932"]
    last_error = None
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, newline="") as handle:
                reader = csv.DictReader(handle)
                rows = []
                for line_no, row in enumerate(reader, start=2):
                    rows.append((line_no, row))
                return reader.fieldnames, rows, enc
        except UnicodeDecodeError as exc:
            last_error = exc
    raise last_error


def truncate_text(text, max_len):
    if max_len is None or max_len <= 0:
        return text
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def build_buckets(signatures, lengths, band_size, length_bucket):
    buckets = {}
    for idx, sig in enumerate(signatures):
        bucket = lengths[idx] // length_bucket if length_bucket else 0
        for offset in range(0, len(sig), band_size):
            band = sig[offset : offset + band_size]
            if len(band) < band_size:
                continue
            key = (bucket, offset, tuple(band))
            buckets.setdefault(key, []).append(idx)
    return buckets


def parse_args():
    parser = argparse.ArgumentParser(
        description="Check duplicate and similar text entries in a CSV column."
    )
    parser.add_argument("csv_path", help="Path to the CSV file.")
    parser.add_argument("--column", default="本文", help="Column name to compare.")
    parser.add_argument("--encoding", default=None, help="CSV encoding override.")
    parser.add_argument(
        "--output-dir", default="output_similarity", help="Directory for results."
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=0.9,
        help="Similarity threshold (0-1).",
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=20,
        help="Minimum text length for similarity checks.",
    )
    parser.add_argument(
        "--length-delta",
        type=float,
        default=0.3,
        help="Max relative length difference for candidate pairs.",
    )
    parser.add_argument(
        "--ngram",
        type=int,
        default=3,
        help="N-gram size used for signatures.",
    )
    parser.add_argument(
        "--sig-size",
        type=int,
        default=12,
        help="Number of minhash values per signature.",
    )
    parser.add_argument(
        "--band-size",
        type=int,
        default=4,
        help="Band size for LSH-style bucketing.",
    )
    parser.add_argument(
        "--length-bucket",
        type=int,
        default=50,
        help="Length bucket size for candidate grouping.",
    )
    parser.add_argument(
        "--max-block-size",
        type=int,
        default=2000,
        help="Skip buckets larger than this size.",
    )
    parser.add_argument(
        "--strip-punct",
        action="store_true",
        help="Remove common punctuation before comparison.",
    )
    parser.add_argument(
        "--text-max-len",
        type=int,
        default=200,
        help="Max text length in outputs (0 for full text).",
    )
    parser.add_argument(
        "--include-identical",
        action="store_true",
        help="Include identical texts in similarity output.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    fieldnames, rows, encoding = load_rows(args.csv_path, args.encoding)
    if fieldnames is None or args.column not in fieldnames:
        print(f"Missing column: {args.column}", file=sys.stderr)
        sys.exit(1)

    texts = []
    normalized = []
    lengths = []
    row_nums = []
    years = []
    municipalities = []
    categories = []
    for line_no, row in rows:
        raw_text = row.get(args.column, "")
        norm = normalize_text(raw_text, strip_punct=args.strip_punct)
        texts.append(raw_text)
        normalized.append(norm)
        lengths.append(len(norm))
        row_nums.append(line_no)
        years.append(row.get("制定年", ""))
        municipalities.append(row.get("自治体", ""))
        categories.append(row.get("区分", ""))

    exact_map = {}
    for idx, norm in enumerate(normalized):
        if not norm:
            continue
        exact_map.setdefault(norm, []).append(idx)

    exact_path = os.path.join(args.output_dir, "exact_duplicates.csv")
    with open(exact_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "group_id",
                "row_num",
                "制定年",
                "自治体",
                "区分",
                "text",
                "normalized_text",
                "count_in_group",
            ]
        )
        group_id = 0
        for norm_text, idxs in exact_map.items():
            if len(idxs) < 2:
                continue
            group_id += 1
            for idx in idxs:
                writer.writerow(
                    [
                        group_id,
                        row_nums[idx],
                        years[idx],
                        municipalities[idx],
                        categories[idx],
                        truncate_text(texts[idx], args.text_max_len),
                        truncate_text(norm_text, args.text_max_len),
                        len(idxs),
                    ]
                )

    signatures = [
        minhash_signature(norm, args.ngram, args.sig_size) for norm in normalized
    ]
    buckets = build_buckets(
        signatures, lengths, args.band_size, args.length_bucket
    )

    similar_path = os.path.join(args.output_dir, "similar_pairs.csv")
    compare_count = 0
    similar_count = 0
    seen_pairs = set()
    with open(similar_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "row_num_a",
                "row_num_b",
                "similarity",
                "制定年_a",
                "自治体_a",
                "区分_a",
                "text_a",
                "制定年_b",
                "自治体_b",
                "区分_b",
                "text_b",
            ]
        )
        for idxs in buckets.values():
            if len(idxs) < 2:
                continue
            if args.max_block_size and len(idxs) > args.max_block_size:
                continue
            for i in range(len(idxs)):
                idx_a = idxs[i]
                if lengths[idx_a] < args.min_length:
                    continue
                for j in range(i + 1, len(idxs)):
                    idx_b = idxs[j]
                    if lengths[idx_b] < args.min_length:
                        continue
                    if idx_a < idx_b:
                        pair_id = (idx_a << 32) | idx_b
                    else:
                        pair_id = (idx_b << 32) | idx_a
                    if pair_id in seen_pairs:
                        continue
                    seen_pairs.add(pair_id)
                    if not args.include_identical and normalized[idx_a] == normalized[idx_b]:
                        continue
                    len_max = max(lengths[idx_a], lengths[idx_b])
                    if len_max:
                        delta = abs(lengths[idx_a] - lengths[idx_b]) / len_max
                        if args.length_delta and delta > args.length_delta:
                            continue
                    compare_count += 1
                    score = similarity_ratio(normalized[idx_a], normalized[idx_b])
                    if score < args.similarity_threshold:
                        continue
                    similar_count += 1
                    writer.writerow(
                        [
                            row_nums[idx_a],
                            row_nums[idx_b],
                            f"{score:.3f}",
                            years[idx_a],
                            municipalities[idx_a],
                            categories[idx_a],
                            truncate_text(texts[idx_a], args.text_max_len),
                            years[idx_b],
                            municipalities[idx_b],
                            categories[idx_b],
                            truncate_text(texts[idx_b], args.text_max_len),
                        ]
                    )

    print(
        "Loaded rows: {rows}, encoding: {enc}".format(
            rows=len(rows), enc=encoding
        )
    )
    print("Exact duplicate groups written to:", exact_path)
    print(
        "Similarity comparisons: {count}, matches: {matches}".format(
            count=compare_count, matches=similar_count
        )
    )
    print("Similar pairs written to:", similar_path)


if __name__ == "__main__":
    main()
