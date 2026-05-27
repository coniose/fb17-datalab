"""
sku_normalizer.py — Normalização de força de selagem por demanda de SKU
=======================================================================

Cada SKU (produto embalado) exige uma força de selagem diferente da máquina.
Comparar força bruta entre períodos com mix de SKU distinto é como comparar
grandezas incomparáveis — um rolo degradado pode parecer saudável com um SKU
leve, e um rolo saudável pode parecer alarme com um SKU pesado.

A normalização converte 'Media' (N) para 'Media_norm' (N equivalente no SKU
de referência), permitindo que o trigger engine compare maçãs com maçãs.

Fórmula:
    Media_norm = Media / sku_fator_bag1

Onde sku_fator_bag1 é o STRESS FACTOR do SKU — razão entre a força média
de um rolo NOVO (primeiros 10 dias de ciclo) com este SKU e a mediana global
de todos os fresh-roller baselines. Este método de calibração por "fresh-roller"
evita o Paradoxo de Simpson: ao usar apenas leituras de rolos novos, o fator
captura a demanda real do SKU sem contaminação por degradação do rolo.

    sku_fator = 1.00 → SKU de demanda média (baseline)
    sku_fator > 1.00 → SKU exigente (força de rolo novo é alta)
    sku_fator < 1.00 → SKU leve (força de rolo novo é baixa)

    Resultado: Media_norm ≈ 1149 N para qualquer SKU com rolo saudável.

Calibração: 2026-05-10 | 23 SKUs calibrados por fresh-roller baseline
            Mediana global de fresh-roller: 1149.1 N
            Range stress factor: 0.511 (leve) → 1.327 (pesado) | Razão: 2.6×
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Catálogo de fatores de stress (bag1_sku) ─────────────────────────────────
# Método: fresh-roller baseline (primeiros 10 dias de cada ciclo) por SKU
# fator = média_forca_fresh_sku / mediana_global_fresh (1149.1 N)
# Calibrado: Mai/2022–Mar/2025 | 23 SKUs com n ≥ 10 observações fresh
# Ordenado do SKU mais leve para o mais exigente.
SKU_CATALOG: Dict[int, float] = {
    # ── SKUs muito leves (força sistematicamente baixa com rolo novo) ──────
    30241655: 0.511,   # força fresh: ~587 N — apenas 51% da mediana
    30244902: 0.658,   # força fresh: ~756 N
    30241637: 0.665,   # força fresh: ~764 N
    30241669: 0.666,   # força fresh: ~766 N
    # ── SKUs leves ────────────────────────────────────────────────────────
    30243599: 0.861,   # força fresh: ~990 N
    30243609: 0.889,   # estimado por similaridade com 30243599
    30243615: 0.924,   # força fresh: ~1062 N
    30243621: 0.946,   # força fresh: ~1087 N
    30242137: 0.947,   # força fresh: ~1088 N
    # ── SKUs próximos ao baseline ─────────────────────────────────────────
    30243807: 0.966,   # força fresh: ~1110 N
    30244770: 0.971,   # força fresh: ~1117 N
    30244719: 0.976,   # força fresh: ~1122 N  ← SKU dominante 2025/2026
    30245878: 0.989,   # força fresh: ~1137 N
    30244748: 1.000,   # força fresh: ~1149 N  ← MEDIANA (referência)
    30242114: 1.006,   # força fresh: ~1156 N
    # ── SKUs pesados ──────────────────────────────────────────────────────
    30244716: 1.042,   # força fresh: ~1197 N
    30242125: 1.055,   # força fresh: ~1213 N
    30244780: 1.074,   # força fresh: ~1234 N
    30244746: 1.092,   # força fresh: ~1255 N
    30242124: 1.101,   # força fresh: ~1265 N
    30244771: 1.116,   # força fresh: ~1282 N
    30244749: 1.142,   # força fresh: ~1312 N
    # ── SKUs muito pesados ────────────────────────────────────────────────
    30242136: 1.286,   # força fresh: ~1477 N
    30243813: 1.327,   # força fresh: ~1525 N  ← mais exigente do catálogo
    # ── SKUs recentes (sem fresh-roller calibrado — estimados por faixa) ─
    30246818: 1.050,
    30246872: 1.020,
    30246850: 1.020,
    30246824: 1.000,
    30246822: 0.950,
    30246827: 0.940,
    30246808: 1.000,
}

# Fator para SKUs fora do catálogo (conservador: assume baseline)
FATOR_DESCONHECIDO: float = 1.0

# ── Definição de SKUs futuros programados ────────────────────────────────────
# Formato: lista de dicts com chaves 'data_inicio', 'data_fim', 'sku'
# Populate com dados reais quando disponíveis via MES / PP do SAP.
# Por enquanto, 5 SKUs simulados cobrindo os próximos ~60 dias após 2026-05-06.
UPCOMING_SKUS: List[Dict] = [
    # SKU pesado — rolo trabalhará mais intensamente
    {"data_inicio": "2026-05-09", "data_fim": "2026-05-18", "sku": 30244719},
    # SKU médio-alto
    {"data_inicio": "2026-05-18", "data_fim": "2026-05-28", "sku": 30243615},
    # SKU baseline
    {"data_inicio": "2026-05-28", "data_fim": "2026-06-08", "sku": 30243621},
    # SKU leve — força aparente cai, mas rolo pode estar OK
    {"data_inicio": "2026-06-08", "data_fim": "2026-06-20", "sku": 30242137},
    # SKU muito leve — alta chance de falso negativo sem normalização
    {"data_inicio": "2026-06-20", "data_fim": "2026-07-05", "sku": 30241637},
]


# ── Funções principais ────────────────────────────────────────────────────────

def get_sku_factor(sku_code) -> float:
    """Retorna o fator de stress normalizado para o SKU informado.

    Parameters
    ----------
    sku_code : int, float ou str
        Código do SKU (ex.: 30244719 ou 30244719.0).

    Returns
    -------
    float
        Fator de stress ∈ (0, ∞), onde 1.0 = demanda de referência.
    """
    if sku_code is None or (isinstance(sku_code, float) and np.isnan(sku_code)):
        return FATOR_DESCONHECIDO
    try:
        key = int(float(sku_code))
    except (ValueError, TypeError):
        return FATOR_DESCONHECIDO
    fator = SKU_CATALOG.get(key, FATOR_DESCONHECIDO)
    if fator == FATOR_DESCONHECIDO and key not in SKU_CATALOG:
        logger.debug("SKU %s não encontrado no catálogo — usando fator %.3f", key, FATOR_DESCONHECIDO)
    return fator


def normalizar_media(
    df_forca: pd.DataFrame,
    df_sku: pd.DataFrame,
    col_sku: str = "bag1_sku",
    col_media: str = "Media",
    tolerancia: str = "3h",
) -> pd.DataFrame:
    """Adiciona coluna ``Media_norm`` ao dataframe de força.

    Faz um merge_asof entre os timestamps de força e os de SKU, depois
    divide ``Media`` pelo fator do SKU vigente.

    Parameters
    ----------
    df_forca : pd.DataFrame
        Index = Timestamp tz-naive, coluna ``Media`` (N).
    df_sku : pd.DataFrame
        Colunas ``ts`` (tz-naive) e ``bag1_sku`` (ou o col_sku indicado).
    col_sku : str
        Coluna de SKU a usar como referência (padrão: bag1_sku).
    col_media : str
        Coluna de força bruta (padrão: Media).
    tolerancia : str
        Tolerância de tempo para o merge_asof (padrão: 3h).

    Returns
    -------
    pd.DataFrame
        df_forca com colunas adicionais: ``sku_codigo``, ``sku_fator``, ``Media_norm``.
    """
    df = df_forca.copy()

    sku_clean = df_sku[["ts", col_sku]].dropna(subset=["ts"]).sort_values("ts").copy()

    forca_reset = df.reset_index()
    if "Timestamp" not in forca_reset.columns:
        forca_reset = forca_reset.rename(columns={forca_reset.columns[0]: "Timestamp"})

    # Alinhar timezones: merge_asof falha silenciosamente se left e right
    # tiverem tz diferentes (tz-aware vs tz-naive).
    forca_tz = forca_reset["Timestamp"].dt.tz
    if forca_tz is not None:
        sku_clean["ts"] = pd.to_datetime(sku_clean["ts"], utc=True).dt.tz_convert(forca_tz)
    else:
        sku_clean["ts"] = pd.to_datetime(sku_clean["ts"]).dt.tz_localize(None)

    merged = pd.merge_asof(
        forca_reset.sort_values("Timestamp"),
        sku_clean.rename(columns={col_sku: "sku_codigo"}),
        left_on="Timestamp",
        right_on="ts",
        direction="nearest",
        tolerance=pd.Timedelta(tolerancia),
    )
    merged["sku_fator"] = merged["sku_codigo"].apply(get_sku_factor)
    merged["Media_norm"] = merged[col_media] / merged["sku_fator"]
    merged = merged.set_index("Timestamp").sort_index()

    df["sku_codigo"] = merged["sku_codigo"]
    df["sku_fator"]  = merged["sku_fator"]
    df["Media_norm"] = merged["Media_norm"]
    return df


def upcoming_sku_factor(
    data_referencia: pd.Timestamp,
    janela_dias: int = 14,
    upcoming: Optional[List[Dict]] = None,
) -> float:
    """Calcula o fator médio ponderado dos SKUs programados nos próximos N dias.

    Útil para ajustar o limiar de alerta antes de uma mudança de SKU:
    - upcoming_factor > 1 → próximo período é mais exigente → alertar mais cedo
    - upcoming_factor < 1 → próximo período é mais leve → força bruta vai cair (não é degradação)

    Parameters
    ----------
    data_referencia : pd.Timestamp
        Data a partir da qual contar os próximos ``janela_dias``.
    janela_dias : int
        Número de dias à frente para calcular o fator médio.
    upcoming : list of dict, optional
        Lista de SKUs futuros. Usa ``UPCOMING_SKUS`` se None.

    Returns
    -------
    float
        Fator de stress médio ponderado pelos dias de cada SKU na janela.
        Retorna 1.0 se não há dados futuros na janela.
    """
    if upcoming is None:
        upcoming = UPCOMING_SKUS

    data_fim_janela = data_referencia + pd.Timedelta(days=janela_dias)
    total_dias = 0
    soma_ponderada = 0.0

    for item in upcoming:
        ini = pd.Timestamp(item["data_inicio"])
        fim = pd.Timestamp(item["data_fim"])
        sku = item["sku"]

        overlap_ini = max(ini, data_referencia)
        overlap_fim = min(fim, data_fim_janela)
        dias = (overlap_fim - overlap_ini).days

        if dias > 0:
            fator = get_sku_factor(sku)
            soma_ponderada += fator * dias
            total_dias += dias

    if total_dias == 0:
        return FATOR_DESCONHECIDO

    return round(soma_ponderada / total_dias, 4)


def proj_48h_ajustada(
    proj_48h_norm: float,
    upcoming_factor: float,
) -> float:
    """Converte projeção normalizada para força bruta esperada com o SKU futuro.

    proj_48h_ajustada = proj_48h_norm × upcoming_factor

    Se upcoming_factor > 1 (SKU pesado vindo), força bruta sobe — mas rolo
    trabalhará mais, acelerando o desgaste.
    Se upcoming_factor < 1 (SKU leve vindo), força bruta cai — pode mascarar
    degradação real.
    """
    return round(proj_48h_norm * upcoming_factor, 1)


def resumo_catalog() -> pd.DataFrame:
    """Retorna o catálogo de SKUs como DataFrame ordenado por fator."""
    rows = [{"sku": k, "fator": v,
             "demanda": "pesado" if v > 1.05 else ("leve" if v < 0.90 else "normal")}
            for k, v in sorted(SKU_CATALOG.items(), key=lambda x: -x[1])]
    return pd.DataFrame(rows)


# ── Auto-calibração por phantom (sem catálogo estático) ──────────────────────

def calibrar_fatores_phantom(
    df_forca: pd.DataFrame,
    df_phantom: pd.DataFrame,
    troca_dates: list,
    col_phantom: str = "bag1_sku",
    col_media: str = "Media",
    fresh_dias: int = 10,
    min_obs: int = 5,
    tolerancia: str = "3h",
) -> dict:
    """Calibra fatores de stress por phantom code a partir de dados históricos.

    Para cada phantom code encontrado no histórico:
      1. Identifica leituras dos primeiros `fresh_dias` dias de cada ciclo
         (rolo novo = sem degradação por envelhecimento)
      2. Calcula a força média nesse período fresco, por phantom
      3. Normaliza pela mediana global das forças frescas

    fresh_dias=10 : os primeiros 10 dias capturam a demanda do phantom antes
    que o desgaste do rolo contamine a leitura. Phantoms com < `min_obs`
    leituras frescas recebem fator=1.0 (baseline neutro).

    Parameters
    ----------
    df_forca : pd.DataFrame
        Deve ter coluna 'ts' (tz-aware, UTC) e col_media.
    df_phantom : pd.DataFrame
        Deve ter colunas 'index' (tz-aware) e col_phantom.
    troca_dates : list
        Lista de datetimes das trocas de rolo (UTC).
    col_phantom : str
        Nome da coluna de phantom no df_phantom.
    col_media : str
        Coluna de força bruta a calibrar.
    fresh_dias : int
        Dias iniciais de cada ciclo considerados como referência fresca.
    min_obs : int
        Mínimo de leituras frescas para incluir o phantom na calibração.
    tolerancia : str
        Tolerância para merge_asof entre força e phantom.

    Returns
    -------
    dict
        {phantom_code: fator}  — 1.0 = demanda de referência (mediana global)
    """
    if df_phantom is None or df_phantom.empty or col_phantom not in df_phantom.columns:
        return {}

    # Normalizar timestamps do phantom
    ph = df_phantom[["index", col_phantom]].copy()
    ph = ph.rename(columns={"index": "ts"})
    ph["ts"] = pd.to_datetime(ph["ts"], utc=True)
    ph = ph.dropna(subset=["ts"]).sort_values("ts")

    # Merge força ↔ phantom (forward-fill dentro da tolerância)
    forca = df_forca[["ts", col_media]].copy().sort_values("ts")
    forca["ts"] = pd.to_datetime(forca["ts"], utc=True)

    merged = pd.merge_asof(
        forca,
        ph.rename(columns={col_phantom: "_phantom"}),
        on="ts",
        direction="nearest",
        tolerance=pd.Timedelta(tolerancia),
    )

    # Anotar horas desde a troca mais recente antes de cada leitura
    troca_ts = sorted(pd.to_datetime(t, utc=True) for t in troca_dates)
    horas = np.full(len(merged), np.nan)
    for i, t in enumerate(troca_ts):
        t_fim = troca_ts[i + 1] if i + 1 < len(troca_ts) else pd.Timestamp.max.tz_localize("UTC")
        mask = (merged["ts"] >= t) & (merged["ts"] < t_fim)
        horas[mask] = (merged.loc[mask, "ts"] - t).dt.total_seconds().values / 3600.0
    merged["_horas"] = horas

    # Filtrar janela fresca
    fresh = merged[(merged["_horas"] >= 0) & (merged["_horas"] <= fresh_dias * 24)].copy()
    fresh = fresh.dropna(subset=["_phantom", col_media])
    if fresh.empty:
        return {}

    # Força média fresca por phantom
    baseline = (
        fresh.groupby("_phantom")[col_media]
        .agg(["mean", "count"])
        .rename(columns={"mean": "forca_fresca", "count": "n_obs"})
    )
    baseline = baseline[baseline["n_obs"] >= min_obs]
    if baseline.empty:
        return {}

    mediana_global = float(baseline["forca_fresca"].median())
    if mediana_global <= 0:
        return {}

    catalog = {
        phantom: round(row["forca_fresca"] / mediana_global, 4)
        for phantom, row in baseline.iterrows()
    }
    logger.debug(
        "Phantom calibration: %d phantoms | mediana_fresca=%.1f N | range=%.3f–%.3f",
        len(catalog), mediana_global, min(catalog.values()), max(catalog.values()),
    )
    return catalog


def normalizar_media_phantom(
    df_forca: pd.DataFrame,
    df_phantom: pd.DataFrame,
    troca_dates: list,
    col_phantom: str = "bag1_sku",
    col_media: str = "Media",
    catalog: Optional[Dict] = None,
    fresh_dias: int = 10,
    tolerancia: str = "3h",
) -> pd.DataFrame:
    """Adiciona ``Media_norm`` ao dataframe de força — versão auto-calibrada.

    Se ``catalog`` for fornecido, usa-o diretamente (retrocompatível com FB14).
    Se ``catalog`` for None, calibra automaticamente via fresh-roller baseline
    a partir de ``df_phantom`` e ``troca_dates``.

    Parameters
    ----------
    df_forca : pd.DataFrame
        Index = Timestamp tz-aware (ou coluna 'ts'), coluna col_media.
    df_phantom : pd.DataFrame
        Colunas 'index' (tz-aware) e col_phantom.
    troca_dates : list
        Lista de datetimes das trocas de rolo.
    col_phantom : str
        Coluna de phantom em df_phantom.
    col_media : str
        Coluna de força bruta (padrão: Media).
    catalog : dict ou None
        Catálogo pré-calibrado {phantom_code: fator}.
        Se None, auto-calibra a partir dos dados.
    fresh_dias : int
        Dias iniciais usados na auto-calibração.
    tolerancia : str
        Tolerância para merge_asof.

    Returns
    -------
    pd.DataFrame
        df_forca com colunas adicionais: ``phantom_codigo``, ``phantom_fator``, ``Media_norm``.
    """
    if catalog is None:
        catalog = calibrar_fatores_phantom(
            df_forca, df_phantom, troca_dates,
            col_phantom=col_phantom, col_media=col_media,
            fresh_dias=fresh_dias, tolerancia=tolerancia,
        )

    if not catalog:
        logger.warning("Catálogo de phantoms vazio — Media_norm não gerada.")
        df_out = df_forca.copy()
        df_out["phantom_codigo"] = None
        df_out["phantom_fator"]  = 1.0
        df_out["Media_norm"]     = df_out[col_media]
        return df_out

    # Preparar phantom df para merge
    ph = df_phantom[["index", col_phantom]].copy()
    ph = ph.rename(columns={"index": "ts", col_phantom: "phantom_codigo"})
    ph["ts"] = pd.to_datetime(ph["ts"], utc=True)
    ph = ph.dropna(subset=["ts"]).sort_values("ts")

    # Preparar df_forca
    df = df_forca.copy()
    has_ts_index = isinstance(df.index, pd.DatetimeIndex)
    if has_ts_index:
        df = df.reset_index().rename(columns={df.index.name or "index": "ts"})
    elif "ts" not in df.columns:
        ts_col = df.columns[0]
        df = df.rename(columns={ts_col: "ts"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)

    # Alinhar timezone do phantom
    forca_tz = df["ts"].dt.tz
    if forca_tz is not None:
        ph["ts"] = ph["ts"].dt.tz_convert(forca_tz)

    merged = pd.merge_asof(
        df.sort_values("ts"),
        ph,
        on="ts",
        direction="nearest",
        tolerance=pd.Timedelta(tolerancia),
    )
    merged["phantom_fator"] = merged["phantom_codigo"].map(
        lambda c: catalog.get(c, FATOR_DESCONHECIDO)
    )
    merged["Media_norm"] = merged[col_media] / merged["phantom_fator"]

    if has_ts_index:
        merged = merged.set_index("ts").sort_index()

    df_forca["phantom_codigo"] = merged["phantom_codigo"].values
    df_forca["phantom_fator"]  = merged["phantom_fator"].values
    df_forca["Media_norm"]     = merged["Media_norm"].values
    return df_forca
