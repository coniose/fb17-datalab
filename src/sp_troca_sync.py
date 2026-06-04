"""
sp_troca_sync.py — Sincroniza troca_modulo.csv a partir do SharePoint.

Baixa CONSUMO MAINTACKER BCM.xlsx da pasta projeto_kairos no SP, filtra pela
máquina configurada em config.yaml e sobrescreve notebooks/troca_modulo.csv.

O formato de saída é idêntico ao produzido por extrair_troca_modulo.py:
    Data-base do inicio,tipo
    2026-05-29,indefinido
    ...

Uso como script:
    python -m src.sp_troca_sync
    python -m src.sp_troca_sync --config config.yaml --out notebooks/troca_modulo.csv

Uso programático (em notebooks ou pipeline):
    from src.sp_troca_sync import sync_troca_modulo
    sync_troca_modulo()   # usa config.yaml e grava em notebooks/troca_modulo.csv
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pandas as pd
import yaml
from dotenv import dotenv_values

# ── Constantes ────────────────────────────────────────────────────────────────

SP_FILE_PATH  = "/Sites/H945/Suzano/api_csv/projeto_kairos/CONSUMO MAINTACKER BCM.xlsx"
SHEET_NAME    = "Trocas Maintacker"
COL_DATA      = "Data"
COL_MAQUINA   = "Linha"
DEFAULT_TIPO  = "indefinido"

ROOT = Path(__file__).resolve().parents[1]   # raiz do fb14-datalab


# ── Core ──────────────────────────────────────────────────────────────────────

def _load_config(config_path: Path) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sp_client(config_path: Path):
    """Cria KairosSharePointClient usando credenciais do sharepoint.ev."""
    # Procura sharepoint.ev na raiz do projeto ou pasta pai
    for candidate in (config_path.parent / "sharepoint.ev",
                      config_path.parent.parent / "sharepoint.ev"):
        if candidate.exists():
            ev_path = candidate
            break
    else:
        raise FileNotFoundError("sharepoint.ev não encontrado.")

    creds = dotenv_values(ev_path)

    # Importa o client do projeto-kairos (disponível no PYTHONPATH do Data Lab)
    # ou faz fallback com office365 diretamente
    try:
        sys.path.insert(0, str(ROOT.parent / "project-kairos"))
        from src.sharepoint.client import KairosSharePointClient
        return KairosSharePointClient(
            url=creds.get("SP_URL"),
            username=creds.get("SP_USER"),
            password=creds.get("SP_PASS"),
        )
    except ImportError:
        from office365.runtime.auth.authentication_context import AuthenticationContext
        from office365.sharepoint.client_context import ClientContext
        from office365.sharepoint.files.file import File as _File

        class _MinimalClient:
            def __init__(self):
                url = creds.get("SP_URL", "")
                ctx_auth = AuthenticationContext(url)
                ctx_auth.acquire_token_for_user(creds.get("SP_USER", ""), creds.get("SP_PASS", ""))
                self._ctx = ClientContext(url, ctx_auth)

            def download_bytes(self, relative_url: str) -> bytes:
                return _File.open_binary(self._ctx, relative_url).content

        return _MinimalClient()


def sync_troca_modulo(
    config_path: str | Path | None = None,
    out_path: str | Path | None = None,
    verbose: bool = True,
) -> Path:
    """
    Baixa a sheet 'Trocas Maintacker' do SP, filtra pela máquina do config.yaml
    e grava troca_modulo.csv no caminho indicado.

    Returns:
        Path do arquivo gravado.
    """
    config_path = Path(config_path or ROOT / "config.yaml")
    cfg         = _load_config(config_path)
    maquina     = cfg["project"]["maquina"]           # ex: "FB14"

    out_path = Path(out_path or ROOT / "notebooks" / "troca_modulo.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"[sp_troca_sync] Máquina: {maquina}")
        print(f"[sp_troca_sync] Baixando {SP_FILE_PATH} ...")

    sp  = _sp_client(config_path)
    raw = sp.download_bytes(SP_FILE_PATH)

    xl  = pd.ExcelFile(io.BytesIO(raw))
    if SHEET_NAME not in xl.sheet_names:
        raise ValueError(
            f"Sheet '{SHEET_NAME}' não encontrada em {SP_FILE_PATH}.\n"
            f"Sheets disponíveis: {xl.sheet_names}"
        )

    df = xl.parse(SHEET_NAME)

    if COL_DATA not in df.columns or COL_MAQUINA not in df.columns:
        raise ValueError(
            f"Colunas esperadas: '{COL_DATA}', '{COL_MAQUINA}'.\n"
            f"Encontradas: {list(df.columns)}"
        )

    # Filtrar pela máquina e preparar saída
    df_maq = df[df[COL_MAQUINA].str.strip().str.upper() == maquina.upper()].copy()

    if df_maq.empty:
        raise ValueError(
            f"Nenhuma troca encontrada para máquina '{maquina}' na sheet '{SHEET_NAME}'."
        )

    df_maq[COL_DATA] = pd.to_datetime(df_maq[COL_DATA], errors="coerce")
    df_maq = (df_maq
              .dropna(subset=[COL_DATA])
              .drop_duplicates(subset=[COL_DATA])
              .sort_values(COL_DATA))

    out_df = pd.DataFrame({
        "Data-base do inicio": df_maq[COL_DATA].dt.strftime("%Y-%m-%d"),
        "tipo": DEFAULT_TIPO,
    }).reset_index(drop=True)

    out_df.to_csv(out_path, index=False)

    if verbose:
        print(f"[sp_troca_sync] {len(out_df)} trocas gravadas → {out_path}")
        for row in out_df.itertuples(index=False):
            print(f"               {row[0]}  {row[1]}")

    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    p = argparse.ArgumentParser(description="Sincroniza troca_modulo.csv a partir do SharePoint.")
    p.add_argument("--config", default=None, help="Caminho para config.yaml")
    p.add_argument("--out",    default=None, help="Caminho de saída para troca_modulo.csv")
    p.add_argument("--quiet",  action="store_true", help="Suprime output")
    args = p.parse_args()

    sync_troca_modulo(
        config_path=args.config,
        out_path=args.out,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    _cli()
