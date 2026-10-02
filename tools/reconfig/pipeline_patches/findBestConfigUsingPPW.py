import pandas as pd
import re

import objective_metric

def find_best_config_per_interval(
    input_csv: str,
    output_csv: str,
    tolerance: int = 100,
    metric: str = 'ppw'
):
    # Load CSV
    df = pd.read_csv(input_csv)

    # Columns we need, with flexible (case/separator-insensitive) name matching
    # to handle either naming convention (space-separated from parse_benchmark_data.py,
    # or camelCase from collect_all_power.py) depending on what survived the merge.
    wanted_cols = {
        'l2_core0': 'L2 core 0',
        'l2_core1': 'L2 core 1',
        'l3': 'L3',
        'prefetch_core0': 'Prefetch core 0',
        'prefetch_core1': 'Prefetch core 1',
        'btb_core0': 'BTB core 0',
        'btb_core1': 'BTB core 1',
        'period_start': 'period_start',
        'period_end': 'period_end',
    }

    def find_col(expected_name):
        norm = lambda s: s.lower().replace('.', '').replace('_', '').replace(' ', '')
        for col in df.columns:
            if norm(col) == norm(expected_name):
                return col
        for col in df.columns:
            if norm(expected_name) in norm(col):
                return col
        return None

    col_mapping = {}
    missing = []
    for key, expected_name in wanted_cols.items():
        found = find_col(expected_name)
        if found:
            col_mapping[key] = found
        else:
            missing.append(expected_name)
    if missing:
        raise ValueError(
            f"Missing required column(s): {missing}. Available columns: {list(df.columns)}"
        )

    # Parse "periodicins-12345" -> 12345
    def parse_ins(val):
        if isinstance(val, str):
            m = re.search(r"periodicins-(\d+)", val)
            return int(m.group(1)) if m else None
        return None

    df["start_num"] = df[col_mapping['period_start']].apply(parse_ins)
    df["end_num"] = df[col_mapping['period_end']].apply(parse_ins)

    # Drop invalid rows
    # Higher is always better in OBJ, including for `power`, which is negated.
    OBJ, RAW, _higher = objective_metric.add_objective(df, metric)
    df = df.dropna(subset=["start_num", "end_num", OBJ])

    # Sort for grouping stability
    df = df.sort_values(by=["start_num", "end_num"]).reset_index(drop=True)

    # Group rows by interval (within tolerance)
    groups = []
    for _, row in df.iterrows():
        start, end = row["start_num"], row["end_num"]
        found = False
        for group in groups:
            if abs(group["start"] - start) <= tolerance and abs(group["end"] - end) <= tolerance:
                group["rows"].append(row)
                found = True
                break
        if not found:
            groups.append({"start": start, "end": end, "rows": [row]})

    # Find best config per group (max PPW)
    output_rows = []
    for group in groups:
        group_df = pd.DataFrame(group["rows"])
        best_row = group_df.loc[group_df[OBJ].idxmax()]

        output = {
            "interval_start": group["start"],
            "interval_end": group["end"],
            "L2 core 0": best_row[col_mapping['l2_core0']],
            "L2 core 1": best_row[col_mapping['l2_core1']],
            "L3": best_row[col_mapping['l3']],
            "Prefetch core 0": best_row[col_mapping['prefetch_core0']],
            "Prefetch core 1": best_row[col_mapping['prefetch_core1']],
            "BTB core 0": best_row[col_mapping['btb_core0']],
            "BTB core 1": best_row[col_mapping['btb_core1']],
            "config": '_'.join(str(best_row[col_mapping[k]]) for k in [
                'l2_core0', 'l2_core1', 'l3', 'prefetch_core0', 'prefetch_core1', 'btb_core0', 'btb_core1'
            ]),
            # Column kept as "PPW" so downstream stages keep working; it holds the
            # value of whichever metric was selected -- see the "metric" column.
            "PPW": best_row[RAW],
            "metric": metric,
        }
        output_rows.append(output)

    # Create output dataframe
    result_df = pd.DataFrame(output_rows)

    # Save to output CSV
    result_df.to_csv(output_csv, index=False)
    print(f"Best configurations saved to {output_csv}")


if __name__ == "__main__":
    import sys
    metric, sys.argv = objective_metric.metric_from_argv(sys.argv)
    if len(sys.argv) >= 3:
        input_csv = sys.argv[1]
        output_csv = sys.argv[2]
        tolerance = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        print("objective metric: %s" % metric)
        find_best_config_per_interval(input_csv, output_csv, tolerance=tolerance, metric=metric)
    else:
        print("Usage: python script.py <input_file.csv> <output_file.csv> [tolerance] "
              "[--metric ppw|ppw_new|ips|power]")
