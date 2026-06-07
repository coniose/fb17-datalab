"""
Generator: 01_vida_rul.csv + 01_weibull_params.json

A partir de 00_hour_prev.csv, calcula:
  - horas_desde_troca e ciclo_id  (atribuição de ciclo por data de troca)
  - horas_op_desde_troca          (horas_desde_troca − horas paradas acumuladas)
  - score_weibull = F_weibull(horas_op_desde_troca)  (CDF, eixo de idade OPERACIONAL)
  - score_roll7d  = média 7d do score, reiniciada por ciclo (entrada Eixo 1 do trigger)
  - rul_p10 / rul_p50 / rul_p90  (RUL condicional em horas operacionais)

Weibull é refitado sobre horas OPERACIONAIS (descontando paradas via anomaly_delay.csv).
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
    "horas_desde_troca", "horas_op_desde_troca", "ciclo_id",
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


def _adicionar_horas_op(
    df: pd.DataFrame,
    troca_dates: list,
    df_delay: "pd.DataFrame | None",
) -> pd.DataFrame:
    """
    Adiciona horas_op_desde_troca = horas_desde_troca − stopped_h_acumulado.

    df_delay deve ter índice Timestamp (UTC) e coluna 'stopped_h' (cumulativo
    dentro do ciclo). O lookup é feito por janela de tempo, não por ciclo_id,
    para evitar desalinhamento de índices entre as duas séries.
    """
    df = df.copy()
    df["horas_op_desde_troca"] = df["horas_desde_troca"].copy()

    if df_delay is None or "stopped_h" not in df_delay.columns:
        return df

    delay = df_delay.reset_index()
    delay.columns = [c if c != delay.columns[0] else "ts_delay" for c in delay.columns]
    if "ts_delay" not in delay.columns:
        delay = delay.rename(columns={delay.columns[0]: "ts_delay"})
    delay["ts_delay"] = pd.to_datetime(delay["ts_delay"], utc=True)
    delay = delay.sort_values("ts_delay").reset_index(drop=True)

    limites = troca_dates + [pd.Timestamp.max.tz_localize("UTC")]
    for t_ini, t_fim in zip(limites[:-1], limites[1:]):
        mask_df    = (df["ts"] >= t_ini) & (df["ts"] < t_fim)
        mask_delay = (delay["ts_delay"] >= t_ini) & (delay["ts_delay"] < t_fim)
        if not mask_df.any() or not mask_delay.any():
            continue

        # merge_asof: para cada leitura do ciclo, pega o stopped_h mais recente
        left = df.loc[mask_df, ["ts", "horas_desde_troca"]].sort_values("ts").reset_index()
        right = delay.loc[mask_delay, ["ts_delay", "stopped_h"]].sort_values("ts_delay")

        merged = pd.merge_asof(
            left,
            right,
            left_on="ts",
            right_on="ts_delay",
            direction="backward",
        )
        stopped = merged["stopped_h"].fillna(0.0).values
        op_h = (left["horas_desde_troca"].values - stopped).clip(min=0.0)
        df.loc[merged["index"].values, "horas_op_desde_troca"] = op_h

    return df


def _detectar_genuinos(
    df: pd.DataFrame,
    troca_dates: list,
    limiar_h: float,
    df_delay: "pd.DataFrame | None" = None,
) -> np.ndarray:
    """
    Retorna array de durações OPERACIONAIS (h) dos ciclos genuínos.

    Genuíno: target_rul < limiar_h na última leitura do ciclo.
    Duração = horas de calendário − máximo de stopped_h do ciclo (via delay).
    """
    duracoes = []
    for t_ini, t_fim in zip(troca_dates[:-1], troca_dates[1:]):
        ciclo = df[(df["ts"] >= t_ini) & (df["ts"] < t_fim)]
        if ciclo.empty:
            continue
        rul_ultimo = ciclo["rul"].iloc[-1]
        duracao_h  = ciclo["horas_desde_troca"].max()
        if not (pd.notna(rul_ultimo) and rul_ultimo < limiar_h
                and pd.notna(duracao_h) and duracao_h > 0):
            continue

        # Desconta paradas acumuladas no ciclo
        stopped_h = 0.0
        if df_delay is not None and "stopped_h" in df_delay.columns:
            mask = (df_delay.index >= t_ini) & (df_delay.index < t_fim)
            if mask.any():
                stopped_h = float(df_delay.loc[mask, "stopped_h"].max())

        duracao_op_h = max(1.0, duracao_h - stopped_h)
        duracoes.append(duracao_op_h)

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
    delay_csv: str | Path | None = None,
    limiar_genuino_h: float = 20.0,
    window_score: str = "7D",
) -> pd.DataFrame:
    """
    Calcula vida, score Weibull e RUL condicional a partir de 00_hour_prev.csv.

    O Weibull é calibrado sobre horas OPERACIONAIS (excluindo paradas registradas
    em anomaly_delay.csv). Se delay_csv não for fornecido, usa horas de calendário.

    Args:
        input_path:       Caminho para 00_hour_prev.csv.
        output_csv:       Destino de 01_vida_rul.csv.
        output_json:      Destino de 01_weibull_params.json.
        troca_csv:        Caminho para troca_modulo.csv (busca automática se None).
        delay_csv:        Caminho para anomaly_delay.csv (opcional; ativa modo operacional).
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

    # 2. Carregar anomaly_delay (opcional)
    df_delay = None
    _delay_path = Path(delay_csv) if delay_csv else _ROOT / "notebooks" / "anomaly_delay.csv"
    if _delay_path.exists():
        df_delay = pd.read_csv(_delay_path, index_col="Timestamp", parse_dates=True)
        df_delay.index = pd.to_datetime(df_delay.index, utc=True)

    # 3. Atribuir ciclo, horas_desde_troca e horas_op_desde_troca
    df = _atribuir_ciclos(df, troca_dates)
    df = _adicionar_horas_op(df, troca_dates, df_delay)

    # 4. Refit Weibull sobre durações OPERACIONAIS dos ciclos genuínos
    duracoes_op_h = _detectar_genuinos(df, troca_dates, limiar_genuino_h, df_delay)
    if len(duracoes_op_h) < 3:
        # Fallback: relaxa o filtro de genuinidade antes de falhar.
        # Ocorre quando trocas oportunistas (paradas longas) dominam o histórico
        # e o limiar configurado é mais estrito que o necessário.
        duracoes_todos = _detectar_genuinos(df, troca_dates, float("inf"), df_delay)
        if len(duracoes_todos) >= 3:
            warnings.warn(
                f"[gen_vida_rul] Apenas {len(duracoes_op_h)} ciclos genuínos "
                f"(limiar={limiar_genuino_h}h). Usando todos os "
                f"{len(duracoes_todos)} ciclos disponíveis para ajuste Weibull. "
                "Considere aumentar limiar_genuino_h em config.yaml.",
                RuntimeWarning,
                stacklevel=2,
            )
            duracoes_op_h = duracoes_todos
        else:
            raise ValueError(
                f"Apenas {len(duracoes_op_h)} ciclos genuínos e "
                f"{len(duracoes_todos)} ciclos totais — "
                "insuficiente para ajuste Weibull (mínimo 3). "
                "Aguarde mais trocas ou revise troca_modulo.csv."
            )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        beta, _, eta = stats.weibull_min.fit(duracoes_op_h, floc=0)

    # 5. score_weibull sobre horas_op_desde_troca
    valid = df["horas_op_desde_troca"].notna() & (df["horas_op_desde_troca"] >= 0)
    h_op = df.loc[valid, "horas_op_desde_troca"].values
    df.loc[valid, "score_weibull"] = stats.weibull_min.cdf(h_op, beta, loc=0, scale=eta)

    # 6. score_roll7d — média 7d reiniciada por ciclo (sem vazamento entre ciclos)
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

    # 7. RUL condicional P10 / P50 / P90 (em horas operacionais)
    df.loc[valid, "rul_p10"] = _rul_condicional(h_op, 0.10, beta, eta)
    df.loc[valid, "rul_p50"] = _rul_condicional(h_op, 0.50, beta, eta)
    df.loc[valid, "rul_p90"] = _rul_condicional(h_op, 0.90, beta, eta)

    # 8. Exportar CSV
    df = df.rename(columns={"ts": "Timestamp"})
    cols = [c for c in COLUNAS_EXPORT if c in df.columns]
    out = df[cols]

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_csv, index=False)

    # 9. Exportar params JSON
    vida_p10  = stats.weibull_min.ppf(0.10, beta, loc=0, scale=eta)
    vida_p50  = stats.weibull_min.ppf(0.50, beta, loc=0, scale=eta)
    vida_p90  = stats.weibull_min.ppf(0.90, beta, loc=0, scale=eta)
    h_score60 = stats.weibull_min.ppf(0.60, beta, loc=0, scale=eta)

    params = {
        "weibull_beta": round(float(beta), 6),
        "weibull_eta_h": round(float(eta), 2),
        "limiar_genuino_h": limiar_genuino_h,
        "n_ciclos_genuinos": int(len(duracoes_op_h)),
        "vida_p10_h": round(float(vida_p10), 1),
        "vida_p50_h": round(float(vida_p50), 1),
        "vida_p90_h": round(float(vida_p90), 1),
        "h_score_cruza_60pct": round(float(h_score60), 1),
        "dias_score_cruza_60pct": round(float(h_score60) / 24, 1),
        "calibrado_em_horas_operacionais": df_delay is not None,
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
