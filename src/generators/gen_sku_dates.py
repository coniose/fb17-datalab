"""
Generator: sku_dates.csv

Extrai dados de SKU (produto embalado nas baggers) via spy.pull() usando os
IDs de sinal configurados em config.yaml (seção sku_signals).

Puxa apenas os dois sinais de SKU — não a worksheet completa — evitando
falhas causadas por outros sinais corrompidos no PI da mesma worksheet.

Colunas de saída:
  index      — timestamp (UTC)
  bag1_sku   — SKU da bagger 1 (string, ex: "30246860")
  bag2_sku   — SKU da bagger 2 (string, ex: "30246825")

Chamado por pipeline_producao.ipynb antes da etapa de normalização SKU.
Pode ser executado diretamente via CLI para atualizar o CSV manualmente.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).parent.parent.parent
_notebooks_dir = _ROOT / "notebooks"
if _notebooks_dir.exists() and str(_notebooks_dir) not in sys.path:
    sys.path.insert(0, str(_notebooks_dir))

from src.connector import load_config

COLUNAS_SAIDA = ["bag1_sku", "bag2_sku"]


def _sku_to_str(series: pd.Series) -> pd.Series:
    """Converte série float (ex: 30246860.0) para string (ex: '30246860').
    Valores NaN permanecem None."""
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.apply(lambda x: str(int(x)) if pd.notna(x) else None)


def run(
    output_path: str | Path = _ROOT / "notebooks" / "sku_dates.csv",
    config_path: str | Path | None = None,
) -> pd.DataFrame:
    """
    Extrai SKU dos sinais Seeq configurados em sku_signals e salva sku_dates.csv.

    Args:
        output_path:  Destino do CSV gerado.
        config_path:  Caminho para config.yaml (usa padrão do projeto se None).

    Returns:
        DataFrame com colunas index, bag1_sku, bag2_sku.

    Raises:
        KeyError:  se config.yaml não tiver a seção sku_signals.
        ValueError: se nenhum sinal de SKU for encontrado.
    """
    from seeq import spy

    cfg = load_config(config_path)
    sku_signals = cfg.get("sku_signals", [])
    if not sku_signals:
        raise KeyError(
            "config.yaml não tem a seção 'sku_signals'. "
            "Adicione os IDs dos sinais bag1_sku e bag2_sku."
        )

    # Mapeia name → id para renomear após o pull
    id_to_name = {s["id"]: s["name"] for s in sku_signals if s.get("id") and s.get("name")}
    items_df = pd.DataFrame([
        {"ID": s["id"], "Type": "Signal"}
        for s in sku_signals if s.get("id")
    ])

    time_delta_days = cfg.get("project", {}).get("time_delta_days", 1460)
    user_tz = spy.utils.get_user_timezone(spy.session)
    end_time = pd.Timestamp.now(tz=user_tz)
    start_time = end_time - pd.Timedelta(days=time_delta_days)

    raw = spy.pull(
        items_df,
        start=start_time.isoformat(),
        end=end_time.isoformat(),
        grid="1h",
        header="ID",
    ).reset_index()

    # Renomear IDs → bag1_sku / bag2_sku
    raw = raw.rename(columns=id_to_name)

    # Garantir que ambas as colunas existam (bag2_sku pode estar ausente)
    for col in COLUNAS_SAIDA:
        if col not in raw.columns:
            raw[col] = None

    # Converter float → string limpa
    for col in COLUNAS_SAIDA:
        raw[col] = _sku_to_str(raw[col])

    # Normalizar coluna de timestamp para "index"
    ts_col = next(
        (c for c in raw.columns if c.lower() in ("index", "timestamp")), None
    )
    if ts_col and ts_col != "index":
        raw = raw.rename(columns={ts_col: "index"})

    df_out = raw[["index"] + COLUNAS_SAIDA].copy()
    df_out["index"] = pd.to_datetime(df_out["index"], utc=True)
    df_out = df_out.sort_values("index").reset_index(drop=True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(output_path, index=False)

    return df_out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gera sku_dates.csv a partir do Seeq")
    parser.add_argument(
        "--output",
        default=str(_ROOT / "notebooks" / "sku_dates.csv"),
        help="Caminho de saída do CSV",
    )
    parser.add_argument("--config", default=None, help="Caminho para config.yaml")
    args = parser.parse_args()

    result = run(output_path=args.output, config_path=args.config)
    n_skus = result["bag1_sku"].notna().sum()
    print(f"Salvo: {args.output}  ({len(result):,} linhas | {n_skus:,} com SKU bag1)")
