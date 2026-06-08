from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from src.predictor import load_troca_dates


def load_history(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True)
    df["pred_rul"] = pd.to_numeric(df["pred_rul"], errors="coerce")
    df["target_rul"] = pd.to_numeric(df["target_rul"], errors="coerce")
    return df.sort_values("Timestamp").reset_index(drop=True)


def compute_overall_metrics(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    valid = df.dropna(subset=["pred_rul", "target_rul"]).copy()
    valid["error_h"] = valid["pred_rul"] - valid["target_rul"]
    valid["abs_error_h"] = valid["error_h"].abs()
    valid["ape"] = valid["abs_error_h"] / valid["target_rul"].replace(0, np.nan)

    metrics = {
        "rows": int(len(valid)),
        "mae_h": float(valid["abs_error_h"].mean()),
        "rmse_h": float(np.sqrt((valid["error_h"] ** 2).mean())),
        "bias_h": float(valid["error_h"].mean()),
        "median_abs_error_h": float(valid["abs_error_h"].median()),
        "mape_pct": float(valid["ape"].mean() * 100),
        # corr exige ≥2 linhas válidas; com menos, .corr() produz NaN
        "corr": (
            float(valid[["pred_rul", "target_rul"]].corr().iloc[0, 1])
            if len(valid) >= 2 else float("nan")
        ),
    }
    return valid, metrics


def compute_band_metrics(valid: pd.DataFrame) -> pd.DataFrame:
    bins = [0, 24, 72, 168, 336, float("inf")]
    labels = ["0-24h", "24-72h", "72-168h", "168-336h", "336h+"]
    out = valid.copy()
    out["target_band"] = pd.cut(
        out["target_rul"], bins=bins, labels=labels, right=False
    )
    return (
        out.groupby("target_band", observed=True)
        .agg(
            rows=("target_rul", "size"),
            mae_h=("abs_error_h", "mean"),
            bias_h=("error_h", "mean"),
            median_abs_error_h=("abs_error_h", "median"),
        )
        .reset_index()
    )


def compute_swap_level_eval(
    df: pd.DataFrame, swap_dates: list[pd.Timestamp]
) -> tuple[pd.DataFrame, dict[str, float]]:
    records: list[dict[str, object]] = []
    for swap_dt in swap_dates:
        prev = df[df["Timestamp"] < swap_dt].tail(1)
        if prev.empty:
            continue
        row = prev.iloc[0]
        actual_h = (swap_dt - row["Timestamp"]).total_seconds() / 3600
        pred_h = row["pred_rul"]
        records.append(
            {
                "swap_dt": swap_dt,
                "ts_prev": row["Timestamp"],
                "actual_h_until_swap": actual_h,
                "pred_h_until_swap": pred_h,
                "error_h": pred_h - actual_h if pd.notna(pred_h) else np.nan,
                "abs_error_h": abs(pred_h - actual_h) if pd.notna(pred_h) else np.nan,
                "evento_troca": row.get("evento_troca", ""),
            }
        )

    swap_eval = pd.DataFrame(records)
    metrics = {
        "rows": int(len(swap_eval)),
        "mae_h": float(swap_eval["abs_error_h"].mean()),
        "rmse_h": float(np.sqrt((swap_eval["error_h"] ** 2).mean())),
        "bias_h": float(swap_eval["error_h"].mean()),
        "median_abs_error_h": float(swap_eval["abs_error_h"].median()),
    }
    return swap_eval, metrics


def compute_threshold_metrics(
    swap_eval: pd.DataFrame, thresholds: list[int]
) -> pd.DataFrame:
    rows = []
    for thr in thresholds:
        y_true = swap_eval["actual_h_until_swap"] <= thr
        y_pred = swap_eval["pred_h_until_swap"] <= thr
        tp = int((y_true & y_pred).sum())
        tn = int((~y_true & ~y_pred).sum())
        fp = int((~y_true & y_pred).sum())
        fn = int((y_true & ~y_pred).sum())
        precision = tp / (tp + fp) if (tp + fp) else np.nan
        recall = tp / (tp + fn) if (tp + fn) else np.nan
        rows.append(
            {
                "threshold_h": thr,
                "tp": tp,
                "tn": tn,
                "fp": fp,
                "fn": fn,
                "precision": precision,
                "recall": recall,
            }
        )
    return pd.DataFrame(rows)


def print_metrics(title: str, metrics: dict[str, float]) -> None:
    print(title)
    for key, value in metrics.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.2f}")
        else:
            print(f"  {key}: {value}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida pred_rul contra as trocas confirmadas."
    )
    parser.add_argument(
        "--history",
        default="notebooks/hour_prev_com_eventos.csv",
        help="CSV historico com pred_rul e target_rul.",
    )
    parser.add_argument(
        "--trocas",
        default="notebooks/troca_modulo.csv",
        help="CSV com datas confirmadas de troca.",
    )
    parser.add_argument(
        "--output-dir",
        default="notebooks",
        help="Diretorio para exportar os CSVs de validacao.",
    )
    args = parser.parse_args()

    history_path = Path(args.history)
    trocas_path = Path(args.trocas)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    history = load_history(history_path)
    swap_dates = load_troca_dates(trocas_path)

    valid, overall = compute_overall_metrics(history)
    band_metrics = compute_band_metrics(valid)
    swap_eval, swap_metrics = compute_swap_level_eval(history, swap_dates)
    threshold_metrics = compute_threshold_metrics(
        swap_eval, thresholds=[24, 72, 120, 168]
    )

    print_metrics("Overall historical metrics", overall)
    print()
    print("Metrics by actual RUL band")
    print(band_metrics.round(2).to_string(index=False))
    print()
    print_metrics("Metrics at confirmed swap boundaries", swap_metrics)
    print()
    print("Threshold metrics at swap boundaries")
    print(threshold_metrics.round(2).to_string(index=False))

    summary = pd.DataFrame(
        [
            {"scope": "overall", **overall},
            {"scope": "swap_boundary", **swap_metrics},
        ]
    )
    summary.to_csv(output_dir / "pred_rul_validation_summary.csv", index=False)
    band_metrics.to_csv(output_dir / "pred_rul_validation_by_band.csv", index=False)
    swap_eval.to_csv(output_dir / "pred_rul_validation_swaps.csv", index=False)
    threshold_metrics.to_csv(
        output_dir / "pred_rul_validation_thresholds.csv", index=False
    )


if __name__ == "__main__":
    main()
