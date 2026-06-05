"""
Generator: anomaly_delay.csv

Puxa o sinal de Tempo Delay Calculado do Seeq para a máquina configurada
e computa stopped_h acumulado por ciclo.

stopped_h: total de horas paradas acumuladas desde o início do ciclo até
cada timestamp. Usado por gen_vida_rul para calibrar o Weibull em tempo
operacional em vez de tempo de calendário.

Schema de saída (anomaly_delay.csv):
    Timestamp (index, UTC)
    stopped_h   — horas paradas acumuladas no ciclo até este ponto
    delay_min   — valor bruto do sinal de delay (minutos)
    ciclo_id    — índice do ciclo (0-based, alinhado com troca_modulo.csv)

Integração no pipeline_producao.ipynb:
    from src.generators.gen_anomaly_delay import run as gen_anomaly_delay
    df_delay = gen_anomaly_delay(output_path=PATH_DELAY, troca_csv=PATH_TROCA)
    # roda antes de gen_vida_rul
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.predictor import load_troca_dates

_ROOT = Path(__file__).parent.parent.parent

DELAY_THRESHOLD_MIN = 0   # delay > N min → máquina parada
GRID_PULL           = "1h"
ANOS_HISTORICO      = 4


def _load_maquina(config_path: Path) -> str:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)["project"]["maquina"]


def _find_delay_signal(maquina: str) -> "pd.DataFrame | None":
    """Busca o sinal de delay via spy.search com padrões pelo nome da máquina."""
    try:
        from seeq import spy
    except ImportError:
        return None

    patterns = [
        f"*{maquina}*Tempo Delay*",
        f"*{maquina}*Delay*Calculado*",
        f"*{maquina}*Delay*",
    ]
    for pat in patterns:
        try:
            res = spy.search({"Name": pat}, quiet=True)
            if res is not None and not res.empty:
                # Prefere sinais calculados (CalculatedSignal)
                calc = res[res.get("Type", "").str.contains("Signal", case=False, na=False)]
                candidates = calc if not calc.empty else res
                row = candidates.iloc[0]
                print(f"[gen_anomaly_delay] Sinal encontrado: {row.get('Name', row['ID'])}")
                return pd.DataFrame([{
                    "ID":   row["ID"],
                    "Type": row.get("Type", "CalculatedSignal"),
                }])
        except Exception as e:
            print(f"[gen_anomaly_delay] search '{pat}': {e}")

    return None


def _pull_delay(signal_df: "pd.DataFrame", start: str, end: str) -> "pd.DataFrame | None":
    """Pull do sinal de delay (grid 1h)."""
    try:
        from seeq import spy
        df = spy.pull(
            signal_df,
            start=start,
            end=end,
            grid=GRID_PULL,
            header="ID",
            quiet=True,
        )
        df.index = pd.to_datetime(df.index, utc=True)
        # Usa primeira coluna como delay_min
        col = df.columns[0]
        out = df[[col]].rename(columns={col: "delay_min"})
        return out.dropna()
    except Exception as e:
        print(f"[gen_anomaly_delay] pull falhou: {e}")
        return None


def _compute_stopped_h(
    df_delay_raw: "pd.DataFrame",
    troca_dates: list,
    threshold_min: float,
) -> "pd.DataFrame":
    """
    Para cada timestamp do sinal de delay, calcula stopped_h = total de horas
    paradas acumuladas desde o início do ciclo corrente.

    stopped_h é cumulativo dentro do ciclo — reseta a 0 em cada troca.
    """
    limites = troca_dates + [pd.Timestamp.max.tz_localize("UTC")]
    records = []

    for ciclo_id, (t_ini, t_fim) in enumerate(zip(limites[:-1], limites[1:])):
        mask = (df_delay_raw.index >= t_ini) & (df_delay_raw.index < t_fim)
        ciclo = df_delay_raw.loc[mask].copy()
        if ciclo.empty:
            continue

        # Cada linha do grid 1h representa 1 hora — conta as paradas cumulativamente
        stopped_flag = (ciclo["delay_min"] > threshold_min).astype(float)
        stopped_cumul = stopped_flag.cumsum()

        for ts, row in ciclo.iterrows():
            records.append({
                "Timestamp": ts,
                "ciclo_id":  ciclo_id,
                "delay_min": float(row["delay_min"]),
                "stopped_h": float(stopped_cumul.loc[ts]),
            })

    df = pd.DataFrame(records)
    if df.empty:
        return df
    df = df.set_index("Timestamp").sort_index()
    return df


def run(
    output_path: "str | Path | None" = None,
    troca_csv:   "str | Path | None" = None,
    config_path: "str | Path | None" = None,
    anos_historico: int = ANOS_HISTORICO,
    delay_threshold_min: float = DELAY_THRESHOLD_MIN,
) -> "pd.DataFrame":
    """
    Gera anomaly_delay.csv com stopped_h acumulado por ciclo.

    Retorna DataFrame vazio (sem falhar) se o sinal de delay não for encontrado
    — gen_vida_rul cairá em modo calendário automaticamente.

    Args:
        output_path:         Destino do CSV (default: notebooks/anomaly_delay.csv).
        troca_csv:           Caminho para troca_modulo.csv.
        config_path:         Caminho para config.yaml (lê nome da máquina).
        anos_historico:      Janela de pull em anos (default 4).
        delay_threshold_min: Limiar em minutos para considerar máquina parada (default 0).
    """
    cfg_path    = Path(config_path  or _ROOT / "config.yaml")
    out_path    = Path(output_path  or _ROOT / "notebooks" / "anomaly_delay.csv")
    maquina     = _load_maquina(cfg_path)
    troca_dates = load_troca_dates(troca_csv)

    print(f"[gen_anomaly_delay] Máquina: {maquina}  |  {len(troca_dates)} trocas")

    # 1. Encontra sinal de delay no Seeq
    signal_df = _find_delay_signal(maquina)
    if signal_df is None:
        print("[gen_anomaly_delay] ⚠️  Sinal de delay não encontrado — anomaly_delay.csv não gerado.")
        return pd.DataFrame()

    # 2. Pull
    end_ts   = pd.Timestamp.now(tz="UTC")
    start_ts = end_ts - pd.DateOffset(years=anos_historico)
    df_raw   = _pull_delay(signal_df, start_ts.isoformat(), end_ts.isoformat())
    if df_raw is None or df_raw.empty:
        print("[gen_anomaly_delay] ⚠️  Pull vazio — anomaly_delay.csv não gerado.")
        return pd.DataFrame()

    print(f"[gen_anomaly_delay] Pull OK: {len(df_raw):,} leituras  "
          f"({df_raw.index[0].date()} → {df_raw.index[-1].date()})")

    # 3. Computa stopped_h por ciclo
    df_out = _compute_stopped_h(df_raw, troca_dates, delay_threshold_min)
    if df_out.empty:
        print("[gen_anomaly_delay] ⚠️  Nenhum dado dentro dos ciclos — verifique troca_modulo.csv.")
        return pd.DataFrame()

    # 4. Exporta
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(out_path)

    paradas = (df_out["stopped_h"] > 0).sum()
    print(f"[gen_anomaly_delay] Exportado: {out_path}")
    print(f"  {len(df_out):,} linhas  |  {paradas:,} horas com parada registrada")
    print(f"  stopped_h máx: {df_out['stopped_h'].max():.1f}h")

    return df_out
