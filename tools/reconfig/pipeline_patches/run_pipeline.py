import subprocess
import os
import sys
import argparse
from datetime import datetime

# def run_script(script_name, *args):
#     """Run a Python script with optional arguments and handle errors."""
#     print(f"\n Running {script_name} {' '.join(args)} ...")
#     result = subprocess.run([sys.executable, script_name, *args], capture_output=True, text=True)
#     if result.returncode != 0:
#         print(f" Error in {script_name}:")
#         print(result.stderr)
#         sys.exit(result.returncode)
#     else:
#         print(f"{script_name} completed successfully.")
def run_script(script_name, *args):
    """Run a Python script with optional arguments and handle errors."""
    print(f"\n▶ Running {script_name} {' '.join(args)} ...")
    
    # Don't capture output - let it print directly
    result = subprocess.run(
        [sys.executable, script_name, *args]
    )
    
    if result.returncode != 0:
        print(f" Error in {script_name}: Exit code {result.returncode}")
        sys.exit(result.returncode)
    else:
        print(f" {script_name} completed successfully.")
        
def main():
    parser = argparse.ArgumentParser(
        description="Run the full SniperSim data pipeline from perf & power CSVs to training datasets."
    )
    parser.add_argument("--perf_csv", required=True, help="Path to the performance input CSV file.")
    parser.add_argument("--power_csv", required=True, help="Path to the power input CSV file.")
    parser.add_argument("--benchmark", required=True, help="Name of the benchmark (used for output file naming).")
    parser.add_argument("--metric", default="ppw", choices=["ppw", "ppw_new", "ips", "power"],
                        help="Objective the best-config and top-3 stages optimise. "
                             "ppw = ips^3/power (original), ppw_new = ips/power, "
                             "ips = throughput only, power = watts only (minimised). "
                             "Non-default metrics suffix the Stage 2/3 outputs so runs "
                             "under different objectives do not overwrite each other.")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # --- File paths ---
    parsed_perf = os.path.join(base_dir, f"parsed_perf_{args.benchmark}.csv")
    cleaned_perf = os.path.join(base_dir, f"cleaned_perf_{args.benchmark}.csv")
    merged_perf_power = os.path.join(base_dir, f"merged_perf_power_{args.benchmark}.csv")
    merged_full = os.path.join(base_dir, f"merged_full_{args.benchmark}.csv")
    msuffix = "" if args.metric == "ppw" else f"_{args.metric}"
    best_configs = os.path.join(base_dir, f"best_configs_{args.benchmark}{msuffix}.csv")
    training_data_lagged = os.path.join(base_dir, f"training_data_lagged_{args.benchmark}.csv")
    training_data_complete = os.path.join(base_dir, f"training_data_complete_{args.benchmark}{msuffix}.csv")
    top3_configs = os.path.join(base_dir, f"top3_configs_{args.benchmark}{msuffix}.csv")
    train_with_top3 = os.path.join(base_dir, f"train_with_top3_{args.benchmark}{msuffix}.csv")

    # --- Stage 1: Preprocessing & Merging ---
    run_script("parseInputConfigsOnPerf.py", args.perf_csv, parsed_perf)
    run_script("deleteEmptyRows.py", parsed_perf, cleaned_perf)
    run_script("mergePerfAndPower.py", cleaned_perf, args.power_csv, merged_perf_power)
    run_script("addCalculatedColumnsToMergedCsv.py", merged_perf_power, merged_full)

    # --- Stage 2: Best Config & Training Data ---
    run_script("findBestConfigUsingPPW.py", merged_full, best_configs, "--metric", args.metric)
    run_script("createTrainingDataLagged.py", merged_full, training_data_lagged)
    run_script("createTrainingDataWithLabels.py", training_data_lagged, best_configs, training_data_complete)

    # --- Stage 3: Top 3 Configs Path ---
    run_script("findTop3ConfigsByPPW.py", merged_full, top3_configs, "--metric", args.metric)
    run_script("mergeTrainWithTop3.py", training_data_lagged, top3_configs, train_with_top3)

    print("\n Pipeline finished successfully!")
    print(" Final outputs:")
    print(f"    {training_data_complete}  Training data with BEST config")
    print(f"    {train_with_top3}         Training data with TOP 3 configs")

if __name__ == "__main__":
    main()
