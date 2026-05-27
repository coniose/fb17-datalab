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
