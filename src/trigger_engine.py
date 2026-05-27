"""
trigger_engine.py v4.0 — Motor de Gatilho Probabilístico Simplificado
Referência: SDD_Manutencao_Preditiva_v1.docx

Mudanças v3.0 → v4.0
  - Hierarquia simplificada para dois cartões apresentáveis:
      RISCO    — leitura anômala isolada (g/f < 800, sem degradação/envelhecimento)
      CRITICO  — degradação + envelhecimento, com severidade graduada internamente:
                   AVISO       : aviso precoce (ex-AMARELO)
                   CONFIRMADO  : degradação confirmada (ex-RED + ex-CRITICO)
                   FIM_DE_VIDA : age >= eta_ajustado - 5 dias (absorve REVISAO/EMERGENCIAL)

  - Removidos: EmergencialTrigger, RedTrigger, AmarelhoTrigger, RevisaoTrigger
  - TriggerEvent ganha campo sub_nivel (None para RISCO, AVISO/CONFIRMADO/FIM_DE_VIDA para CRITICO)
  - Migração automática de estado v3.0 → v4.0 em _load_state()
  - API pública inalterada: evaluate(), close_all_by_troca(), snooze(), compute_p_risk_snapshot()
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

WEIBULL_BETA    = 1.181
WEIBULL_ETA_H   = 1297.0
WEIBULL_ETA_D   = WEIBULL_ETA_H / 24.0

BOOST_SINAL           = 0.65
LIMIAR_P_RISK         = 0.48
LIMIAR_SIGNAL_SCORE   = 0.22
IDADE_MINIMA_DIAS     = 15
PROJ_48H_LIMIAR       = 800.0
SUSTENTACAO_PROJ_DIAS = 2
COOLDOWN_H            = 48
SNOOZE_DIAS           = 5

# AVISO (ex-AMARELO)
AVISO_P_RISK     = 0.35
AVISO_SIGNAL     = 0.15
AVISO_COOLDOWN_H = 72

# RISCO
RISCO_FORCA_LIMIAR = 800.0
RISCO_MEDIANA_OK   = 950.0
RISCO_N_MAX        = 1
RISCO_COOLDOWN_H   = 48

# CONFIRMADO (força crítica, ex-CRITICO)
CRITICO_FORCA_MIN  = 800.0
CRITICO_P_RISK_MIN = 0.40
CRITICO_COOLDOWN_H = 48

# FIM_DE_VIDA — dispara quando restar este número de dias até o ETA ajustado
FIM_DE_VIDA_DIAS_ANTES = 5
FIM_DE_VIDA_COOLDOWN_H = 48

JANELA_SLOPE_D  = 7
JANELA_MEAN_3D  = 3
JANELA_MEAN_7D  = 7
JANELA_MEAN_14D = 14

DEFAULT_LIST_NAME = "Gatilhos_Selagem"

SP_LIST_SCHEMA = {
    "Title":        "Linha única de texto — chave: Maquina|Gatilho|Data",
    "Maquina":      "Linha única de texto",
    "TeamsPayload": "Várias linhas de texto — JSON estruturado do card",
}


# ─────────────────────────────────────────────────────────────────────────────
# Payload de um disparo
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TriggerEvent:
    maquina: str
    gatilho: str
    severidade: str
    mensagem: str
    idade_maintacker: int
    data_disparo: str
    acao_recomendada: str
    sub_nivel: Optional[str] = None       # AVISO | CONFIRMADO | FIM_DE_VIDA (só CRITICO)
    evento_no_ciclo: int = 0
    score_atual: Optional[float] = None
    slope_forca_7d: Optional[float] = None
    forca_minima_3d: Optional[float] = None
    proj_48h: Optional[float] = None
    signal_score: Optional[float] = None
    age_risk: Optional[float] = None
    status: str = "ATIVO"
    snooze_fim: Optional[str] = None
    sp_item_id: Optional[int] = None
    teams_payload: Optional[str] = None

    def to_sp_dict(self) -> dict:
        d = {
            "Title":   f"{self.maquina} | {self.gatilho} | {self.data_disparo[:10]}",
            "Maquina": self.maquina,
        }
        if self.teams_payload:
            d["TeamsPayload"] = self.teams_payload
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot de métricas
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TriggerFeatures:
    today: pd.Timestamp
    age_days: float
    mean_3d: float
    mean_14d: float
    mean_7d: float
    mean_7d_3d_ago: float
    min_3d: float
    mediana_3d: float
    n_leituras_abaixo_800: int
    slope_7d: float
    slope_min_ciclo: float
    proj_48h: float
    sig_score: float
    age_risk: float
    p_risk: float
    eventos_risco_ciclo: int
    eta_ajustado_dias: float
    data_forca_min: str
    forca_min_ciclo: float


# ─────────────────────────────────────────────────────────────────────────────
# Estado interno
# ─────────────────────────────────────────────────────────────────────────────
def _default_state(maquina: str) -> dict:
    return {
        "maquina": maquina,
        "eventos_risco_ciclo": 0,
        "risco":  {"sp_id": None, "last_fired": None},
        "critico": {
            "sp_id":                  None,
            "last_fired_aviso":       None,
            "last_fired_confirmado":  None,
            "last_fired_fim_de_vida": None,
            "snooze_until":           None,
            "proj_window":            [],
        },
    }


def _merge_defaults(state: dict, defaults: dict) -> dict:
    for k, v in defaults.items():
        if k not in state:
            state[k] = v
        elif isinstance(v, dict) and isinstance(state[k], dict):
            _merge_defaults(state[k], v)
    return state


def _load_state(path: Path, maquina: str) -> dict:
    defaults = _default_state(maquina)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Migração v1 → v4.0
        if "red" not in data and "critico" not in data:
            logger.info("Estado v1 em '%s' — resetando para v4.0.", path)
            return defaults
        # Migração v2.3 → v3.0
        if "outlier_sinal" in data and "risco" not in data:
            data["risco"] = data.pop("outlier_sinal")
        if "emergencia" in data and "critico" not in data:
            data["critico"] = data.pop("emergencia")
        # Migração v3.0 → v4.0: colapsa amarelo/red/emergencial/revisao em critico
        old_keys = {"amarelo", "red", "emergencial", "revisao"}
        if old_keys & set(data.keys()):
            logger.info("Migrando estado v3.0 -> v4.0: consolidando em critico.")
            for k in old_keys:
                data.pop(k, None)
            data["critico"] = defaults["critico"]
        # Migração critico v3.0 (sem last_fired_aviso) → v4.0
        if "critico" in data and "last_fired_aviso" not in data.get("critico", {}):
            data["critico"] = defaults["critico"]
        _merge_defaults(data, defaults)
        return data
    return defaults


def _save_state(state: dict, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Configuração YAML
# ─────────────────────────────────────────────────────────────────────────────
def _load_trigger_config(state_path: Path) -> dict:
    candidates = [
        state_path.parent / "config.yaml",
        state_path.parent.parent / "config.yaml",
        Path("config.yaml"),
    ]
    for p in candidates:
        if p.exists():
            try:
                import yaml
                with open(p, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                trig = cfg.get("trigger", {}) if isinstance(cfg, dict) else {}
                if trig:
                    logger.debug("Config trigger carregada de '%s'.", p)
                return trig
            except Exception as exc:
                logger.warning("Erro ao carregar config.yaml ('%s'): %s", p, exc)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de feature
# ─────────────────────────────────────────────────────────────────────────────
def _to_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _align_tz(ts: pd.Timestamp, index: pd.DatetimeIndex) -> pd.Timestamp:
    if index.tz is None:
        return ts.tz_localize(None) if ts.tzinfo is not None else ts
    return ts.tz_localize(index.tz) if ts.tzinfo is None else ts.tz_convert(index.tz)


_DAY_END = pd.Timedelta(hours=23, minutes=59)


def _rolling_mean(df: pd.DataFrame, col: str, ref: pd.Timestamp, days: int) -> float:
    t = _align_tz(ref, df.index)
    sub = df.loc[t - pd.Timedelta(days=days) : t + _DAY_END, col].dropna()
    return float(sub.mean()) if len(sub) > 0 else float("nan")


def _rolling_min(df: pd.DataFrame, col: str, today: pd.Timestamp, days: int) -> float:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days) : t + _DAY_END, col].dropna()
    return float(sub.min()) if len(sub) > 0 else float("nan")


def _rolling_median(df: pd.DataFrame, col: str, today: pd.Timestamp, days: int) -> float:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days) : t + _DAY_END, col].dropna()
    return float(sub.median()) if len(sub) > 0 else float("nan")


def _count_below(df: pd.DataFrame, col: str, today: pd.Timestamp,
                 days: int, threshold: float) -> int:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days) : t + _DAY_END, col].dropna()
    return int((sub < threshold).sum())


def _date_of_min(df: pd.DataFrame, col: str, today: pd.Timestamp, days: int) -> str:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days) : t + _DAY_END, col].dropna()
    if len(sub) == 0:
        return today.date().isoformat()
    return sub.idxmin().date().isoformat()


def _slope_7d(df: pd.DataFrame, col: str, today: pd.Timestamp) -> float:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=JANELA_SLOPE_D) : t + _DAY_END, col].dropna()
    if len(sub) < 2:
        return 0.0
    x = (sub.index - sub.index[0]).total_seconds().values / 86400.0
    y = sub.values.astype(float)
    xm, ym = x.mean(), y.mean()
    denom = ((x - xm) ** 2).sum()
    return float(((x - xm) * (y - ym)).sum() / denom) if denom != 0 else 0.0


def _weibull_age_risk(age_days: float, beta: float = WEIBULL_BETA,
                      eta_d: float = WEIBULL_ETA_D) -> float:
    if age_days <= 0:
        return 0.0
    return float(1.0 - np.exp(-((age_days / eta_d) ** beta)))


def _min_slope_ciclo(df: pd.DataFrame, col: str,
                     cycle_start: pd.Timestamp, today: pd.Timestamp) -> float:
    """Pior slope (mais negativo) de 7 dias observado desde cycle_start."""
    first_valid = cycle_start + pd.Timedelta(days=JANELA_SLOPE_D)
    if first_valid > today:
        return _slope_7d(df, col, today)
    slopes = []
    t = first_valid
    while t <= today:
        slopes.append(_slope_7d(df, col, t))
        t += pd.Timedelta(days=1)
    return min(slopes)


def _compute_signal_score(mean_3d: float, mean_14d: float, slope_for_danger: float,
                           boost: float = BOOST_SINAL) -> float:
    if np.isnan(mean_3d) or np.isnan(mean_14d) or mean_14d == 0:
        return 0.0
    deg_signal   = max(0.0, 1.0 - mean_3d / mean_14d)
    slope_danger = float(np.clip(-slope_for_danger / 50.0, 0.0, 1.0))
    return float(deg_signal * 0.6 + slope_danger * 0.4)


def _compute_proj_48h(mean_3d: float, slope_7d: float) -> float:
    if np.isnan(mean_3d):
        return float("nan")
    return float(mean_3d + slope_7d * 2.0)


def _in_cooldown(last_fired_iso: Optional[str], cooldown_h: float,
                 today: pd.Timestamp) -> bool:
    if not last_fired_iso:
        return False
    return (today - _to_utc(last_fired_iso)).total_seconds() / 3600 < cooldown_h


# ─────────────────────────────────────────────────────────────────────────────
# Card JSON estruturado — contrato v4.0 com Power Automate
# ─────────────────────────────────────────────────────────────────────────────
def _build_card_json(ev: TriggerEvent, features: TriggerFeatures) -> str:
    dias_restantes = int(round(features.eta_ajustado_dias - features.age_days))
    base: dict = {
        "nivel":             ev.gatilho,
        "sub_nivel":         ev.sub_nivel,
        "evento_no_ciclo":   ev.evento_no_ciclo,
        "dias_operacao":     int(features.age_days),
        "eta_ajustado_dias": int(round(features.eta_ajustado_dias)),
        "dias_restantes":    dias_restantes,
        "acao":              ev.acao_recomendada,
    }
    if ev.gatilho == "RISCO":
        base["data_evento"] = ev.data_disparo
        if not np.isnan(features.min_3d):
            base["forca_gf"] = int(round(features.min_3d))

    elif ev.gatilho == "CRITICO":
        base["data_evento"] = ev.data_disparo
        if not np.isnan(features.min_3d):
            base["forca_min_ultima_amostra_gf"] = int(round(features.min_3d))
        base["data_forca_min"] = features.data_forca_min
        if not np.isnan(features.proj_48h):
            base["forca_projetada_gf"] = int(round(features.proj_48h))
        if not np.isnan(features.mean_7d_3d_ago):
            base["media_movel_7d_anterior_gf"] = int(round(features.mean_7d_3d_ago))
        if not np.isnan(features.mean_7d):
            base["media_movel_7d_atual_gf"] = int(round(features.mean_7d))
        base["p_risk"] = round(features.p_risk, 2)
        base["eventos_risco_ciclo"] = ev.evento_no_ciclo

    return json.dumps(base, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# TriggerBase
# ─────────────────────────────────────────────────────────────────────────────
class TriggerBase(ABC):
    name:      str
    severity:  str
    state_key: str
    sp_id_key: Optional[str] = "sp_id"

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg

    @abstractmethod
    def check(self, features: TriggerFeatures, state: dict) -> bool: ...

    @abstractmethod
    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent: ...

    def update_state(self, state: dict, ts: str) -> None:
        state[self.state_key]["last_fired"] = ts

    def default_state(self) -> dict:
        return {"sp_id": None, "last_fired": None}


# ─────────────────────────────────────────────────────────────────────────────
# Gatilhos concretos
# ─────────────────────────────────────────────────────────────────────────────

class RiscoTrigger(TriggerBase):
    """Leitura anômala isolada — g/f < 800 N, isolada, sinal geral saudável."""
    name      = "RISCO"
    severity  = "INFO"
    state_key = "risco"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.forca_limiar = float(cfg.get("risco_forca_limiar", RISCO_FORCA_LIMIAR))
        self.mediana_ok   = float(cfg.get("risco_mediana_ok",   RISCO_MEDIANA_OK))
        self.n_max        = int(cfg.get("risco_n_max",          RISCO_N_MAX))
        self.cooldown_h   = float(cfg.get("risco_cooldown_h",   RISCO_COOLDOWN_H))

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        if np.isnan(features.min_3d) or features.min_3d >= self.forca_limiar:
            return False
        if features.n_leituras_abaixo_800 > self.n_max:
            return False
        if np.isnan(features.mediana_3d) or features.mediana_3d <= self.mediana_ok:
            return False
        if _in_cooldown(state[self.state_key].get("last_fired"), self.cooldown_h, features.today):
            return False
        return True

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        evento_n = state.get("eventos_risco_ciclo", 0) + 1
        min_str  = f"{features.min_3d:.0f}" if not np.isnan(features.min_3d) else "N/D"
        acao = (
            f"Ir verificar no local — analisar ultima amostra ({min_str} N) e identificar causa. "
            f"Se 2+ leituras abaixo de {self.forca_limiar:.0f} N nas proximas 72h, "
            "o sistema escalara para CRITICO."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = (
                f"RISCO FB14 — Leitura Anomala Isolada | "
                f"Evento No{evento_n} no ciclo atual | "
                f"Dia {int(features.age_days)} | {min_str} N"
            ),
            idade_maintacker = int(features.age_days),
            data_disparo     = features.today.isoformat(),
            acao_recomendada = acao,
            evento_no_ciclo  = evento_n,
            forca_minima_3d  = round(features.min_3d, 1) if not np.isnan(features.min_3d) else None,
        )

    def update_state(self, state: dict, ts: str) -> None:
        state[self.state_key]["last_fired"] = ts
        state["eventos_risco_ciclo"] = state.get("eventos_risco_ciclo", 0) + 1


class CriticoTrigger(TriggerBase):
    """
    Degradação + envelhecimento com severidade graduada (v4.0).

    Sub-níveis avaliados em ordem decrescente de urgência:
      FIM_DE_VIDA : age >= eta_ajustado - fim_de_vida_dias_antes
      CONFIRMADO  : condições RED (C1 AND C2 AND C3 AND C4)
                    OU força critica confirmada (min < 800 + p_risk >= limiar)
      AVISO       : aviso precoce — p_risk >= limiar_aviso OU sig >= limiar_sig
    """
    name      = "CRITICO"
    state_key = "critico"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        # AVISO
        self.aviso_p_risk   = float(cfg.get("aviso_p_risk",   cfg.get("amarelo_p_risk",     AVISO_P_RISK)))
        self.aviso_signal   = float(cfg.get("aviso_signal",   cfg.get("amarelo_signal",     AVISO_SIGNAL)))
        self.aviso_cooldown = float(cfg.get("aviso_cooldown_h", cfg.get("amarelo_cooldown_h", AVISO_COOLDOWN_H)))
        # CONFIRMADO — RED
        self.limiar_p_risk       = float(cfg.get("limiar_p_risk",       LIMIAR_P_RISK))
        self.limiar_signal_score = float(cfg.get("limiar_signal_score", LIMIAR_SIGNAL_SCORE))
        self.idade_minima_dias   = float(cfg.get("idade_minima_dias",   IDADE_MINIMA_DIAS))
        self.proj_48h_limiar     = float(cfg.get("proj_48h_limiar",     PROJ_48H_LIMIAR))
        self.sustentacao_proj    = int(cfg.get("sustentacao_proj_dias", SUSTENTACAO_PROJ_DIAS))
        self.confirmado_cooldown = float(cfg.get("cooldown_h",          COOLDOWN_H))
        # CONFIRMADO — força crítica
        self.forca_critica_min   = float(cfg.get("critico_forca_min",  CRITICO_FORCA_MIN))
        self.forca_critica_p_risk= float(cfg.get("critico_p_risk_min", CRITICO_P_RISK_MIN))
        self._mediana_ok         = float(cfg.get("risco_mediana_ok",   RISCO_MEDIANA_OK))
        self._risco_n_max        = int(cfg.get("risco_n_max",          RISCO_N_MAX))
        self.snooze_dias         = int(cfg.get("snooze_dias",          SNOOZE_DIAS))
        # FIM_DE_VIDA
        self.fim_de_vida_antes   = float(cfg.get("fim_de_vida_dias_antes", FIM_DE_VIDA_DIAS_ANTES))
        self.fim_de_vida_cooldown= float(cfg.get("fim_de_vida_cooldown_h", FIM_DE_VIDA_COOLDOWN_H))

        self._sub_nivel: Optional[str] = None

    def default_state(self) -> dict:
        return {
            "sp_id":                  None,
            "last_fired_aviso":       None,
            "last_fired_confirmado":  None,
            "last_fired_fim_de_vida": None,
            "snooze_until":           None,
            "proj_window":            [],
        }

    def _is_outlier(self, features: TriggerFeatures) -> bool:
        return (
            features.n_leituras_abaixo_800 <= self._risco_n_max
            and not np.isnan(features.mediana_3d)
            and features.mediana_3d > self._mediana_ok
        )

    def _get_sub_nivel(self, features: TriggerFeatures, state: dict) -> Optional[str]:
        e = state[self.state_key]

        # FIM_DE_VIDA: prioridade máxima, ignora snooze
        if features.age_days >= features.eta_ajustado_dias - self.fim_de_vida_antes:
            if not _in_cooldown(e.get("last_fired_fim_de_vida"), self.fim_de_vida_cooldown, features.today):
                return "FIM_DE_VIDA"

        # Snooze bloqueia CONFIRMADO e AVISO
        if e.get("snooze_until"):
            if features.today <= _to_utc(e["snooze_until"]).normalize():
                return None

        # CONFIRMADO
        if not _in_cooldown(e.get("last_fired_confirmado"), self.confirmado_cooldown, features.today):
            # Caminho força crítica
            if (not np.isnan(features.min_3d)
                    and features.min_3d < self.forca_critica_min
                    and features.p_risk >= self.forca_critica_p_risk
                    and features.age_days >= 5
                    and not self._is_outlier(features)):
                return "CONFIRMADO"

            # Caminho RED (C1 AND C2 AND C3 AND C4)
            today_str = features.today.isoformat()[:10]
            proj_val  = round(float(features.proj_48h), 1) if not np.isnan(features.proj_48h) else 9999.0
            window = e.get("proj_window", [])
            # Sobrescreve entrada do dia (idempotente em múltiplos runs diários)
            if window and window[-1]["date"] == today_str:
                window[-1]["proj"] = proj_val
            else:
                window.append({"date": today_str, "proj": proj_val})
            e["proj_window"] = window[-5:]
            n_below = sum(1 for d in e["proj_window"] if d["proj"] < self.proj_48h_limiar)

            cond1 = features.p_risk    >= self.limiar_p_risk
            cond2 = features.sig_score >= self.limiar_signal_score
            cond3 = features.age_days  >= self.idade_minima_dias
            cond4 = n_below            >= self.sustentacao_proj
            logger.debug(
                "[%s] CONFIRMADO | p_risk=%.3f(C1=%s) sig=%.3f(C2=%s) age=%.0fd(C3=%s) proj_below=%d/5(C4=%s)",
                features.today.date(), features.p_risk, cond1, features.sig_score, cond2,
                features.age_days, cond3, n_below, cond4,
            )
            if cond1 and cond2 and cond3 and cond4:
                return "CONFIRMADO"

        # AVISO
        if features.age_days >= self.idade_minima_dias:
            if not _in_cooldown(e.get("last_fired_confirmado"), self.confirmado_cooldown, features.today):
                if not _in_cooldown(e.get("last_fired_aviso"), self.aviso_cooldown, features.today):
                    cond_a = features.p_risk    >= self.aviso_p_risk
                    cond_b = features.sig_score >= self.aviso_signal
                    already_confirmado = (
                        features.p_risk    >= self.limiar_p_risk
                        and features.sig_score >= self.limiar_signal_score
                    )
                    if (cond_a or cond_b) and not already_confirmado:
                        return "AVISO"

        return None

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        self._sub_nivel = self._get_sub_nivel(features, state)
        return self._sub_nivel is not None

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        sub      = self._sub_nivel
        evento_n = state.get("eventos_risco_ciclo", 0) + 1
        min_str  = f"{features.min_3d:.0f}" if not np.isnan(features.min_3d) else "N/D"

        if sub == "FIM_DE_VIDA":
            severidade   = "CRITICA"
            dias_rest    = int(round(features.eta_ajustado_dias - features.age_days))
            dias_str     = (f"{abs(dias_rest)} dias alem do ETA"
                            if dias_rest < 0 else f"faltam {dias_rest} dias para o ETA")
            mensagem     = (
                f"CRITICO FB14 — Fim de Vida Projetado | "
                f"Dia {int(features.age_days)} / ETA {int(features.eta_ajustado_dias)}d | {dias_str}"
            )
            acao = "Troca imediata do rolo maintacker — vida util projetada atingida."

        elif sub == "CONFIRMADO":
            severidade = "ALTA"
            mensagem   = (
                f"CRITICO FB14 — Degradacao + Risco Elevado | "
                f"Evento No{evento_n} no ciclo | "
                f"Dia {int(features.age_days)} | {min_str} N | p_risk={features.p_risk:.2f}"
            )
            acao = "Analise aprofundada e planejamento de troca do rolo maintacker."

        else:  # AVISO
            severidade = "MEDIA"
            mean_str   = f"{features.mean_3d:.0f}" if not np.isnan(features.mean_3d) else "N/D"
            razao      = ("risco em elevacao" if features.p_risk >= self.aviso_p_risk
                          else "degradacao de sinal detectada")
            mensagem   = (
                f"CRITICO FB14 — Aviso Precoce | "
                f"Dia {int(features.age_days)} | Forca media 72h: {mean_str} N | {razao}"
            )
            acao = (
                "Aumentar frequencia de monitoramento do sinal de forca. "
                "Registrar observacoes no proximo turno."
            )

        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            sub_nivel        = sub,
            severidade       = severidade,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = features.today.isoformat(),
            acao_recomendada = acao,
            evento_no_ciclo  = evento_n if sub != "FIM_DE_VIDA" else 0,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.min_3d, 1) if not np.isnan(features.min_3d) else None,
            proj_48h         = round(features.proj_48h, 1) if not np.isnan(features.proj_48h) else None,
            signal_score     = round(features.sig_score, 4),
            age_risk         = round(features.age_risk, 4),
        )

    def update_state(self, state: dict, ts: str) -> None:
        sub = self._sub_nivel
        e   = state[self.state_key]
        if sub == "FIM_DE_VIDA":
            e["last_fired_fim_de_vida"] = ts
        elif sub == "CONFIRMADO":
            e["last_fired_confirmado"] = ts
            state["eventos_risco_ciclo"] = state.get("eventos_risco_ciclo", 0) + 1
        else:  # AVISO
            e["last_fired_aviso"] = ts
            state["eventos_risco_ciclo"] = state.get("eventos_risco_ciclo", 0) + 1


# ─────────────────────────────────────────────────────────────────────────────
# Motor principal
# ─────────────────────────────────────────────────────────────────────────────
class TriggerEngine:
    """
    Motor de gatilho probabilístico — v4.0

    Ordem de avaliação: RISCO → CRITICO (FIM_DE_VIDA > CONFIRMADO > AVISO)
    """

    def __init__(self, maquina: str, state_path: Path) -> None:
        self.maquina    = maquina
        self.state_path = Path(state_path)
        self.state      = _load_state(self.state_path, maquina)
        self.state["maquina"] = maquina

        cfg = _load_trigger_config(self.state_path)

        self.weibull_beta  = float(cfg.get("weibull_beta",  WEIBULL_BETA))
        self.weibull_eta_h = float(cfg.get("weibull_eta_h", WEIBULL_ETA_H))
        self.weibull_eta_d = self.weibull_eta_h / 24.0
        self.boost_sinal   = float(cfg.get("boost_sinal",   BOOST_SINAL))
        self.snooze_dias   = int(cfg.get("snooze_dias",     SNOOZE_DIAS))
        self.vida_decay_w  = float(cfg.get("vida_decay_w",  0.8))

        self.triggers: List[TriggerBase] = [
            RiscoTrigger(cfg),
            CriticoTrigger(cfg),
        ]

    def evaluate(
        self,
        df_hourly: pd.DataFrame,
        troca_date: datetime,
        sp_client=None,
        list_name: str = DEFAULT_LIST_NAME,
        today: Optional[pd.Timestamp] = None,
        media_col: str = "Media",
        troca_dates: Optional[list] = None,
    ) -> List[TriggerEvent]:
        """Avalia todos os gatilhos para `today`."""
        if today is None:
            today = _to_utc(df_hourly.index[-1]).normalize()
        else:
            today = _to_utc(today).normalize()
        troca_date = _to_utc(troca_date).normalize()
        age_days   = float((today - troca_date).days)

        eta_ajustado_dias = (
            self._compute_vida_ref_ajustada(troca_dates)
            if troca_dates is not None else self.weibull_eta_d
        )

        mean_3d        = _rolling_mean(df_hourly, media_col, today, JANELA_MEAN_3D)
        mean_14d       = _rolling_mean(df_hourly, media_col, today, JANELA_MEAN_14D)
        mean_7d        = _rolling_mean(df_hourly, media_col, today, JANELA_MEAN_7D)
        mean_7d_3d_ago = _rolling_mean(
            df_hourly, media_col, today - pd.Timedelta(days=3), JANELA_MEAN_7D
        )
        min_3d         = _rolling_min(df_hourly,    "Media", today, JANELA_MEAN_3D)
        mediana_3d     = _rolling_median(df_hourly, "Media", today, JANELA_MEAN_3D)
        n_abaixo       = _count_below(df_hourly, "Media", today, JANELA_MEAN_3D, RISCO_FORCA_LIMIAR)
        data_forca_min = _date_of_min(df_hourly, "Media", today, JANELA_MEAN_3D)
        _ciclo_media   = df_hourly.loc[df_hourly.index >= troca_date, "Media"].dropna()
        forca_min_ciclo = float(_ciclo_media.min()) if len(_ciclo_media) > 0 else float("nan")
        slope          = _slope_7d(df_hourly, media_col, today)
        slope_min      = _min_slope_ciclo(df_hourly, media_col, troca_date, today)
        proj_48h       = _compute_proj_48h(mean_3d, slope)
        sig_score      = _compute_signal_score(mean_3d, mean_14d, slope_min)
        age_risk       = _weibull_age_risk(age_days, self.weibull_beta, self.weibull_eta_d)
        p_risk         = age_risk + (1.0 - age_risk) * sig_score * self.boost_sinal

        features = TriggerFeatures(
            today                 = today,
            age_days              = age_days,
            mean_3d               = mean_3d,
            mean_14d              = mean_14d,
            mean_7d               = mean_7d,
            mean_7d_3d_ago        = mean_7d_3d_ago,
            min_3d                = min_3d,
            mediana_3d            = mediana_3d,
            n_leituras_abaixo_800 = n_abaixo,
            slope_7d              = slope,
            slope_min_ciclo       = slope_min,
            proj_48h              = proj_48h,
            sig_score             = sig_score,
            age_risk              = age_risk,
            p_risk                = p_risk,
            eventos_risco_ciclo   = self.state.get("eventos_risco_ciclo", 0),
            eta_ajustado_dias     = eta_ajustado_dias,
            data_forca_min        = data_forca_min,
            forca_min_ciclo       = forca_min_ciclo,
        )

        fired: List[TriggerEvent] = []
        for trigger in self.triggers:
            if trigger.check(features, self.state):
                ev = trigger.build_event(self.maquina, features, self.state)
                trigger.update_state(self.state, ev.data_disparo)
                self._persist(ev, trigger, features, sp_client, list_name, eta_ajustado_dias)
                fired.append(ev)

        _save_state(self.state, self.state_path)

        if fired:
            logger.info(
                "[%s] Disparos: %s | p_risk=%.3f sig=%.3f proj=%.0f min3d=%.0f age=%dd n<800=%d eventos=%d",
                today.date(),
                "+".join(f"{ev.gatilho}({ev.sub_nivel})" if ev.sub_nivel else ev.gatilho
                         for ev in fired),
                p_risk, sig_score,
                proj_48h if not np.isnan(proj_48h) else -1,
                min_3d   if not np.isnan(min_3d)   else -1,
                int(age_days), n_abaixo, self.state.get("eventos_risco_ciclo", 0),
            )
        return fired

    def close_all_by_troca(
        self,
        sp_client=None,
        list_name: str = DEFAULT_LIST_NAME,
    ) -> None:
        for trigger in self.triggers:
            if trigger.sp_id_key is None:
                continue
            sp_id = self.state.get(trigger.state_key, {}).get(trigger.sp_id_key)
            if sp_client and sp_id and sp_id > 0:
                sp_client.update_list_items(list_name, [sp_id], "Status", "FECHADO")
                logger.info("%s (ID=%s) fechado por troca confirmada.", trigger.name, sp_id)

        maquina = self.state["maquina"]
        self.state = _default_state(maquina)
        _save_state(self.state, self.state_path)
        logger.info("Estado reiniciado por troca (eventos_risco_ciclo=0).")

    def snooze(
        self,
        sp_id: int,
        sp_client=None,
        list_name: str = DEFAULT_LIST_NAME,
        dias: Optional[int] = None,
    ) -> None:
        if dias is None:
            dias = self.snooze_dias
        snooze_fim = (datetime.now() + timedelta(days=dias)).date().isoformat()
        self.state["critico"]["snooze_until"] = snooze_fim
        if sp_client and sp_id and sp_id > 0:
            sp_client.update_list_items(list_name, [sp_id], "Status",    "SNOOZE")
            sp_client.update_list_items(list_name, [sp_id], "SnoozeFim", snooze_fim)
        _save_state(self.state, self.state_path)
        logger.info("CRITICO (ID=%s) em snooze ate %s.", sp_id, snooze_fim)

    def _compute_vida_ref_ajustada(self, troca_dates: list) -> float:
        """V = Eta - (d × w) onde d = Eta - mean_vida_ano_vigente."""
        if not troca_dates or len(troca_dates) < 2:
            return self.weibull_eta_d
        ano = datetime.now().year
        sorted_dates = sorted(troca_dates)
        duracoes = []
        for i in range(1, len(sorted_dates)):
            fim = sorted_dates[i]
            if _to_utc(fim).year == ano:
                dur = (_to_utc(fim) - _to_utc(sorted_dates[i - 1])).total_seconds() / 86400.0
                if dur > 0:
                    duracoes.append(dur)
        if not duracoes:
            return self.weibull_eta_d
        mean_ano = sum(duracoes) / len(duracoes)
        return max(1.0, self.weibull_eta_d - (self.weibull_eta_d - mean_ano) * self.vida_decay_w)

    def _persist(self, ev: TriggerEvent, trigger: TriggerBase,
                 features: TriggerFeatures, sp_client, list_name: str,
                 eta_ajustado_dias: float = 0.0) -> None:
        try:
            if ev.gatilho == "RISCO":
                from .card_formatter import build_risco_card
                ev.teams_payload = build_risco_card(
                    maquina            = ev.maquina,
                    idade_dias         = ev.idade_maintacker,
                    forca_min          = features.min_3d,
                    data_forca_min     = features.data_forca_min,
                    n_abaixo_800_ciclo = ev.evento_no_ciclo,
                    p_risk             = features.p_risk,
                    data_disparo       = datetime.fromisoformat(ev.data_disparo[:19]),
                    media_3d           = features.mean_3d,
                )
            elif ev.gatilho == "CRITICO":
                from .card_formatter import build_critico_card
                ev.teams_payload = build_critico_card(
                    maquina             = ev.maquina,
                    sub_nivel           = ev.sub_nivel,
                    idade_dias          = ev.idade_maintacker,
                    p_risk              = features.p_risk,
                    slope_7d            = ev.slope_forca_7d,
                    forca_min_3d        = features.min_3d if not np.isnan(features.min_3d) else None,
                    proj_48h            = features.proj_48h if not np.isnan(features.proj_48h) else None,
                    media_7d            = features.mean_7d if not np.isnan(features.mean_7d) else None,
                    media_7d_anterior   = features.mean_7d_3d_ago if not np.isnan(features.mean_7d_3d_ago) else None,
                    acao_recomendada    = ev.acao_recomendada,
                    data_disparo        = datetime.fromisoformat(ev.data_disparo[:19]),
                    vida_ref_dias       = eta_ajustado_dias or self.weibull_eta_d,
                    eventos_risco_ciclo = ev.evento_no_ciclo,
                    forca_min_ciclo     = features.forca_min_ciclo if not np.isnan(features.forca_min_ciclo) else None,
                )
        except Exception as exc:
            logger.warning("card build falhou (%s) — TeamsPayload omitido.", exc)

        if sp_client is None:
            logger.warning("[dry-run] %s(%s) nao persistido (sp_client=None).",
                           ev.gatilho, ev.sub_nivel)
            return

        _SP_CAMPOS_ATIVOS = {"Title", "Maquina", "TeamsPayload"}
        sp_payload = {k: v for k, v in ev.to_sp_dict().items()
                      if k in _SP_CAMPOS_ATIVOS and v is not None}
        ids = sp_client.insert_list_item(list_name, [sp_payload])
        if ids and ids[0] is not None:
            ev.sp_item_id = ids[0]
            if trigger.sp_id_key is not None:
                self.state[trigger.state_key][trigger.sp_id_key] = ids[0]
            logger.info("%s(%s) inserido na lista '%s' (ID=%s).",
                        ev.gatilho, ev.sub_nivel, list_name, ids[0])
        else:
            logger.warning("%s(%s) inserido mas ID nao retornado.", ev.gatilho, ev.sub_nivel)


# ─────────────────────────────────────────────────────────────────────────────
# Utilitário exportado
# ─────────────────────────────────────────────────────────────────────────────
def compute_p_risk_snapshot(
    df: pd.DataFrame,
    troca_date: datetime,
    today: pd.Timestamp,
) -> dict:
    """Snapshot completo de indicadores para `today`. Útil para diagnóstico."""
    today      = _to_utc(today).normalize()
    troca_date = _to_utc(troca_date).normalize()
    age_days   = float((today - troca_date).days)
    mean_3d    = _rolling_mean(df, "Media", today, JANELA_MEAN_3D)
    mean_7d    = _rolling_mean(df, "Media", today, JANELA_MEAN_7D)
    mean_14d   = _rolling_mean(df, "Media", today, JANELA_MEAN_14D)
    min_3d     = _rolling_min(df,  "Media", today, JANELA_MEAN_3D)
    mediana_3d = _rolling_median(df, "Media", today, JANELA_MEAN_3D)
    n_abaixo   = _count_below(df, "Media", today, JANELA_MEAN_3D, RISCO_FORCA_LIMIAR)
    slope      = _slope_7d(df, "Media", today)
    slope_min  = _min_slope_ciclo(df, "Media", troca_date, today)
    proj_48h   = _compute_proj_48h(mean_3d, slope)
    sig_score  = _compute_signal_score(mean_3d, mean_14d, slope_min)
    age_risk   = _weibull_age_risk(age_days)
    p_risk     = age_risk + (1.0 - age_risk) * sig_score * BOOST_SINAL
    ratio_3_14 = (mean_3d / mean_14d) if (not np.isnan(mean_14d) and mean_14d > 0) else float("nan")

    is_risco = (
        n_abaixo <= RISCO_N_MAX
        and not np.isnan(mediana_3d) and mediana_3d > RISCO_MEDIANA_OK
        and not np.isnan(min_3d) and min_3d < RISCO_FORCA_LIMIAR
    )

    return {
        "today":                 today.date(),
        "age_days":              int(age_days),
        "mean_3d":               round(mean_3d,    1) if not np.isnan(mean_3d)    else None,
        "mean_7d":               round(mean_7d,    1) if not np.isnan(mean_7d)    else None,
        "mean_14d":              round(mean_14d,   1) if not np.isnan(mean_14d)   else None,
        "min_3d":                round(min_3d,     1) if not np.isnan(min_3d)     else None,
        "mediana_3d":            round(mediana_3d, 1) if not np.isnan(mediana_3d) else None,
        "n_leituras_abaixo_800": n_abaixo,
        "ratio_3_14":            round(ratio_3_14, 4) if not np.isnan(ratio_3_14) else None,
        "slope_7d":              round(slope,       1),
        "slope_min_ciclo":       round(slope_min,   1),
        "proj_48h":              round(proj_48h, 1) if not np.isnan(proj_48h) else None,
        "age_risk":              round(age_risk,  4),
        "sig_score":             round(sig_score, 4),
        "p_risk":                round(p_risk,    4),
        "cond_aviso":            (p_risk >= AVISO_P_RISK or sig_score >= AVISO_SIGNAL) and age_days >= IDADE_MINIMA_DIAS,
        "cond_confirmado_red":   p_risk >= LIMIAR_P_RISK and sig_score >= LIMIAR_SIGNAL_SCORE and age_days >= IDADE_MINIMA_DIAS,
        "cond_confirmado_forca": not np.isnan(min_3d) and min_3d < CRITICO_FORCA_MIN,
        "cond_risco":            is_risco,
    }
