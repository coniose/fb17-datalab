"""
Generator: 03_padroes.csv

Une 01_vida_rul.csv e 02_sinais_forca.csv e calcula, para cada ciclo genuíno
(target_rul < 20h na troca), a antecedência de detecção de cada sinal:

  Eixo 1: e1_cruzou      — dias antes da troca em que score_roll7d ≥ 0.60
           e1_sustentado  — idem, mas com sustentação contínua ≥ 14 dias
  Eixo 2: e2_slope        — slope_Media_7d < -50 N/dia
           e2_pct         — pct_abaixo_800N_7d > 5%
           e2_min         — min_forca_3d < 800 N

Também calcula lead_e1, lead_e2 e lead_combinado (E1 OR E2) por ciclo.
Produz uma linha por ciclo (N_ciclos linhas, não série temporal).

Substitui a lógica de notebooks/03_padroes_pretroca.ipynb para execução em produção.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.connector import load_config
from src.predictor import load_troca_dates

_ROOT = Path(__file__).parent.parent.parent

# Thresholds v1 — não estão em config.yaml (usados apenas para análise de padrões)
_SCORE_CRITICO       = 0.60
_SLOPE_THRESHOLD     = -50.0
_PCT_LIMIAR          = 0.05
_SUSTENTACAO_MIN_D   = 14
_LIMIAR_GENUINO_H    = 20.0

COLUNAS_EXPORT = [
    "ciclo_id", "troca_ini", "troca_fim", "dur_dias", "rul_fim_h", "genuino",
    "e1_cruzou", "e1_sustentado",
    "e2_slope", "e2_pct", "e2_min",
    "lead_e1", "lead_e2", "lead_combinado",
]


def _primeiro_cruzamento_dias(
    ciclo_df: pd.DataFrame, t_troca: pd.Timestamp, col: str, cond_fn
) -> float | None:
    """Dias de antecedência do primeiro cruzamento de threshold, ou None."""
    sub = ciclo_df[ciclo_df[col].notna()]
    sub = sub[cond_fn(sub[col])]
    if sub.empty:
        return None
    return (t_troca - sub.iloc[0]["Timestamp"]).total_seconds() / 86400


def _dias_sustentados(
    ciclo_df: pd.DataFrame,
    t_troca: pd.Timestamp,
    col: str,
    cond_fn,
    min_dias: int = _SUSTENTACAO_MIN_D,
) -> float | None:
    """
    Dias antes da troca em que o sinal ficou continuamente acima do threshold
    por pelo menos min_dias dias.
    """
    sub = ciclo_df[ciclo_df[col].notna()].copy()
    if sub.empty:
        return None
    sub["above"] = cond_fn(sub[col]).astype(int)
    sub = sub.set_index("Timestamp")
    rolling_mean = sub["above"].rolling(f"{min_dias}D", min_periods=1).mean()
    candidates = rolling_mean[rolling_mean >= 1.0]
    if candidates.empty:
        return None
    return (t_troca - candidates.index[0]).total_seconds() / 86400


def run(
    input_vida_rul: str | Path = _ROOT / "notebooks" / "01_vida_rul.csv",
    input_sinais:   str | Path = _ROOT / "notebooks" / "02_sinais_forca.csv",
    output_path:    str | Path = _ROOT / "notebooks" / "03_padroes.csv",
    config_path:    str | Path | None = None,
    troca_csv:      str | Path | None = None,
) -> pd.DataFrame:
    """
    Calcula antecedência de detecção por ciclo genuíno.

    Args:
        input_vida_rul: Caminho para 01_vida_rul.csv.
        input_sinais:   Caminho para 02_sinais_forca.csv.
        output_path:    Destino de 03_padroes.csv.
        config_path:    Caminho para config.yaml (usa padrão do projeto se None).
        troca_csv:      Caminho para troca_modulo.csv (busca automática se None).

    Returns:
        DataFrame com uma linha por ciclo.
    """
    cfg  = load_config(config_path)
    trig = cfg.get("trigger", {})
    forca_limiar = trig.get("forca_min_emergencia", 800.0)

    # 1. Carregar e unir dados
    v1 = pd.read_csv(input_vida_rul, parse_dates=["Timestamp"])
    v2 = pd.read_csv(input_sinais,   parse_dates=["Timestamp"])

    v1["Timestamp"] = pd.to_datetime(v1["Timestamp"], utc=True)
    v2["Timestamp"] = pd.to_datetime(v2["Timestamp"], utc=True)

    df = v1.merge(
        v2[["Timestamp", "Media", "Delta_AB",
            "slope_Media_7d", "pct_abaixo_800N_7d",
            "min_forca_3d", "cv_Delta_AB_7d"]],
        on="Timestamp", how="left",
    )

    troca_dates = load_troca_dates(troca_csv)

    # Recomputar target_rul_h a partir das datas de troca (ground truth)
    df["target_rul_h"] = np.nan
    for i, t_fim in enumerate(troca_dates[1:], start=1):
        t_ini = troca_dates[i - 1]
        mask  = (df["Timestamp"] >= t_ini) & (df["Timestamp"] < t_fim)
        df.loc[mask, "target_rul_h"] = (
            (t_fim - df.loc[mask, "Timestamp"]).dt.total_seconds() / 3600
        ).values

    # 2. Calcular antecedência por ciclo
    registros = []

    for i, (t_ini, t_fim) in enumerate(zip(troca_dates[:-1], troca_dates[1:])):
        ciclo_df = df[(df["Timestamp"] >= t_ini) & (df["Timestamp"] < t_fim)].copy()
        if ciclo_df.empty:
            continue

        dur_h   = ciclo_df["horas_desde_troca"].max() if "horas_desde_troca" in ciclo_df else np.nan
        rul_fim = ciclo_df["target_rul_h"].iloc[-1] if ciclo_df["target_rul_h"].notna().any() else np.nan
        genuino = pd.notna(rul_fim) and rul_fim < _LIMIAR_GENUINO_H

        rec = {
            "ciclo_id"  : i,
            "troca_ini" : t_ini.date(),
            "troca_fim" : t_fim.date(),
            "dur_dias"  : round(dur_h / 24, 1) if pd.notna(dur_h) else None,
            "rul_fim_h" : round(rul_fim, 1) if pd.notna(rul_fim) else None,
            "genuino"   : genuino,
        }

        if genuino:
            rec["e1_cruzou"]     = _primeiro_cruzamento_dias(
                ciclo_df, t_fim, "score_roll7d",
                lambda s: s >= _SCORE_CRITICO)
            rec["e1_sustentado"] = _dias_sustentados(
                ciclo_df, t_fim, "score_roll7d",
                lambda s: s >= _SCORE_CRITICO)
            rec["e2_slope"]      = _primeiro_cruzamento_dias(
                ciclo_df, t_fim, "slope_Media_7d",
                lambda s: s < _SLOPE_THRESHOLD)
            rec["e2_pct"]        = _primeiro_cruzamento_dias(
                ciclo_df, t_fim, "pct_abaixo_800N_7d",
                lambda s: s > _PCT_LIMIAR)
            rec["e2_min"]        = _primeiro_cruzamento_dias(
                ciclo_df, t_fim, "min_forca_3d",
                lambda s: s < forca_limiar)

        registros.append(rec)

    padroes = pd.DataFrame(registros)

    # 3. Lead times agregados por ciclo
    sinal_cols = ["e1_cruzou", "e1_sustentado", "e2_slope", "e2_pct", "e2_min"]
    for col in sinal_cols:
        if col not in padroes.columns:
            padroes[col] = np.nan

    padroes["lead_e1"]        = padroes[["e1_cruzou", "e1_sustentado"]].max(axis=1)
    padroes["lead_e2"]        = padroes[["e2_slope", "e2_pct", "e2_min"]].max(axis=1)
    padroes["lead_combinado"] = padroes[sinal_cols].max(axis=1)

    # 4. Exportar
    cols = [c for c in COLUNAS_EXPORT if c in padroes.columns]
    out = padroes[cols]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_path, index=False)

    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gera 03_padroes.csv")
    parser.add_argument("--input-vida-rul", default=str(_ROOT / "notebooks" / "01_vida_rul.csv"))
    parser.add_argument("--input-sinais",   default=str(_ROOT / "notebooks" / "02_sinais_forca.csv"))
    parser.add_argument("--output",         default=str(_ROOT / "notebooks" / "03_padroes.csv"))
    parser.add_argument("--config",         default=None)
    parser.add_argument("--troca-csv",      default=None)
    args = parser.parse_args()

    result = run(
        input_vida_rul=args.input_vida_rul,
        input_sinais=args.input_sinais,
        output_path=args.output,
        config_path=args.config,
        troca_csv=args.troca_csv,
    )
    genuinos = result[result["genuino"]]
    cobertos = genuinos["lead_combinado"].notna().sum()
    lc       = genuinos["lead_combinado"].dropna()

    print(f"Salvo: {args.output}  ({len(result)} ciclos)")
    print(f"Cobertura combinada : {cobertos}/{len(genuinos)}  ({cobertos/len(genuinos):.0%})")
    if len(lc) > 0:
        print(f"Lead time mediano   : {lc.median():.1f} dias")
