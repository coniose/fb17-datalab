"""
card_formatter.py — Adaptive Card builder para alertas de selagem no Microsoft Teams.

Recebe os campos de um item da lista Gatilhos_Selagem e retorna JSON string pronto
para ser gravado na coluna TeamsPayload e postado via Power Automate.

Tradução intencional: jargão técnico → linguagem gerencial.
"""

import json
import math
from datetime import datetime
from typing import Optional


# Dias de operação considerados "vida plena" para escala da barra.
# Baseado em P50 Weibull (~39.6d) + margem operacional.
VIDA_REF_DIAS: float = 45.0

_SEVERIDADE_META = {
    "RED":        {"style": "default",   "titulo": "⚫ FIM DE VIDA DO MAINTACKER",  "bg": "#1a1a1a"},
    "AMARELO":    {"style": "attention", "titulo": "🔴 ALERTA CRÍTICO",             "bg": None},
    "EMERGENCIA": {"style": "attention", "titulo": "🚨 EMERGÊNCIA",                 "bg": None},
    "REVISAO":    {"style": "accent",    "titulo": "📋 REVISÃO DE CICLO",           "bg": None},
    "RISCO":      {"style": "warning",   "titulo": "⚠️ RISCO — Leitura Anômala",   "bg": None},
}

_BAR_WIDTH = 20  # caracteres de largura da barra de vida


def _barra_vida(consumida: float) -> str:
    preenchido = round(consumida * _BAR_WIDTH)
    vazio = _BAR_WIDTH - preenchido
    return "█" * preenchido + "░" * vazio


def _label_tendencia(slope_n_por_dia: Optional[float]) -> str:
    if slope_n_por_dia is None:
        return "—"
    val = f"{slope_n_por_dia:+.0f} gf/dia"
    if slope_n_por_dia < -10:
        return f"▼▼ Declínio acentuado ({val})"
    if slope_n_por_dia < -3:
        return f"▼ Declínio leve ({val})"
    if slope_n_por_dia > 3:
        return f"▲ Recuperando ({val})"
    return f"→ Estável ({val})"


def _label_risco(p_risk: float) -> str:
    pct = round(p_risk * 100)
    if p_risk >= 0.60:
        return f"Alto ({pct}%)"
    if p_risk >= 0.35:
        return f"Médio ({pct}%)"
    return f"Baixo ({pct}%)"


def build_ai_prompt(
    maquina: str,
    gatilho: str,
    idade_dias: int,
    p_risk: float,
    slope_7d: Optional[float],
    forca_min_3d: Optional[float],
    proj_48h: Optional[float],
    acao_recomendada: str,
    data_disparo: Optional[datetime] = None,
    # Contexto histórico — derivado de troca_modulo.csv (30 ciclos FB14)
    hist_ciclos: int = 30,
    hist_p50_dias: float = 41.5,
    hist_media_dias: float = 50.1,
    hist_p10_dias: float = 11.0,
    hist_ciclos_recentes: str = "19d, 27d, 49d, 21d, 23d",
    # Parâmetros Weibull (config.yaml)
    weibull_beta: float = 1.181,
    weibull_eta_dias: float = 54.05,
) -> str:
    """
    Monta prompt estruturado para consulta à IA interna da planta.

    Inclui: como o algoritmo funciona, limites reais da FB14, contexto
    histórico dos ciclos e a situação atual — tudo que a IA precisa para
    recuperar a documentação certa e dar uma resposta assertiva.
    """
    data_str = (data_disparo or datetime.now()).strftime("%d/%m/%Y %H:%M")
    severidade = {
        "RED": "ALTA", "AMARELO": "MÉDIA",
        "EMERGENCIA": "CRÍTICA", "REVISAO": "INFO",
    }.get(gatilho, "ALTA")

    # Weibull CDF: F(t) = 1 - exp(-(t/η)^β)  — só usa math (stdlib)
    age_risk = 1.0 - math.exp(-((idade_dias / weibull_eta_dias) ** weibull_beta))
    age_risk_pct = round(age_risk * 100, 1)
    p_risk_pct = round(p_risk * 100, 1)

    slope_str = f"{slope_7d:+.1f} gf/dia" if slope_7d is not None else "—"
    tendencia = _label_tendencia(slope_7d)
    forca_min_str = f"{round(forca_min_3d)} gf" if forca_min_3d is not None else "—"
    proj_str = f"{round(proj_48h)} gf" if proj_48h is not None else "—"

    # Significado histórico: em quantos % dos ciclos a troca ocorreu antes desta idade
    pct_ciclos_mais_curtos = round(
        sum(1 for d in [19, 27, 49, 21, 23, 23, 21, 26, 17, 185]
            if d <= idade_dias) / 10 * 100
    )

    linhas = [
        f"[SISTEMA DE MANUTENÇÃO PREDITIVA — {maquina} Rolo Maintacker]",
        f"Gerado em: {data_str} | Gatilho: {gatilho} | Severidade: {severidade}",
        "",
        "━━━ COMO O ALGORITMO CALCULA O RISCO ━━━",
        f"O sistema usa modelo híbrido (Weibull + sinal de força) em duas camadas:",
        "",
        f"  1. age_risk = Weibull CDF(idade={idade_dias}d)",
        f"     β={weibull_beta}, η={weibull_eta_dias:.1f}d",
        f"     Calibrado em {hist_ciclos} ciclos históricos reais da FB14",
        f"     → age_risk atual: {age_risk_pct}%",
        "",
        f"  2. signal_score = degradação do sinal (ratio média_3d/média_14d + slope)",
        f"     boost de amplificação: 0.65",
        "",
        f"  3. p_risk = age_risk + (1 − age_risk) × signal_score × 0.65",
        f"     → p_risk atual: {p_risk_pct}%",
        "",
        "━━━ LIMITES OPERACIONAIS DA MÁQUINA FB14 ━━━",
        "  EMERGÊNCIA : força mínima (3d) < 800 gf → intervenção imediata",
        "  RED        : p_risk ≥ 48% E signal ≥ 22% E projeção 48h < 800 gf (≥2 dias consecutivos)",
        "  AMARELO    : p_risk ≥ 35% OU signal ≥ 15% (aviso precoce)",
        "  REVISÃO    : marcos automáticos nos dias 20, 25 e 35 do ciclo",
        "  Cooldown   : RED 48h | AMARELO 72h | EMERGÊNCIA 48h | Snooze pós-OK: 5 dias",
        "",
        f"━━━ CONTEXTO HISTÓRICO ({hist_ciclos} ciclos FB14) ━━━",
        f"  Duração mediana (P50): {hist_p50_dias}d | Média: {hist_media_dias}d",
        f"  Ciclos curtos (P10): ~{hist_p10_dias}d | Variação: 1d a 185d",
        f"  Últimos 5 ciclos   : {hist_ciclos_recentes}",
        f"  Referência: com {idade_dias} dias de operação, aproximadamente {pct_ciclos_mais_curtos}%",
        f"  dos ciclos recentes já haviam sido encerrados (troca realizada).",
        "",
        "━━━ SITUAÇÃO ATUAL ━━━",
        f"  Máquina            : {maquina}",
        f"  Dias em operação   : {idade_dias}d",
        f"  Gatilho ativo      : {gatilho}",
        f"  p_risk             : {p_risk_pct}% ({_label_risco(p_risk).split('(')[0].strip()})",
        f"  Slope força 7d     : {slope_str} ({tendencia})",
        f"  Força mínima (3d)  : {forca_min_str}  [limite crítico: 800 gf]",
        f"  Projeção 48h       : {proj_str}",
        f"  Ação recomendada   : {acao_recomendada}",
        "",
        "━━━ PERGUNTA ━━━",
        f"Com base na documentação técnica do rolo maintacker {maquina} e nos dados acima:",
        "",
        f"1. O que um p_risk de {p_risk_pct}% aos {idade_dias} dias de ciclo representa",
        f"   nos registros históricos desta máquina? Qual foi o desfecho típico?",
        f"2. Considerando slope de {slope_str} e projeção de {proj_str} em 48h,",
        f"   quanto tempo temos antes de atingir o limite crítico de 800 gf?",
        f"3. Como devemos operar nas próximas 48h para evitar parada não planejada?",
        f"4. Existe algum padrão de degradação documentado que corresponda a este cenário?",
    ]
    return "\n".join(linhas)


def build_alert_card(
    maquina: str,
    gatilho: str,
    idade_dias: int,
    p_risk: float,
    slope_7d: Optional[float],
    forca_min_3d: Optional[float],
    proj_48h: Optional[float],
    acao_recomendada: str,
    data_disparo: Optional[datetime] = None,
    vida_ref_dias: float = VIDA_REF_DIAS,
    n_abaixo_800_ciclo: int = 0,
    forca_min_ciclo: Optional[float] = None,
) -> str:
    """
    Retorna JSON string do Adaptive Card pronto para o campo TeamsPayload.

    Parâmetros vindos da lista Gatilhos_Selagem:
        maquina         → Title / Maquina
        gatilho         → RED | AMARELO | EMERGENCIA | REVISAO
        idade_dias      → IdadeMaintacker
        p_risk          → ScoreAtual
        slope_7d        → SlopeForca7d  (gf/dia — negativo = declinando)
        forca_min_3d    → ForcaMinima3d (N)
        proj_48h        → proj_48h (N)
        acao_recomendada→ AcaoRecomendada
        data_disparo    → DataDisparo
    """
    meta = _SEVERIDADE_META.get(gatilho, _SEVERIDADE_META["RED"])

    consumida = min(idade_dias / vida_ref_dias, 1.0)
    pct_consumida = round(consumida * 100)
    dias_restantes = max(0, round((1.0 - consumida) * vida_ref_dias))
    barra = _barra_vida(consumida)

    data_str = (data_disparo or datetime.now()).strftime("%d/%m/%Y %H:%M")

    forca_proj_str     = f"{round(proj_48h)} gf"         if proj_48h is not None else "—"
    forca_min_str      = f"{round(forca_min_3d)} gf"     if forca_min_3d is not None else "—"
    forca_min_ciclo_str = (
        f"{round(forca_min_ciclo)} gf"
        if forca_min_ciclo is not None and not math.isnan(forca_min_ciclo)
        else "—"
    )
    abaixo_str = f"{n_abaixo_800_ciclo}x" if n_abaixo_800_ciclo > 0 else "Nenhuma"

    if gatilho == "REVISAO":
        indicadores_facts = [
            {"title": "Força Mínima (3 dias)",  "value": forca_min_str},
            {"title": "Força Mínima do Ciclo",  "value": forca_min_ciclo_str},
            {"title": "Força Projetada (48h)",  "value": forca_proj_str},
            {"title": "< 800 gf no ciclo",       "value": abaixo_str},
            {"title": "Tendência de Força",     "value": _label_tendencia(slope_7d)},
        ]
    elif gatilho in ("AMARELO", "RED"):
        indicadores_facts = [
            {"title": "Tendência de Força",     "value": _label_tendencia(slope_7d)},
            {"title": "Força Projetada (48h)",  "value": forca_proj_str},
            {"title": "Força Mínima (3 dias)",  "value": forca_min_str},
            {"title": "Força Mínima do Ciclo",  "value": forca_min_ciclo_str},
            {"title": "< 800 gf no ciclo",       "value": abaixo_str},
        ]
    else:
        indicadores_facts = [
            {"title": "Tendência de Força",     "value": _label_tendencia(slope_7d)},
            {"title": "Força Projetada (48h)",  "value": forca_proj_str},
            {"title": "Força Mínima (3 dias)",  "value": forca_min_str},
        ]

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            # ── Cabeçalho ─────────────────────────────────────────
            {
                "type": "Container",
                "style": meta["style"],
                **({"backgroundColor": meta["bg"]} if meta.get("bg") else {}),
                "bleed": True,
                "items": [
                    {
                        "type": "ColumnSet",
                        "columns": [
                            {
                                "type": "Column",
                                "width": "stretch",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": meta["titulo"],
                                        "weight": "Bolder",
                                        "size": "Large",
                                        "color": "Light",
                                        "wrap": True,
                                    },
                                    {
                                        "type": "TextBlock",
                                        "text": "Força de Selagem — Rolo Maintacker",
                                        "color": "Light",
                                        "isSubtle": True,
                                        "spacing": "None",
                                        "wrap": True,
                                    },
                                ],
                            },
                            {
                                "type": "Column",
                                "width": "auto",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": maquina,
                                        "weight": "Bolder",
                                        "size": "ExtraLarge",
                                        "color": "Light",
                                        "horizontalAlignment": "Right",
                                    },
                                    {
                                        "type": "TextBlock",
                                        "text": data_str,
                                        "color": "Light",
                                        "isSubtle": True,
                                        "horizontalAlignment": "Right",
                                        "spacing": "None",
                                        "size": "Small",
                                    },
                                ],
                            },
                        ],
                    }
                ],
            },

            # ── Barra de vida do maintacker ────────────────────────
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "VIDA DO MAINTACKER",
                        "weight": "Bolder",
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "Small",
                    },
                    {
                        "type": "TextBlock",
                        "text": f"{barra}  **{pct_consumida}% consumida**",
                        "fontType": "Monospace",
                        "spacing": "Small",
                        "wrap": False,
                    },
                    {
                        "type": "ColumnSet",
                        "spacing": "Small",
                        "columns": [
                            {
                                "type": "Column",
                                "width": "stretch",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": f"{idade_dias} dias em operação",
                                        "isSubtle": True,
                                        "size": "Small",
                                    }
                                ],
                            },
                            {
                                "type": "Column",
                                "width": "auto",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": f"~{dias_restantes} dias para troca",
                                        "isSubtle": True,
                                        "size": "Small",
                                        "horizontalAlignment": "Right",
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },

            # ── Indicadores ────────────────────────────────────────
            {
                "type": "Container",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "INDICADORES",
                        "weight": "Bolder",
                        "size": "Small",
                        "isSubtle": True,
                    },
                    {
                        "type": "FactSet",
                        "facts": indicadores_facts,
                    },
                ],
            },

            # ── Ação recomendada ───────────────────────────────────
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "AÇÃO RECOMENDADA",
                        "weight": "Bolder",
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "Small",
                    },
                    {
                        "type": "TextBlock",
                        "text": acao_recomendada,
                        "wrap": True,
                        "weight": "Bolder",
                        "spacing": "Small",
                    },
                ],
            },
        ],
    }

    prompt_text = build_ai_prompt(
        maquina=maquina,
        gatilho=gatilho,
        idade_dias=idade_dias,
        p_risk=p_risk,
        slope_7d=slope_7d,
        forca_min_3d=forca_min_3d,
        proj_48h=proj_48h,
        acao_recomendada=acao_recomendada,
        data_disparo=data_disparo,
    )

    card["actions"] = [
        {
            "type": "Action.ShowCard",
            "title": "💬 Prompt para o Violet",
            "card": {
                "type": "AdaptiveCard",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": "Selecione o texto abaixo, copie e cole na IA interna:",
                        "size": "Small",
                        "isSubtle": True,
                        "wrap": True,
                        "spacing": "Small",
                    },
                    {
                        "type": "TextBlock",
                        "text": prompt_text,
                        "fontType": "Monospace",
                        "size": "Small",
                        "wrap": True,
                        "spacing": "Small",
                    },
                ],
            },
        }
    ]

    return json.dumps(card, ensure_ascii=False, indent=2)


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
        "Ir ao local e verificar os motivos da força de selagem abaixo de 800 gf. "
        "Rolo novo — sem degradação associada."
    ),
) -> str:
    """
    Adaptive Card para gatilho RISCO: leitura isolada abaixo de 800 gf em rolo sem degradação.

    Parâmetros:
        forca_min          → features.min_3d (N)
        data_forca_min     → features.data_forca_min ("YYYY-MM-DD")
        n_abaixo_800_ciclo → ev.evento_no_ciclo (Nº acumulado no ciclo)
        p_risk             → features.p_risk (baixo — sem degradação)
    """
    data_str = (data_disparo or datetime.now()).strftime("%d/%m/%Y %H:%M")

    try:
        data_evento_fmt = datetime.strptime(data_forca_min, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        data_evento_fmt = data_forca_min

    forca_str    = f"{round(forca_min)} gf"  if not math.isnan(forca_min) else "—"
    media_3d_str = f"{round(media_3d)} gf"  if media_3d is not None and not math.isnan(media_3d) else "—"

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            # ── Cabeçalho ─────────────────────────────────────────
            {
                "type": "Container",
                "style": "warning",
                "bleed": True,
                "items": [
                    {
                        "type": "ColumnSet",
                        "columns": [
                            {
                                "type": "Column",
                                "width": "stretch",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": "⚠️ RISCO — Leitura Anômala",
                                        "weight": "Bolder",
                                        "size": "Large",
                                        "color": "Light",
                                        "wrap": True,
                                    },
                                    {
                                        "type": "TextBlock",
                                        "text": "Força de Selagem — Rolo Maintacker",
                                        "color": "Light",
                                        "isSubtle": True,
                                        "spacing": "None",
                                        "wrap": True,
                                    },
                                ],
                            },
                            {
                                "type": "Column",
                                "width": "auto",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": maquina,
                                        "weight": "Bolder",
                                        "size": "ExtraLarge",
                                        "color": "Light",
                                        "horizontalAlignment": "Right",
                                    },
                                    {
                                        "type": "TextBlock",
                                        "text": data_str,
                                        "color": "Light",
                                        "isSubtle": True,
                                        "horizontalAlignment": "Right",
                                        "spacing": "None",
                                        "size": "Small",
                                    },
                                ],
                            },
                        ],
                    }
                ],
            },

            # ── Evento ────────────────────────────────────────────
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "EVENTO",
                        "weight": "Bolder",
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "Small",
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {
                                "title": "Força mínima registrada",
                                "value": forca_str,
                            },
                            {
                                "title": "Força média (3 dias)",
                                "value": media_3d_str,
                            },
                            {
                                "title": "Data do evento",
                                "value": data_evento_fmt,
                            },
                            {
                                "title": "< 800 gf no ciclo",
                                "value": f"{n_abaixo_800_ciclo}x",
                            },
                        ],
                    },
                ],
            },

            # ── Status do rolo ────────────────────────────────────
            {
                "type": "Container",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "STATUS DO ROLO",
                        "weight": "Bolder",
                        "size": "Small",
                        "isSubtle": True,
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {
                                "title": "Dias em operação",
                                "value": f"{idade_dias} dias",
                            },
                        ],
                    },
                ],
            },

            # ── Ação recomendada ───────────────────────────────────
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "AÇÃO RECOMENDADA",
                        "weight": "Bolder",
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "Small",
                    },
                    {
                        "type": "TextBlock",
                        "text": acao_recomendada,
                        "wrap": True,
                        "weight": "Bolder",
                        "spacing": "Small",
                    },
                ],
            },
        ],
    }

    return json.dumps(card, ensure_ascii=False, indent=2)


# ── Teste com dados sintéticos ─────────────────────────────────────────────────

if __name__ == "__main__":
    scenarios = [
        {
            "label": "RED — FB14 (27 dias, declínio acentuado)",
            "args": dict(
                maquina="FB14",
                gatilho="RED",
                idade_dias=27,
                p_risk=0.63,
                slope_7d=-8.2,
                forca_min_3d=812.0,
                proj_48h=870.0,
                acao_recomendada=(
                    "Programar troca preventiva do rolo maintacker esta semana. "
                    "Força abaixo de 800 gf pode causar falhas de selagem com impacto "
                    "direto na qualidade do produto."
                ),
                data_disparo=datetime(2026, 5, 12, 14, 35),
            ),
        },
        {
            "label": "AMARELO — FB14 (18 dias, declínio leve)",
            "args": dict(
                maquina="FB14",
                gatilho="AMARELO",
                idade_dias=18,
                p_risk=0.38,
                slope_7d=-4.1,
                forca_min_3d=940.0,
                proj_48h=920.0,
                acao_recomendada=(
                    "Monitorar força de selagem diariamente. "
                    "Incluir troca do maintacker no próximo plano de manutenção preventiva."
                ),
                data_disparo=datetime(2026, 5, 12, 9, 10),
            ),
        },
        {
            "label": "EMERGENCIA — FB14 (38 dias, força crítica)",
            "args": dict(
                maquina="FB14",
                gatilho="EMERGENCIA",
                idade_dias=38,
                p_risk=0.88,
                slope_7d=-14.5,
                forca_min_3d=791.0,
                proj_48h=765.0,
                acao_recomendada=(
                    "PARAR máquina para troca imediata. "
                    "Força mínima abaixo do limite operacional de 800 gf. "
                    "Risco alto de defeito de selagem na embalagem."
                ),
                data_disparo=datetime(2026, 5, 12, 6, 0),
            ),
        },
    ]

    for s in scenarios:
        print(f"\n{'='*60}")
        print(f"  {s['label']}")
        print('='*60)
        print(build_alert_card(**s["args"]))
