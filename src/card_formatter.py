"""
card_formatter.py v4.0 — Adaptive Card builder para alertas de selagem no Microsoft Teams.

Dois tipos de cartão:
  build_risco_card   — leitura anômala isolada (g/f < 800, sem degradação)
  build_critico_card — degradação + envelhecimento, com sub_nivel:
                         AVISO       : aviso precoce
                         CONFIRMADO  : degradação confirmada
                         FIM_DE_VIDA : vida útil projetada atingida
"""

import json
import math
from datetime import datetime
from typing import Optional

VIDA_REF_DIAS: float = 45.0

_CRITICO_META = {
    "AVISO": {
        "style": "attention",
        "titulo": "AVISO — Degradacao Detectada",
        "bg": None,
    },
    "CONFIRMADO": {
        "style": "attention",
        "titulo": "ALERTA CRÍTICO",
        "bg": None,
    },
    "FIM_DE_VIDA": {
        "style": "default",
        "titulo": "FIM DE VIDA — Troca Imediata",
        "bg": "#1a1a1a",
    },
}

_BAR_WIDTH = 20


def _barra_vida(consumida: float) -> str:
    preenchido = round(consumida * _BAR_WIDTH)
    vazio = _BAR_WIDTH - preenchido
    return "█" * preenchido + "░" * vazio


def _label_tendencia(slope_n_por_dia: Optional[float]) -> str:
    if slope_n_por_dia is None:
        return "—"
    val = f"{slope_n_por_dia:+.0f} gf/dia"
    if slope_n_por_dia < -10:
        return f"Declinio acentuado ({val})"
    if slope_n_por_dia < -3:
        return f"Declinio leve ({val})"
    return f"Estavel ({val})"


def _label_risco(p_risk: float) -> str:
    pct = round(p_risk * 100)
    if p_risk >= 0.60:
        return f"Alto ({pct}%)"
    if p_risk >= 0.35:
        return f"Medio ({pct}%)"
    return f"Baixo ({pct}%)"


def _fmt_gf(v: Optional[float]) -> str:
    return f"{round(v)} gf" if v is not None and not math.isnan(v) else "—"


def _cabecalho(titulo: str, subtitulo: str, maquina: str, data_str: str,
               style: str, bg: Optional[str]) -> dict:
    container: dict = {
        "type": "Container",
        "style": style,
        "bleed": True,
        "items": [{
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": titulo,
                     "weight": "Bolder", "size": "Large", "color": "Light", "wrap": True},
                    {"type": "TextBlock", "text": subtitulo,
                     "color": "Light", "isSubtle": True, "spacing": "None", "wrap": True},
                ]},
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": maquina,
                     "weight": "Bolder", "size": "ExtraLarge", "color": "Light",
                     "horizontalAlignment": "Right"},
                    {"type": "TextBlock", "text": data_str,
                     "color": "Light", "isSubtle": True, "spacing": "None",
                     "size": "Small", "horizontalAlignment": "Right"},
                ]},
            ],
        }],
    }
    if bg:
        container["backgroundColor"] = bg
    return container


def _barra_container(idade_dias: int, vida_ref_dias: float) -> tuple[dict, float]:
    consumida     = min(idade_dias / vida_ref_dias, 1.0)
    pct_consumida = round(consumida * 100)
    dias_restantes = max(0, round((1.0 - consumida) * vida_ref_dias))
    barra = _barra_vida(consumida)
    container = {
        "type": "Container",
        "style": "emphasis",
        "spacing": "Medium",
        "items": [
            {"type": "TextBlock", "text": "VIDA DO MAINTACKER",
             "weight": "Bolder", "size": "Small", "isSubtle": True, "spacing": "Small"},
            {"type": "TextBlock", "text": f"{barra}  **{pct_consumida}% consumida**",
             "fontType": "Monospace", "spacing": "Small", "wrap": False},
            {"type": "ColumnSet", "spacing": "Small", "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": f"{idade_dias} dias em operacao",
                     "isSubtle": True, "size": "Small"}]},
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": f"~{dias_restantes} dias para troca",
                     "isSubtle": True, "size": "Small", "horizontalAlignment": "Right"}]},
            ]},
        ],
    }
    return container, consumida


def _acao_container(acao_recomendada: str) -> dict:
    return {
        "type": "Container",
        "style": "emphasis",
        "spacing": "Medium",
        "items": [
            {"type": "TextBlock", "text": "ACAO RECOMENDADA",
             "weight": "Bolder", "size": "Small", "isSubtle": True, "spacing": "Small"},
            {"type": "TextBlock", "text": acao_recomendada,
             "wrap": True, "weight": "Bolder", "spacing": "Small"},
        ],
    }


def _indicadores_container(facts: list) -> dict:
    return {
        "type": "Container",
        "spacing": "Medium",
        "items": [
            {"type": "TextBlock", "text": "INDICADORES",
             "weight": "Bolder", "size": "Small", "isSubtle": True},
            {"type": "FactSet", "facts": facts},
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# RISCO — leitura anômala isolada
# ─────────────────────────────────────────────────────────────────────────────
def build_risco_card(
    maquina: str,
    idade_dias: int,
    forca_min: float,
    data_forca_min: str,
    n_abaixo_800_ciclo: int,
    p_risk: float,
    data_disparo: Optional[datetime] = None,
    media_3d: Optional[float] = None,
    acao_recomendada: str = (
        "Ir ao local e verificar os motivos da forca de selagem abaixo de 800 gf. "
        "Rolo sem degradacao associada."
    ),
) -> str:
    data_str = (data_disparo or datetime.now()).strftime("%d/%m/%Y %H:%M")

    try:
        data_evento_fmt = datetime.strptime(data_forca_min, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        data_evento_fmt = data_forca_min

    forca_str    = f"{round(forca_min)} gf" if not math.isnan(forca_min) else "—"
    media_3d_str = _fmt_gf(media_3d)

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            _cabecalho(
                titulo    = "RISCO — Leitura Anomala",
                subtitulo = "Forca de Selagem — Rolo Maintacker",
                maquina   = maquina,
                data_str  = data_str,
                style     = "warning",
                bg        = None,
            ),
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Medium",
                "items": [
                    {"type": "TextBlock", "text": "EVENTO",
                     "weight": "Bolder", "size": "Small", "isSubtle": True, "spacing": "Small"},
                    {"type": "FactSet", "facts": [
                        {"title": "Forca minima registrada", "value": forca_str},
                        {"title": "Forca media (3 dias)",    "value": media_3d_str},
                        {"title": "Data do evento",          "value": data_evento_fmt},
                        {"title": "< 800 gf no ciclo",       "value": f"{n_abaixo_800_ciclo}x"},
                    ]},
                ],
            },
            {
                "type": "Container",
                "spacing": "Medium",
                "items": [
                    {"type": "TextBlock", "text": "STATUS DO ROLO",
                     "weight": "Bolder", "size": "Small", "isSubtle": True},
                    {"type": "FactSet", "facts": [
                        {"title": "Dias em operacao", "value": f"{idade_dias} dias"},
                    ]},
                ],
            },
            _acao_container(acao_recomendada),
        ],
    }
    return json.dumps(card, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# CRITICO — degradação + envelhecimento (AVISO / CONFIRMADO / FIM_DE_VIDA)
# ─────────────────────────────────────────────────────────────────────────────
def build_critico_card(
    maquina: str,
    sub_nivel: str,
    idade_dias: int,
    p_risk: float,
    slope_7d: Optional[float],
    forca_min_3d: Optional[float],
    proj_48h: Optional[float],
    media_7d: Optional[float],
    media_7d_anterior: Optional[float],
    acao_recomendada: str,
    data_disparo: Optional[datetime] = None,
    vida_ref_dias: float = VIDA_REF_DIAS,
    eventos_risco_ciclo: int = 0,
    forca_min_ciclo: Optional[float] = None,
) -> str:
    meta     = _CRITICO_META.get(sub_nivel, _CRITICO_META["CONFIRMADO"])
    data_str = (data_disparo or datetime.now()).strftime("%d/%m/%Y %H:%M")

    barra_container, consumida = _barra_container(idade_dias, vida_ref_dias)

    # Para FIM_DE_VIDA: mostrar quanto passou do ETA quando consumida > 1
    if consumida >= 1.0 and sub_nivel == "FIM_DE_VIDA":
        dias_alem = idade_dias - round(vida_ref_dias)
        barra_container["items"][2]["columns"][1]["items"][0]["text"] = (
            f"{dias_alem} dias alem do ETA"
        )

    if sub_nivel == "FIM_DE_VIDA":
        indicadores_facts = [
            {"title": "Forca Minima (3 dias)",  "value": _fmt_gf(forca_min_3d)},
            {"title": "Forca Projetada (48h)",  "value": _fmt_gf(proj_48h)},
            {"title": "Media 7d atual",         "value": _fmt_gf(media_7d)},
            {"title": "Tendencia de Forca",     "value": _label_tendencia(slope_7d)},
            {"title": "Risco acumulado",        "value": _label_risco(p_risk)},
        ]
    elif sub_nivel == "CONFIRMADO":
        indicadores_facts = [
            {"title": "Tendência de Força",          "value": _label_tendencia(slope_7d)},
            {"title": "Força Projetada (48h)",        "value": _fmt_gf(proj_48h)},
            {"title": "Força Mínima (3 dias)",        "value": _fmt_gf(forca_min_3d)},
            {"title": "Força Mínima do Ciclo",        "value": _fmt_gf(forca_min_ciclo)},
            {"title": "< 800 gf no ciclo atual",      "value": f"{eventos_risco_ciclo}x"},
            {"title": "Média da Semana Atual",        "value": _fmt_gf(media_7d)},
            {"title": "Média da Semana Retrasada",    "value": _fmt_gf(media_7d_anterior)},
        ]
    else:  # AVISO
        indicadores_facts = [
            {"title": "Tendencia de Forca",    "value": _label_tendencia(slope_7d)},
            {"title": "Forca Projetada (48h)", "value": _fmt_gf(proj_48h)},
            {"title": "Media 7d atual",        "value": _fmt_gf(media_7d)},
            {"title": "Risco acumulado",       "value": _label_risco(p_risk)},
        ]

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            _cabecalho(
                titulo    = meta["titulo"],
                subtitulo = "Forca de Selagem — Rolo Maintacker",
                maquina   = maquina,
                data_str  = data_str,
                style     = meta["style"],
                bg        = meta["bg"],
            ),
            barra_container,
            _indicadores_container(indicadores_facts),
            _acao_container(acao_recomendada),
        ],
    }
    return json.dumps(card, ensure_ascii=False, indent=2)


# ── Teste com dados sintéticos ─────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import datetime

    print("=== RISCO ===")
    print(build_risco_card(
        maquina="FB14", idade_dias=8, forca_min=762.0,
        data_forca_min="2026-05-24", n_abaixo_800_ciclo=1,
        p_risk=0.12, media_3d=980.0,
        data_disparo=datetime(2026, 5, 26, 8, 0),
    )[:200], "...")

    for sub in ("AVISO", "CONFIRMADO", "FIM_DE_VIDA"):
        print(f"\n=== CRITICO / {sub} ===")
        print(build_critico_card(
            maquina="FB14", sub_nivel=sub, idade_dias=38,
            p_risk=0.72, slope_7d=-9.1, forca_min_3d=790.0,
            proj_48h=755.0, media_7d=820.0, media_7d_anterior=870.0,
            acao_recomendada="Troca do rolo maintacker urgente.",
            data_disparo=datetime(2026, 5, 26, 8, 0),
            vida_ref_dias=45.0, eventos_risco_ciclo=3,
        )[:200], "...")
