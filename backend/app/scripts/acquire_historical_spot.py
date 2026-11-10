"""
TradeLens AI - Command-Line Interface for Historical NIFTY Spot Data Acquisition.

Usage:
  # Dry-run inspection mode (no network download):
  python3 -m backend.app.scripts.acquire_historical_spot --instrument NIFTY --interval 1m --start-date 2026-11-02 --end-date 2026-11-06 --dry-run

  # Live acquisition execution mode:
  python3 -m backend.app.scripts.acquire_historical_spot --instrument NIFTY --interval 1m --start-date 2026-11-02 --end-date 2026-11-06
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from datetime import datetime, date

from backend.app.data.historical_spot_store import HistoricalSpotStore
from backend.app.data.historical_acquisition import (
    AngelOneHistoricalSpotProvider,
    HistoricalAcquisitionService
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Acquire and validate genuine historical NIFTY spot index 1m OHLC data."
    )
    parser.add_argument(
        "--instrument",
        type=str,
        default="NIFTY",
        help="Target index instrument (e.g. NIFTY, BANKNIFTY). Default: NIFTY."
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="1m",
        choices=["1m", "1min", "ONE_MINUTE"],
        help="Candle interval. Only 1m is supported for spot base data."
    )
    parser.add_argument(
        "--start-date",
        type=str,
        required=True,
        help="Start date in YYYY-MM-DD format (e.g. 2026-11-02)."
    )
    parser.add_argument(
        "--end-date",
        type=str,
        required=True,
        help="End date in YYYY-MM-DD format (e.g. 2026-11-06)."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/historical",
        help="Output storage root directory for Phase 3B partitions."
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=str,
        default="data/checkpoints",
        help="Checkpoint storage directory for resumable acquisition."
    )
    parser.add_argument(
        "--storage-format",
        type=str,
        default="csv",
        choices=["parquet", "csv"],
        help="Storage format: parquet (if pyarrow available) or csv fallback."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Performs calculation and pre-flight planning without executing downloads."
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=15,
        help="Maximum chunk size in days to prevent provider silent truncation. Default: 15."
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        start_d = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    except ValueError as e:
        print(f"[!] Error: Invalid date format: {e}. Expected YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)

    if start_d > end_d:
        print(f"[!] Error: start-date ({start_d}) cannot be later than end-date ({end_d}).", file=sys.stderr)
        sys.exit(1)

    # Initialize provider and store
    provider = AngelOneHistoricalSpotProvider(max_chunk_days=args.chunk_days)
    store = HistoricalSpotStore(base_dir=args.output_dir, storage_format=args.storage_format)
    service = HistoricalAcquisitionService(
        provider=provider,
        store=store,
        checkpoint_dir=args.checkpoint_dir
    )

    if args.dry_run:
        print("================================================================================")
        print("TRADELENS AI - HISTORICAL DATA ACQUISITION DRY RUN (PRE-FLIGHT)")
        print("================================================================================")
        plan = service.dry_run(
            symbol=args.instrument,
            start_date=start_d,
            end_date=end_d,
            chunk_days=args.chunk_days
        )
        print(json.dumps(plan, indent=2))
        print("--------------------------------------------------------------------------------")
        print("[✔] Dry run completed. To execute live acquisition, re-run without --dry-run.")
        sys.exit(0)

    print("================================================================================")
    print("TRADELENS AI - HISTORICAL DATA ACQUISITION EXECUTION")
    print("================================================================================")
    print(f"Target Instrument : {args.instrument}")
    print(f"Interval          : {args.interval}")
    print(f"Date Range        : {start_d} to {end_d}")
    print(f"Storage Format    : {args.storage_format}")
    print(f"Output Directory  : {args.output_dir}")
    print("--------------------------------------------------------------------------------")

    result = service.execute_acquisition(
        symbol=args.instrument,
        start_date=start_d,
        end_date=end_d,
        chunk_days=args.chunk_days
    )

    print("================================================================================")
    print("ACQUISITION RESULT SUMMARY")
    print("================================================================================")
    print(f"Status              : {result['status']}")
    print(f"Chunks Acquired     : {result['chunks_acquired']} / {result['chunks_total']}")
    print(f"Chunks Failed       : {result['chunks_failed']}")
    cov = result["coverage_validation"]
    print(f"Expected Intervals  : {cov['expected_intervals']}")
    print(f"Valid Candles       : {cov['valid_candles']}")
    print(f"Missing Intervals   : {cov['missing_intervals']}")
    print(f"Coverage Percentage : {cov['coverage_pct']}%")
    print(f"Exact Duplicates    : {cov['exact_duplicates']}")
    print(f"Conflicting Dups    : {cov['conflicting_duplicates']}")
    if "provider_cutoff_metadata" in cov:
        pcm = cov["provider_cutoff_metadata"]
        print("--------------------------------------------------------------------------------")
        print("PROVIDER CUTOFF METADATA (PHASE 3E.1):")
        print(f"Cutoff Observed     : {pcm.get('PROVIDER_CUTOFF_OBSERVED')}")
        print(f"Last Candle Start   : {pcm.get('LAST_VALIDATED_CANDLE_START')}")
        print(f"Last Candle End     : {pcm.get('LAST_VALIDATED_CANDLE_END')}")
        print(f"Exchange Close      : {pcm.get('EXCHANGE_SESSION_CLOSE')}")
        print(f"Cutoff Cause        : {pcm.get('PROVIDER_CUTOFF_CAUSE')} (Verified: {pcm.get('PROVIDER_CUTOFF_CAUSE_VERIFIED')})")
    print("--------------------------------------------------------------------------------")
    if result.get("storage_manifest"):
        m = result["storage_manifest"]
        print(f"Dataset ID          : {m['dataset_id']}")
        print(f"Raw Source SHA256   : {m['source_raw_sha256'][:16]}...")
        print(f"Canonical SHA256    : {m['canonical_content_sha256'][:16]}...")
        print(f"Partitions Written  : {len(m['partition_paths'])}")
    print("================================================================================")


if __name__ == "__main__":
    main()

