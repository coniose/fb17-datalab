"""
Generator: 01_vida_rul.csv + 01_weibull_params.json

A partir de 00_hour_prev.csv, calcula:
  - horas_desde_troca e ciclo_id  (atribuição de ciclo por data de troca)
  - score_weibull = F_weibull(horas_desde_troca)   (CDF, eixo de idade)
  - score_roll7d  = média 7d do score, reiniciada por ciclo (entrada Eixo 1 do trigger)
  - rul_p10 / rul_p50 / rul_p90  (RUL condicional)

Weibull é refitado a cada execução sobre os ciclos genuínos detectados no histórico.
Substitui a lógica de notebooks/01_vida_rul.ipynb para execução em produção.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from src.predictor import load_troca_dates

_ROOT = Path(__file__).parent.parent.parent

COLUNAS_EXPORT = [
    "Timestamp",
    "horas_desde_troca", "ciclo_id",
    "score_weibull", "score_roll7d",
    "rul_p10", "rul_p50", "rul_p90",
]


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
        elapsed = (df.loc[mask, "ts"] - t_ini).dt.total_seconds() / 3600
        df.loc[mask, "horas_desde_troca"] = elapsed.values
        df.loc[mask, "ciclo_id"] = i

    return df


def _detectar_genuinos(df: pd.DataFrame, troca_dates: list, limiar_h: float) -> np.ndarray:
    """
    Retorna array de durações (h) dos ciclos genuínos.
    Genuíno: target_rul < limiar_h na última leitura do ciclo.
    """
    duracoes = []
    for i, (t_ini, t_fim) in enumerate(zip(troca_dates[:-1], troca_dates[1:])):
        ciclo = df[(df["ts"] >= t_ini) & (df["ts"] < t_fim)]
        if ciclo.empty:
            continue
        rul_ultimo = ciclo["rul"].iloc[-1]
        duracao_h = ciclo["horas_desde_troca"].max()
        if pd.notna(rul_ultimo) and rul_ultimo < limiar_h and pd.notna(duracao_h) and duracao_h > 0:
            duracoes.append(duracao_h)
    return np.array(duracoes)


def _rul_condicional(h_arr: np.ndarray, q: float, beta: float, eta: float) -> np.ndarray:
    p_atual = stats.weibull_min.cdf(h_arr, beta, loc=0, scale=eta)
    p_alvo = np.minimum(p_atual + (1 - p_atual) * q, 0.9999)
    t_alvo = stats.weibull_min.ppf(p_alvo, beta, loc=0, scale=eta)
    return np.maximum(0.0, t_alvo - h_arr)


def run(
    input_path: str | Path = _ROOT / "notebooks" / "00_hour_prev.csv",
    output_csv: str | Path = _ROOT / "notebooks" / "01_vida_rul.csv",
    output_json: str | Path = _ROOT / "notebooks" / "01_weibull_params.json",
    troca_csv: str | Path | None = None,
    limiar_genuino_h: float = 20.0,
    window_score: str = "7D",
) -> pd.DataFrame:
    """
    Calcula vida, score Weibull e RUL condicional a partir de 00_hour_prev.csv.

    Args:
        input_path:       Caminho para 00_hour_prev.csv.
        output_csv:       Destino de 01_vida_rul.csv.
        output_json:      Destino de 01_weibull_params.json.
        troca_csv:        Caminho para troca_modulo.csv (busca automática se None).
        limiar_genuino_h: target_rul < N horas na troca → ciclo genuíno (default 20).
        window_score:     Janela de suavização do score (default '7D').

    Returns:
        DataFrame exportado (sem index).
    """
    # 1. Carregar dados
    df = pd.read_csv(input_path, parse_dates=["Timestamp"])
    df = df.rename(columns={"Timestamp": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["rul"] = pd.to_numeric(df.get("target_rul", np.nan), errors="coerce")
    df = df.sort_values("ts").reset_index(drop=True)

    troca_dates = load_troca_dates(troca_csv)

    # 2. Atribuir ciclo e horas_desde_troca
    df = _atribuir_ciclos(df, troca_dates)

    # 3. Refit Weibull sobre ciclos genuínos
    duracoes_h = _detectar_genuinos(df, troca_dates, limiar_genuino_h)
    if len(duracoes_h) < 3:
        raise ValueError(
            f"Apenas {len(duracoes_h)} ciclos genuínos detectados — "
            "insuficiente para ajuste Weibull (mínimo 3)."
        )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        beta, _, eta = stats.weibull_min.fit(duracoes_h, floc=0)

    # 4. score_weibull
    valid = df["horas_desde_troca"].notna() & (df["horas_desde_troca"] >= 0)
    h = df.loc[valid, "horas_desde_troca"].values
    df.loc[valid, "score_weibull"] = stats.weibull_min.cdf(h, beta, loc=0, scale=eta)

    # 5. score_roll7d — média 7d reiniciada por ciclo (sem vazamento entre ciclos)
    score_roll = np.full(len(df), np.nan)
    for ciclo_id in df["ciclo_id"].unique():
        if ciclo_id < 0:
            continue
        mask_ciclo = df["ciclo_id"] == ciclo_id
        s = df.loc[mask_ciclo, "score_weibull"]
        if s.notna().sum() == 0:
            continue
        s_indexed = s.copy()
        s_indexed.index = df.loc[mask_ciclo, "ts"]
        rolled = s_indexed.rolling(window_score, min_periods=1).mean().values
        score_roll[mask_ciclo.values] = rolled

    df["score_roll7d"] = score_roll

    # 6. RUL condicional P10 / P50 / P90
    df.loc[valid, "rul_p10"] = _rul_condicional(h, 0.10, beta, eta)
    df.loc[valid, "rul_p50"] = _rul_condicional(h, 0.50, beta, eta)
    df.loc[valid, "rul_p90"] = _rul_condicional(h, 0.90, beta, eta)

    # 7. Exportar CSV
    df = df.rename(columns={"ts": "Timestamp"})
    cols = [c for c in COLUNAS_EXPORT if c in df.columns]
    out = df[cols]

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_csv, index=False)

    # 8. Exportar params JSON
    vida_p10 = stats.weibull_min.ppf(0.10, beta, loc=0, scale=eta)
    vida_p50 = stats.weibull_min.ppf(0.50, beta, loc=0, scale=eta)
    vida_p90 = stats.weibull_min.ppf(0.90, beta, loc=0, scale=eta)
    h_score60 = stats.weibull_min.ppf(0.60, beta, loc=0, scale=eta)

    params = {
        "weibull_beta": round(float(beta), 6),
        "weibull_eta_h": round(float(eta), 2),
        "limiar_genuino_h": limiar_genuino_h,
        "n_ciclos_genuinos": int(len(duracoes_h)),
        "vida_p10_h": round(float(vida_p10), 1),
        "vida_p50_h": round(float(vida_p50), 1),
        "vida_p90_h": round(float(vida_p90), 1),
        "h_score_cruza_60pct": round(float(h_score60), 1),
        "dias_score_cruza_60pct": round(float(h_score60) / 24, 1),
    }

    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)

    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gera 01_vida_rul.csv e 01_weibull_params.json")
    parser.add_argument("--input", default=str(_ROOT / "notebooks" / "00_hour_prev.csv"))
    parser.add_argument("--output-csv", default=str(_ROOT / "notebooks" / "01_vida_rul.csv"))
    parser.add_argument("--output-json", default=str(_ROOT / "notebooks" / "01_weibull_params.json"))
    parser.add_argument("--troca-csv", default=None)
    parser.add_argument("--limiar-genuino-h", type=float, default=20.0)
    args = parser.parse_args()

    result = run(
        input_path=args.input,
        output_csv=args.output_csv,
        output_json=args.output_json,
        troca_csv=args.troca_csv,
        limiar_genuino_h=args.limiar_genuino_h,
    )
    print(f"Salvo: {args.output_csv}  ({len(result):,} linhas)")
    print(f"Salvo: {args.output_json}")
