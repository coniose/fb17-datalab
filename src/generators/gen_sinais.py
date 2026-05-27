"""
Generator: 02_sinais_forca.csv

A partir de 00_hour_prev.csv, calcula features de força v1 e v2
que alimentam o trigger_engine.py.

Features v1  : slope_Media_7d, pct_abaixo_800N_7d, min_forca_3d, cv_Delta_AB_7d
Features v2  : mean_3d, mean_14d, ratio_3_14, deg_signal, signal_score,
               proj_48h, age_risk, p_risk

Todas as janelas rolantes são cycle-aware: reiniciam no início de cada ciclo
para não cruzar o limite de uma troca de rolo.

Substitui a lógica de notebooks/02_sinais_forca.ipynb para execução em produção.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from src.connector import load_config
from src.predictor import load_troca_dates
from src.sku_normalizer import normalizar_media_phantom

_ROOT = Path(__file__).parent.parent.parent

COLUNAS_EXPORT = [
    "Timestamp", "ciclo_id", "horas_desde_troca",
    "Forca_A", "Forca_B", "Media", "Media_norm", "Delta_AB",
    "phantom_codigo", "phantom_fator",
    # v1
    "slope_Media_7d", "pct_abaixo_800N_7d", "min_forca_3d", "cv_Delta_AB_7d",
    # v2
    "mean_3d", "mean_14d", "ratio_3_14", "deg_signal",
    "signal_score", "proj_48h", "age_risk", "p_risk",
]

_MIN_PERIODS_SLOPE = 4   # pontos mínimos para calcular slope


def _atribuir_ciclos(df: pd.DataFrame, troca_dates: list) -> pd.DataFrame:
    """Adiciona horas_desde_troca e ciclo_id a partir das datas de troca."""
    df = df.copy()
    df["horas_desde_troca"] = np.nan
    df["ciclo_id"] = -1
    limites = troca_dates + [pd.Timestamp.max.tz_localize("UTC")]
    for i, (t_ini, t_fim) in enumerate(zip(limites[:-1], limites[1:])):
        mask = (df["ts"] >= t_ini) & (df["ts"] < t_fim)
        if not mask.any():
            continue
        df.loc[mask, "horas_desde_troca"] = (
            (df.loc[mask, "ts"] - t_ini).dt.total_seconds() / 3600
        ).values
        df.loc[mask, "ciclo_id"] = i
    return df


def _slope_series(s_indexed: pd.Series, window: str = "7D") -> pd.Series:
    """Regressão linear em janela rolante — retorna inclinação em N/dia."""
    def _slope_fn(w):
        if len(w) < _MIN_PERIODS_SLOPE:
            return np.nan
        t = (w.index - w.index[0]).total_seconds().values / 86400.0
        if t[-1] < 0.5:          # janela < 12h → inclinação instável
            return np.nan
        return np.polyfit(t, w.values, 1)[0]

    return s_indexed.rolling(window, min_periods=_MIN_PERIODS_SLOPE).apply(
        _slope_fn, raw=False
    )


def _calcular_features_ciclo(
    df: pd.DataFrame,
    ciclo_id: int,
    slope_arr: np.ndarray,
    pct_arr: np.ndarray,
    min3d_arr: np.ndarray,
    cv_dab_arr: np.ndarray,
    mean3_arr: np.ndarray,
    mean14_arr: np.ndarray,
    forca_limiar: float,
    media_col: str = "Media",
) -> None:
    """Preenche os arrays de features para um único ciclo (in-place)."""
    mask = df["ciclo_id"] == ciclo_id
    sub = df.loc[mask]
    if sub[media_col].notna().sum() < 2:
        return

    idx = mask.values.nonzero()[0]
    media_s = pd.Series(sub[media_col].values, index=sub["ts"])
    dab_s   = pd.Series(sub["Delta_AB"].values, index=sub["ts"])
    below_s = (media_s < forca_limiar).astype(float)

    # v1
    slope_arr[idx]  = _slope_series(media_s).values
    pct_arr[idx]    = below_s.rolling("7D", min_periods=1).mean().values
    min3d_arr[idx]  = media_s.rolling("3D", min_periods=1).min().values

    r_std  = dab_s.rolling("7D", min_periods=4).std()
    r_mean = dab_s.rolling("7D", min_periods=4).mean()
    cv_dab_arr[idx] = np.where(r_mean > 0, r_std / r_mean, np.nan)

    # v2 — médias rolantes
    mean3_arr[idx]  = media_s.rolling("3D",  min_periods=1).mean().values
    mean14_arr[idx] = media_s.rolling("14D", min_periods=1).mean().values


def run(
    input_path: str | Path = _ROOT / "notebooks" / "00_hour_prev.csv",
    output_path: str | Path = _ROOT / "notebooks" / "02_sinais_forca.csv",
    config_path: str | Path | None = None,
    troca_csv: str | Path | None = None,
    phantom_csv: str | Path | None = None,
) -> pd.DataFrame:
    """
    Calcula features de força v1 e v2 a partir de 00_hour_prev.csv.

    Args:
        input_path:  Caminho para 00_hour_prev.csv.
        output_path: Destino de 02_sinais_forca.csv.
        config_path: Caminho para config.yaml (usa padrão do projeto se None).
        troca_csv:   Caminho para troca_modulo.csv (busca automática se None).
        phantom_csv: Caminho para sku_dates.csv com phantom codes.
                     Se None, tenta notebooks/sku_dates.csv automaticamente.
                     Se não existir, Media_norm não é gerada.

    Returns:
        DataFrame exportado (sem index).
    """
    cfg = load_config(config_path)
    trig = cfg.get("trigger", {})

    weibull_beta = trig.get("weibull_beta", 1.181)
    weibull_eta_d = trig.get("weibull_eta_h", 1297.0) / 24.0
    boost_sinal   = trig.get("boost_sinal", 0.65)
    forca_limiar  = trig.get("forca_min_emergencia", 800.0)

    # 1. Carregar e preparar
    df = pd.read_csv(input_path, parse_dates=["Timestamp"])
    df = df.rename(columns={"Timestamp": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    # Recalcular Media e Delta_AB a partir dos sinais brutos
    df["Media"]    = (df["Forca_A"] + df["Forca_B"]) / 2.0
    df["Delta_AB"] = (df["Forca_A"] - df["Forca_B"]).abs()

    troca_dates = load_troca_dates(troca_csv)
    df = _atribuir_ciclos(df, troca_dates)

    # 2. Normalização por phantom (auto-calibrada se sku_dates.csv disponível)
    _phantom_path = Path(phantom_csv) if phantom_csv else _ROOT / "notebooks" / "sku_dates.csv"
    if _phantom_path.exists():
        df_phantom = pd.read_csv(_phantom_path)
        df_phantom["index"] = pd.to_datetime(df_phantom["index"], utc=True)
        df = normalizar_media_phantom(df, df_phantom, troca_dates, col_phantom="phantom")
    else:
        df["Media_norm"]     = df["Media"]
        df["phantom_codigo"] = None
        df["phantom_fator"]  = 1.0

    # Usar Media_norm nas features se disponível e com cobertura >= 50%
    _media_col = "Media_norm" if (
        "Media_norm" in df.columns
        and df["Media_norm"].notna().mean() >= 0.5
    ) else "Media"

    # 3. Arrays de destino
    n = len(df)
    slope_arr  = np.full(n, np.nan)
    pct_arr    = np.full(n, np.nan)
    min3d_arr  = np.full(n, np.nan)
    cv_dab_arr = np.full(n, np.nan)
    mean3_arr  = np.full(n, np.nan)
    mean14_arr = np.full(n, np.nan)

    # 4. Features cycle-aware (loop por ciclo)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for cid in sorted(df["ciclo_id"].unique()):
            if cid < 0:
                continue
            _calcular_features_ciclo(
                df, cid,
                slope_arr, pct_arr, min3d_arr, cv_dab_arr,
                mean3_arr, mean14_arr,
                forca_limiar,
                media_col=_media_col,
            )

    df["slope_Media_7d"]     = slope_arr
    df["pct_abaixo_800N_7d"] = pct_arr
    df["min_forca_3d"]       = min3d_arr
    df["cv_Delta_AB_7d"]     = cv_dab_arr
    df["mean_3d"]            = mean3_arr
    df["mean_14d"]           = mean14_arr

    # 5. Features v2 — vetorizadas
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio    = np.where(mean14_arr > 0, mean3_arr / mean14_arr, np.nan)
        deg      = np.clip(1.0 - ratio, 0.0, None)
        danger   = np.where(
            np.isnan(slope_arr), 0.0,
            np.clip(-slope_arr / 50.0, 0.0, 1.0)
        )
        sig_score = deg * 0.6 + danger * 0.4
        proj_48h  = mean3_arr + np.where(np.isnan(slope_arr), 0.0, slope_arr) * 2.0

    age_h   = df["horas_desde_troca"].values
    valid   = (df["ciclo_id"].values >= 0) & np.isfinite(age_h)
    age_d   = np.where(valid & (age_h > 0), age_h / 24.0, 0.0)
    age_risk = np.where(
        valid,
        1.0 - np.exp(-((age_d / weibull_eta_d) ** weibull_beta)),
        np.nan,
    )
    p_risk = np.where(
        valid & np.isfinite(sig_score),
        age_risk + (1.0 - age_risk) * np.nan_to_num(sig_score) * boost_sinal,
        np.nan,
    )

    df["ratio_3_14"]   = ratio
    df["deg_signal"]   = deg
    df["signal_score"] = np.where(valid, sig_score, np.nan)
    df["proj_48h"]     = np.where(valid, proj_48h, np.nan)
    df["age_risk"]     = age_risk
    df["p_risk"]       = p_risk

    # 6. Exportar
    df = df.rename(columns={"ts": "Timestamp"})
    cols = [c for c in COLUNAS_EXPORT if c in df.columns]
    out = df[cols]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_path, index=False)

    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gera 02_sinais_forca.csv")
    parser.add_argument("--input",  default=str(_ROOT / "notebooks" / "00_hour_prev.csv"))
    parser.add_argument("--output", default=str(_ROOT / "notebooks" / "02_sinais_forca.csv"))
    parser.add_argument("--config", default=None)
    parser.add_argument("--troca-csv", default=None)
    parser.add_argument("--phantom-csv", default=None,
                        help="sku_dates.csv com phantom codes (auto-detectado se omitido)")
    args = parser.parse_args()

    result = run(
        input_path=args.input,
        output_path=args.output,
        config_path=args.config,
        troca_csv=args.troca_csv,
        phantom_csv=args.phantom_csv,
    )
    print(f"Salvo: {args.output}  ({len(result):,} linhas)")
