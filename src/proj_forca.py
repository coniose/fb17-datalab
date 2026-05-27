"""
proj_forca.py — Projeção de força de selagem para as próximas 48h (age-gated).

Motivação
---------
Em maintackers jovens (< min_idade_dias), os sinais de força ainda estão se
estabilizando após a troca. A extrapolação linear nessa janela produz ruído, não
sinal. Para maintackers mais velhos, a tendência de degradação se estabelece e a
regressão linear sobre os últimos N dias é confiável o suficiente para projetar.

Parâmetros aceitos em config.yaml (seção 'projecao')
-----------------------------------------------------
  min_idade_dias:        20   # não projeta para maintackers mais novos que isso
  janela_regressao_dias: 14   # janela de dados históricos para a regressão OLS
  min_pontos:            24   # mínimo de pontos horários na janela (1 dia)
  horizonte_h:           48   # quantas horas à frente projetar
  r2_minimo:             0.0  # R² mínimo para aceitar a projeção (0.0 = sem filtro)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
MIN_IDADE_DIAS        = 20
JANELA_REGRESSAO_DIAS = 14
MIN_PONTOS            = 24
HORIZONTE_H           = 48
R2_MINIMO             = 0.0


def _load_proj_config(state_path: Optional[Path] = None) -> dict:
    candidates = [
        state_path,
        Path("config.yaml"),
        Path("../config.yaml"),
    ]
    for p in candidates:
        if p and Path(p).exists():
            try:
                import yaml
                with open(p, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                return cfg.get("projecao", {}) if isinstance(cfg, dict) else {}
            except Exception:
                pass
    return {}


def calcular_proj_48h(
    df_hourly: pd.DataFrame,
    troca_date: datetime,
    today: pd.Timestamp,
    col: str = "Media",
    config: Optional[dict] = None,
) -> float:
    """
    Projeta a força média 48h à frente por regressão linear OLS (age-gated).

    Retorna NaN quando:
    - maintacker com menos de `min_idade_dias` dias (sinal instável)
    - dados insuficientes na janela de regressão
    - R² abaixo de `r2_minimo` (tendência indefinida)

    Args:
        df_hourly:   DataFrame com DateTimeIndex horário e coluna `col`
        troca_date:  início do ciclo atual (datetime ou date)
        today:       dia de avaliação (Timestamp, date ou str ISO)
        col:         coluna de força — "Media" ou "Media_norm"
        config:      dict com parâmetros (lido de config.yaml se None)
    """
    if config is None:
        config = _load_proj_config()

    min_idade   = int(config.get("min_idade_dias",        MIN_IDADE_DIAS))
    janela_dias = int(config.get("janela_regressao_dias", JANELA_REGRESSAO_DIAS))
    min_pontos  = int(config.get("min_pontos",            MIN_PONTOS))
    horizonte_h = int(config.get("horizonte_h",           HORIZONTE_H))
    r2_min      = float(config.get("r2_minimo",           R2_MINIMO))

    today = pd.Timestamp(today).normalize()
    troca = pd.Timestamp(troca_date)

    # Normaliza timezone: se df tem tz e today não tem, alinha
    if df_hourly.index.tz is not None and today.tz is None:
        today = today.tz_localize(df_hourly.index.tz)
    elif df_hourly.index.tz is None and today.tz is not None:
        today = today.tz_localize(None)
    if troca.tz is None and today.tz is not None:
        troca = troca.tz_localize(today.tz)
    elif troca.tz is not None and today.tz is None:
        troca = troca.tz_localize(None)

    age_days = (today - troca).days

    # ── Gate de idade ─────────────────────────────────────────────────────────
    if age_days < min_idade:
        return float("nan")

    # ── Janela de dados ───────────────────────────────────────────────────────
    inicio = today - pd.Timedelta(days=janela_dias)
    fim    = today + pd.Timedelta(hours=23, minutes=59)

    if col not in df_hourly.columns:
        logger.warning("proj_forca: coluna '%s' não existe em df_hourly.", col)
        return float("nan")

    sub = df_hourly.loc[inicio:fim, col].dropna()

    if len(sub) < min_pontos:
        logger.debug(
            "proj_forca: pontos insuficientes (%d < %d) em %s — age=%dd",
            len(sub), min_pontos, today.date(), age_days,
        )
        return float("nan")

    # ── Regressão OLS: x = horas desde início da janela, y = força ───────────
    x  = (sub.index - sub.index[0]).total_seconds().values / 3600.0
    y  = sub.values.astype(float)
    xm = x.mean()
    ym = y.mean()

    ss_xx = ((x - xm) ** 2).sum()
    if ss_xx == 0:
        return float("nan")

    slope     = ((x - xm) * (y - ym)).sum() / ss_xx
    intercept = ym - slope * xm

    # ── Filtro R² opcional ────────────────────────────────────────────────────
    if r2_min > 0.0:
        y_pred = slope * x + intercept
        ss_res = ((y - y_pred) ** 2).sum()
        ss_tot = ((y - ym) ** 2).sum()
        r2     = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        if r2 < r2_min:
            logger.debug(
                "proj_forca: R²=%.3f < r2_min=%.3f em %s — retornando NaN",
                r2, r2_min, today.date(),
            )
            return float("nan")

    # ── Projeção: horas desde início da janela até today + horizonte_h ────────
    horas_hoje = (today - sub.index[0]).total_seconds() / 3600.0
    proj       = slope * (horas_hoje + horizonte_h) + intercept

    logger.debug(
        "proj_forca: age=%dd slope=%.3f N/h (%.1f N/dia) → %.0f N  [%s]",
        age_days, slope, slope * 24, proj, today.date(),
    )
    return float(proj)


def adicionar_proj_48h_backtest(
    eventos: pd.DataFrame,
    df_hourly: pd.DataFrame,
    col: str = "Media",
    config: Optional[dict] = None,
) -> pd.Series:
    """
    Calcula proj_48h para cada linha de um DataFrame de eventos do backtest.

    Colunas esperadas em `eventos`:
        data_disparo  — Timestamp ou date (dia de avaliação)
        troca_ini     — Timestamp ou date (início do ciclo)

    Retorna pd.Series alinhada pelo índice de `eventos`.
    Mantém valores existentes quando o novo cálculo retorna NaN
    (não piora o que já existe).
    """
    if config is None:
        config = _load_proj_config()

    novos = {}
    for idx, ev in eventos.iterrows():
        v = calcular_proj_48h(
            df_hourly  = df_hourly,
            troca_date = pd.Timestamp(ev["troca_ini"]),
            today      = pd.Timestamp(ev["data_disparo"]),
            col        = col,
            config     = config,
        )
        novos[idx] = v

    nova_serie = pd.Series(novos, name="proj_48h")

    # Preserva valor existente quando a regressão não produz resultado
    if "proj_48h" in eventos.columns:
        existente = eventos["proj_48h"]
        return nova_serie.where(nova_serie.notna(), other=existente)

    return nova_serie
