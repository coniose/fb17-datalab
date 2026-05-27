"""
Generator: sku_dates.csv

Extrai o sinal de Phantom Code via spy.pull() usando o ID configurado em
config.yaml (seção phantom_signals).

Puxa apenas o sinal de phantom — não a worksheet completa — evitando
falhas causadas por outros sinais corrompidos no PI da mesma worksheet.

Colunas de saída:
  index    — timestamp (UTC)
  phantom  — código do phantom (string, ex: "30246860")

Chamado por pipeline_producao.ipynb antes da etapa de normalização.
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


def _phantom_to_str(series: pd.Series) -> pd.Series:
    """Converte série float (ex: 30246860.0) para string (ex: '30246860').
    Valores NaN permanecem None."""
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.apply(lambda x: str(int(x)) if pd.notna(x) else None)


def run(
    output_path: str | Path = _ROOT / "notebooks" / "sku_dates.csv",
    config_path: str | Path | None = None,
) -> pd.DataFrame:
    """
    Extrai Phantom Code do sinal Seeq configurado em phantom_signals e salva sku_dates.csv.

    Args:
        output_path:  Destino do CSV gerado.
        config_path:  Caminho para config.yaml (usa padrão do projeto se None).

    Returns:
        DataFrame com colunas index, phantom.

    Raises:
        KeyError:  se config.yaml não tiver a seção phantom_signals.
    """
    from seeq import spy

    cfg = load_config(config_path)
    phantom_signals = cfg.get("phantom_signals", [])
    if not phantom_signals:
        raise KeyError(
            "config.yaml não tem a seção 'phantom_signals'. "
            "Adicione o ID do sinal de Phantom Code."
        )

    # Só pega o primeiro ID de phantom válido
    phantom = next((s for s in phantom_signals if s.get("id")), None)
    if not phantom:
        raise KeyError("Nenhum ID válido encontrado em phantom_signals.")

    phantom_id = phantom["id"]

    items_df = pd.DataFrame([{"ID": phantom_id, "Type": "Signal"}])

    time_delta_days = cfg.get("project", {}).get("time_delta_days", 1460)
    user_tz = spy.utils.get_user_timezone(spy.session)
    end_time = pd.Timestamp.now(tz=user_tz)

    # Tenta janelas progressivamente menores até obter dados
    _fallback_days = [time_delta_days, 730, 365, 180, 90, 60, 30, 14]
    raw = None
    for days in _fallback_days:
        start_time = end_time - pd.Timedelta(days=days)
        try:
            raw = spy.pull(
                items_df,
                start=start_time.isoformat(),
                end=end_time.isoformat(),
                grid="1h",
                header="ID",
                quiet=True,
            ).reset_index()
            if days < time_delta_days:
                print(f"      ⚠ PI Archive corrompido — janela reduzida para {days} dias")
            break
        except Exception as e:
            if days == _fallback_days[-1]:
                raise
            print(f"      ⚠ Erro ao puxar {days}d ({type(e).__name__}), tentando próxima janela...")
            continue

    # Renomeia a coluna do ID para 'phantom'
    raw = raw.rename(columns={phantom_id: "phantom"})

    # Garante coluna 'phantom' presente
    if "phantom" not in raw.columns:
        raw["phantom"] = None

    raw["phantom"] = _phantom_to_str(raw["phantom"])

    # Garante coluna 'index' como timestamp UTC
    ts_col = next((c for c in raw.columns if c.lower() in ("index", "timestamp")), None)
    if ts_col and ts_col != "index":
        raw = raw.rename(columns={ts_col: "index"})

    df_out = raw[["index", "phantom"]].copy()
    df_out["index"] = pd.to_datetime(df_out["index"], utc=True)
    df_out = df_out.sort_values("index").reset_index(drop=True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(output_path, index=False)

    return df_out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gera sku_dates.csv com Phantom Code do Seeq")
    parser.add_argument(
        "--output",
        default=str(_ROOT / "notebooks" / "sku_dates.csv"),
        help="Caminho de saída do CSV",
    )
    parser.add_argument("--config", default=None, help="Caminho para config.yaml")
    args = parser.parse_args()

    result = run(output_path=args.output, config_path=args.config)
    n_phantom = result["phantom"].notna().sum()
    print(f"Salvo: {args.output}  ({len(result):,} linhas | {n_phantom:,} com Phantom Code)")
