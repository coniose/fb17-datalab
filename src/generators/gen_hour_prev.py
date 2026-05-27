"""
Generator: 00_hour_prev.csv

Extrai sinais de força do Seeq, aplica pré-processamento e calcula
target_rul + features de degradação.

Substitui a lógica de notebooks/00_gerar_hour_prev.ipynb para execução em produção.
Chamado por pipeline_producao.ipynb; pode ser executado diretamente via CLI.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).parent.parent.parent

# Expõe o mock seeq em desenvolvimento local (notebooks/seeq/).
# Em produção (Databricks) o pacote real está no sys.path do sistema.
_notebooks_dir = _ROOT / "notebooks"
if _notebooks_dir.exists() and str(_notebooks_dir) not in sys.path:
    sys.path.insert(0, str(_notebooks_dir))

from src.connector import load_config
from src.preprocessing import add_media, filter_delta_outliers
from src.predictor import build_rul_target, extract_degradation_features, load_troca_dates

COLUNAS_EXPORT = [
    "Timestamp",
    "Forca_A", "Forca_B",
    "Media", "Delta_AB",
    "target_rul",
    "slope_Media_14d", "std_Delta_AB_7d", "media_ratio_14d",
]


def _pull_raw(cfg: dict) -> pd.DataFrame:
    """Chama spy.pull() com os IDs de sinal do config.yaml."""
    from seeq import spy

    signals = cfg.get("signals", [])
    signal_ids = {s["name"]: s["id"] for s in signals if s.get("name") and s.get("id")}

    time_delta_days = cfg.get("project", {}).get("time_delta_days", 1460)
    user_tz = spy.utils.get_user_timezone(spy.session)
    end_time = pd.Timestamp.now(tz=user_tz)
    start_time = end_time - pd.Timedelta(days=time_delta_days)

    items_df = pd.DataFrame([
        {"ID": sid, "Type": "Signal"}
        for sid in signal_ids.values()
    ])

    data = spy.pull(
        items_df,
        start=start_time.isoformat(),
        end=end_time.isoformat(),
        grid=None,
        header="ID",
    )

    id_to_name = {v: k for k, v in signal_ids.items()}
    return data.rename(columns=id_to_name)


def run(
    output_path: str | Path = _ROOT / "notebooks" / "00_hour_prev.csv",
    config_path: str | Path | None = None,
    troca_csv: str | Path | None = None,
) -> pd.DataFrame:
    """
    Extrai, processa e salva 00_hour_prev.csv.

    Args:
        output_path: Destino do CSV gerado.
        config_path: Caminho para config.yaml (usa padrão do projeto se None).
        troca_csv:   Caminho para troca_modulo.csv (busca automática se None).

    Returns:
        DataFrame exportado (sem index).
    """
    cfg = load_config(config_path)
    prep_cfg = cfg.get("preprocessing", {})

    # 1. Pull Seeq
    data_raw = _pull_raw(cfg)

    # 2. Normalizar índice, ordenar e remover NaN nos sinais principais
    df = data_raw.copy()
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index().rename(columns={"index": "Timestamp"})
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True)
    df = df.sort_values("Timestamp").reset_index(drop=True)
    df = df.dropna(subset=["Forca_A", "Forca_B"])

    # 3. Média + filtro IQR no Delta_AB
    df = add_media(df, col_a="Forca_A", col_b="Forca_B")
    df, _ = filter_delta_outliers(
        df,
        col_a=prep_cfg.get("delta_col_a", "Forca_A"),
        col_b=prep_cfg.get("delta_col_b", "Forca_B"),
        multiplier=prep_cfg.get("iqr_multiplier", 1.0),
    )
    df = df.reset_index(drop=True)

    # 4. target_rul — horas até a próxima troca confirmada
    troca_dates = load_troca_dates(troca_csv)
    df = build_rul_target(df, troca_dates=troca_dates)

    # 5. Features de degradação (slope, std, ratio)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        features = extract_degradation_features(df)

    df = pd.concat([df.reset_index(drop=True), features.reset_index(drop=True)], axis=1)
    df = df.loc[:, ~df.columns.duplicated()]

    # 6. Exportar
    cols = [c for c in COLUNAS_EXPORT if c in df.columns]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df[cols].to_csv(output_path, index=False)

    return df[cols]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gera 00_hour_prev.csv a partir do Seeq")
    parser.add_argument("--output", default=str(_ROOT / "notebooks" / "00_hour_prev.csv"),
                        help="Caminho de saída do CSV")
    parser.add_argument("--config", default=None, help="Caminho para config.yaml")
    parser.add_argument("--troca-csv", default=None, help="Caminho para troca_modulo.csv")
    args = parser.parse_args()

    result = run(output_path=args.output, config_path=args.config, troca_csv=args.troca_csv)
    print(f"Salvo: {args.output}  ({len(result):,} linhas)")
