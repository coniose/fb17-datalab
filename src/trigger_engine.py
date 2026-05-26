"""
trigger_engine.py v3.0 — Motor de Gatilho Probabilístico Multi-Nível
Referência: SDD_Manutencao_Preditiva_v1.docx / Issue #1

Mudanças v2.3 → v3.0
  - Nova hierarquia aprovada com o time:
      RISCO      (ex-OUTLIER_SINAL) — leitura isolada, ir verificar no local
      CRITICO    (ex-EMERGENCIA)    — força crítica + p_risk ≥ 0.40
      EMERGENCIAL (novo, cumulativo) — ≥3 eventos + idade ≥ 85% eta_ajustado
                                       + p_risk ≥ 0.40 + MM7d declinante

  - Sistema de pontuação de desconfiança: `eventos_risco_ciclo` conta eventos
    força < 800 N no ciclo; reseta na troca; exibido nos cards como
    "Evento Nº{N} no ciclo atual".

  - Novo contrato JSON para TeamsPayload: schemas estruturados por nível
    (RISCO / CRITICO / EMERGENCIAL). RED / AMARELO / REVISAO continuam
    usando card_formatter.

  - TriggerFeatures expandido: mean_7d, mean_7d_3d_ago, eventos_risco_ciclo,
    eta_ajustado_dias, data_forca_min.

  - Migração de estado automática v2.3 → v3.0 em _load_state().

  - API pública inalterada: evaluate(), close_all_by_troca(), snooze(),
    compute_p_risk_snapshot().
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constantes módulo — valores padrão / fallback
# ─────────────────────────────────────────────────────────────────────────────

WEIBULL_BETA    = 1.181
WEIBULL_ETA_H   = 1297.0
WEIBULL_ETA_D   = WEIBULL_ETA_H / 24.0

LIMIAR_P_RISK         = 0.48
LIMIAR_SIGNAL_SCORE   = 0.22
IDADE_MINIMA_DIAS     = 15
PROJ_48H_LIMIAR       = 800.0
SUSTENTACAO_PROJ_DIAS = 2
BOOST_SINAL           = 0.65

COOLDOWN_H  = 48
SNOOZE_DIAS = 5

AMARELO_P_RISK     = 0.35
AMARELO_SIGNAL     = 0.15
AMARELO_COOLDOWN_H = 72

# RISCO (ex-OUTLIER_SINAL)
RISCO_FORCA_LIMIAR = 800.0
RISCO_MEDIANA_OK   = 950.0
RISCO_N_MAX        = 1
RISCO_COOLDOWN_H   = 48

# CRITICO (ex-EMERGENCIA)
CRITICO_FORCA_MIN  = 800.0
CRITICO_P_RISK_MIN = 0.40
CRITICO_COOLDOWN_H = 48

# EMERGENCIAL — cumulativo, novo em v3.0
EMERGENCIAL_MIN_EVENTOS = 3
EMERGENCIAL_IDADE_FRAC  = 0.85
EMERGENCIAL_P_RISK_MIN  = 0.40
EMERGENCIAL_COOLDOWN_H  = 48

REVISAO_MARCOS_DIAS = [20, 25, 35]

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
# Snapshot de métricas — v3.0
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
        "red": {
            "red_sp_id":    None,
            "last_fired":   None,
            "snooze_until": None,
            "proj_window":  [],
        },
        "amarelo":    {"sp_id": None, "last_fired": None},
        "critico":    {"sp_id": None, "last_fired": None},
        "emergencial":{"sp_id": None, "last_fired": None},
        "risco":      {"sp_id": None, "last_fired": None},
        "revisao":    {"marcos_disparados": []},
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
        if "red" not in data:
            logger.info("Estado v1 em '%s' — migrando para v3.0.", path)
            return defaults
        # Migração v2.3 → v3.0
        if "outlier_sinal" in data and "risco" not in data:
            data["risco"] = data.pop("outlier_sinal")
            logger.info("Migração v2.3→v3.0: outlier_sinal → risco")
        if "emergencia" in data and "critico" not in data:
            data["critico"] = data.pop("emergencia")
            logger.info("Migração v2.3→v3.0: emergencia → critico")
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


_DAY_END = pd.Timedelta(hours=23, minutes=59)   # inclui intradiário do dia atual


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
    """Worst (most negative) 7d slope observed since cycle_start.

    Physical justification: the roll degrades monotonically — force recovery
    is transient noise, not material recovery. The minimum slope is the best
    estimate of the true underlying degradation rate.

    Only considers windows fully within the cycle (starts at cycle_start + 7d).
    Falls back to the current slope when the cycle is younger than 7 days.
    """
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
# Card JSON estruturado — contrato v3.0 com Power Automate
# ─────────────────────────────────────────────────────────────────────────────
def _build_card_json(ev: TriggerEvent, features: TriggerFeatures) -> str:
    dias_restantes = int(round(features.eta_ajustado_dias - features.age_days))
    base: dict = {
        "nivel":             ev.gatilho,
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
        base["eventos_risco_ciclo"] = ev.evento_no_ciclo

    elif ev.gatilho == "EMERGENCIAL":
        if not np.isnan(features.min_3d):
            base["forca_min_ultima_amostra_gf"] = int(round(features.min_3d))
        base["data_forca_min"] = features.data_forca_min
        if not np.isnan(features.proj_48h):
            base["forca_projetada_48h_gf"] = int(round(features.proj_48h))
        if not np.isnan(features.mean_7d):
            base["media_movel_7d_atual_gf"] = int(round(features.mean_7d))
        base["p_risk"] = round(features.p_risk, 2)
        base["eventos_risco_ciclo"] = features.eventos_risco_ciclo

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
    """Leitura anômala isolada — força < 800 N, isolada, sinal geral saudável."""
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
            f"Ir verificar no local — analisar última amostra ({min_str} N) e identificar causa. "
            f"Se 2+ leituras abaixo de {self.forca_limiar:.0f} N nas próximas 72h, "
            "o sistema escalará para CRÍTICO."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = (
                f"RISCO FB14 — Leitura Anômala Isolada | "
                f"Evento Nº{evento_n} no ciclo atual | "
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
    """Força crítica confirmada + p_risk ≥ 0.40 — Análise aprofundada + planejamento."""
    name      = "CRITICO"
    severity  = "ALTA"
    state_key = "critico"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.forca_min    = float(cfg.get("critico_forca_min",  CRITICO_FORCA_MIN))
        self.p_risk_min   = float(cfg.get("critico_p_risk_min", CRITICO_P_RISK_MIN))
        self.cooldown_h   = float(cfg.get("critico_cooldown_h", CRITICO_COOLDOWN_H))
        self._mediana_ok  = float(cfg.get("risco_mediana_ok",   RISCO_MEDIANA_OK))
        self._risco_n_max = int(cfg.get("risco_n_max",          RISCO_N_MAX))

    def _is_outlier(self, features: TriggerFeatures) -> bool:
        return (
            features.n_leituras_abaixo_800 <= self._risco_n_max
            and not np.isnan(features.mediana_3d)
            and features.mediana_3d > self._mediana_ok
        )

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        if features.age_days < 5:
            return False
        if np.isnan(features.min_3d) or features.min_3d >= self.forca_min:
            return False
        if features.p_risk < self.p_risk_min:
            return False
        if _in_cooldown(state[self.state_key].get("last_fired"), self.cooldown_h, features.today):
            return False
        return not self._is_outlier(features)

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        evento_n = state.get("eventos_risco_ciclo", 0) + 1
        min_str  = f"{features.min_3d:.0f}" if not np.isnan(features.min_3d) else "N/D"
        acao = "Análise aprofundada e planejamento de troca do rolo maintacker."
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = (
                f"CRÍTICO FB14 — Força Crítica + Risco Elevado | "
                f"Evento Nº{evento_n} no ciclo atual | "
                f"Dia {int(features.age_days)} | {min_str} N | p_risk={features.p_risk:.2f}"
            ),
            idade_maintacker = int(features.age_days),
            data_disparo     = features.today.isoformat(),
            acao_recomendada = acao,
            evento_no_ciclo  = evento_n,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.min_3d, 1) if not np.isnan(features.min_3d) else None,
        )

    def update_state(self, state: dict, ts: str) -> None:
        state[self.state_key]["last_fired"] = ts
        state["eventos_risco_ciclo"] = state.get("eventos_risco_ciclo", 0) + 1


class EmergencialTrigger(TriggerBase):
    """
    Avaliação cumulativa do ciclo — Retenção iminente.

    Dispara quando o quadro completo indica fim de vida próximo:
    ≥ N eventos força < 800 N + idade ≥ frac × eta_ajustado
    + p_risk ≥ limiar + MM7d declinante.
    """
    name      = "EMERGENCIAL"
    severity  = "CRITICA"
    state_key = "emergencial"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.min_eventos = int(cfg.get("emergencial_min_eventos",  EMERGENCIAL_MIN_EVENTOS))
        self.idade_frac  = float(cfg.get("emergencial_idade_frac", EMERGENCIAL_IDADE_FRAC))
        self.p_risk_min  = float(cfg.get("emergencial_p_risk_min", EMERGENCIAL_P_RISK_MIN))
        self.cooldown_h  = float(cfg.get("emergencial_cooldown_h", EMERGENCIAL_COOLDOWN_H))

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        if _in_cooldown(state[self.state_key].get("last_fired"), self.cooldown_h, features.today):
            return False
        if state.get("eventos_risco_ciclo", 0) < self.min_eventos:
            return False
        if features.age_days < features.eta_ajustado_dias * self.idade_frac:
            return False
        if features.p_risk < self.p_risk_min:
            return False
        if (not np.isnan(features.mean_7d) and not np.isnan(features.mean_7d_3d_ago)
                and features.mean_7d >= features.mean_7d_3d_ago):
            return False
        return True

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        min_str = f"{features.min_3d:.0f}" if not np.isnan(features.min_3d) else "N/D"
        acao = "Risco iminente de retenção — acionar troca imediata do rolo maintacker."
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = (
                f"EMERGENCIAL FB14 — Risco Iminente de Retenção | "
                f"Evento Nº{features.eventos_risco_ciclo} no ciclo | "
                f"Dia {int(features.age_days)} | {min_str} N | p_risk={features.p_risk:.2f}"
            ),
            idade_maintacker = int(features.age_days),
            data_disparo     = features.today.isoformat(),
            acao_recomendada = acao,
            evento_no_ciclo  = features.eventos_risco_ciclo,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.min_3d, 1) if not np.isnan(features.min_3d) else None,
        )


class RedTrigger(TriggerBase):
    """Gatilho principal: C1 AND C2 AND C3 AND C4."""
    name      = "RED"
    severity  = "ALTA"
    state_key = "red"
    sp_id_key = "red_sp_id"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.limiar_p_risk       = float(cfg.get("limiar_p_risk",       LIMIAR_P_RISK))
        self.limiar_signal_score = float(cfg.get("limiar_signal_score", LIMIAR_SIGNAL_SCORE))
        self.idade_minima_dias   = float(cfg.get("idade_minima_dias",   IDADE_MINIMA_DIAS))
        self.proj_48h_limiar     = float(cfg.get("proj_48h_limiar",     PROJ_48H_LIMIAR))
        self.sustentacao_proj    = int(cfg.get("sustentacao_proj_dias", SUSTENTACAO_PROJ_DIAS))
        self.cooldown_h          = float(cfg.get("cooldown_h",          COOLDOWN_H))
        self.snooze_dias         = int(cfg.get("snooze_dias",           SNOOZE_DIAS))

    def default_state(self) -> dict:
        return {"red_sp_id": None, "last_fired": None, "snooze_until": None, "proj_window": []}

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        e = state[self.state_key]
        if _in_cooldown(e.get("last_fired"), self.cooldown_h, features.today):
            return False
        if e.get("snooze_until"):
            if features.today <= _to_utc(e["snooze_until"]).normalize():
                return False

        window = e.get("proj_window", [])
        window.append({
            "date": features.today.isoformat()[:10],
            "proj": round(float(features.proj_48h), 1) if not np.isnan(features.proj_48h) else 9999.0,
        })
        e["proj_window"] = window[-5:]
        n_below = sum(1 for d in e["proj_window"] if d["proj"] < self.proj_48h_limiar)

        cond1 = features.p_risk    >= self.limiar_p_risk
        cond2 = features.sig_score >= self.limiar_signal_score
        cond3 = features.age_days  >= self.idade_minima_dias
        cond4 = n_below            >= self.sustentacao_proj
        logger.debug(
            "[%s] RED | p_risk=%.3f(C1=%s) sig=%.3f(C2=%s) age=%.0fd(C3=%s) proj_below=%d/5(C4=%s)",
            features.today.date(), features.p_risk, cond1, features.sig_score, cond2,
            features.age_days, cond3, n_below, cond4,
        )
        return cond1 and cond2 and cond3 and cond4

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        slope_str = f"{features.slope_7d:+.0f}"
        proj_str  = f"{features.proj_48h:.0f}" if not np.isnan(features.proj_48h) else "N/D"
        mean_str  = f"{features.mean_3d:.0f}"  if not np.isnan(features.mean_3d)  else "N/D"
        mensagem = (
            f"ALERTA FB14 (IC-I1-14) — Risco de Impacto na Força de Selagem\n"
            f"Rolo ativo há {int(features.age_days)} dias | "
            f"Força média 72h: {mean_str} N | "
            f"Projeção 48h: {proj_str} N (tendência: {slope_str} N/dia)\n"
            f"Probabilidade de impacto nas próximas 48-72h: {features.p_risk*100:.0f}%"
        )
        acao = (
            "Programar inspeção preventiva do rolo. "
            "Verificar força de selagem no próximo turno. "
            "Registrar resultado: OK / Troca programada / Troca imediata."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = features.today.isoformat(),
            acao_recomendada = acao,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.mean_3d, 1) if not np.isnan(features.mean_3d) else None,
            proj_48h         = round(features.proj_48h, 1) if not np.isnan(features.proj_48h) else None,
            signal_score     = round(features.sig_score, 4),
            age_risk         = round(features.age_risk, 4),
        )

    def update_state(self, state: dict, ts: str) -> None:
        state[self.state_key]["last_fired"] = ts
        state[self.state_key]["red_sp_id"]  = -1


class AmarelhoTrigger(TriggerBase):
    """Aviso precoce de anomalia."""
    name      = "AMARELO"
    severity  = "MEDIA"
    state_key = "amarelo"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.amarelo_p_risk    = float(cfg.get("amarelo_p_risk",      AMARELO_P_RISK))
        self.amarelo_signal    = float(cfg.get("amarelo_signal",      AMARELO_SIGNAL))
        self.cooldown_h        = float(cfg.get("amarelo_cooldown_h",  AMARELO_COOLDOWN_H))
        self.idade_minima_dias = float(cfg.get("idade_minima_dias",   IDADE_MINIMA_DIAS))
        self.limiar_p_risk     = float(cfg.get("limiar_p_risk",       LIMIAR_P_RISK))
        self.limiar_signal     = float(cfg.get("limiar_signal_score", LIMIAR_SIGNAL_SCORE))
        self.red_cooldown_h    = float(cfg.get("cooldown_h",          COOLDOWN_H))

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        if features.age_days < self.idade_minima_dias:
            return False
        if _in_cooldown(state["red"].get("last_fired"), self.red_cooldown_h, features.today):
            return False
        if _in_cooldown(state[self.state_key].get("last_fired"), self.cooldown_h, features.today):
            return False
        cond_a = features.p_risk    >= self.amarelo_p_risk
        cond_b = features.sig_score >= self.amarelo_signal
        if not (cond_a or cond_b):
            return False
        if (features.p_risk >= self.limiar_p_risk and features.sig_score >= self.limiar_signal
                and features.age_days >= self.idade_minima_dias):
            return False
        return True

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        mean_str  = f"{features.mean_3d:.0f}" if not np.isnan(features.mean_3d) else "N/D"
        slope_str = f"{features.slope_7d:+.0f}"
        razao = "risco em elevação" if features.p_risk >= self.amarelo_p_risk else "degradação de sinal detectada"
        mensagem = (
            f"AVISO FB14 (IC-I1-14) — Anomalia Detectada no Sinal de Selagem\n"
            f"Rolo ativo há {int(features.age_days)} dias | "
            f"Força média 72h: {mean_str} N | Tendência: {slope_str} N/dia\n"
            f"Probabilidade de impacto atual: {features.p_risk*100:.0f}% ({razao})\n"
            "Monitoramento reforçado recomendado. Não requer ação imediata."
        )
        acao = (
            "Aumentar frequência de monitoramento do sinal de força. "
            "Registrar observações no próximo turno. "
            "Aguardar evolução — sistema irá escalar para RED se tendência continuar."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = features.today.isoformat(),
            acao_recomendada = acao,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.min_3d, 1) if not np.isnan(features.min_3d) else None,
            proj_48h         = round(features.proj_48h, 1) if not np.isnan(features.proj_48h) else None,
            signal_score     = round(features.sig_score, 4),
        )

    def update_state(self, state: dict, ts: str) -> None:
        state[self.state_key]["last_fired"] = ts
        state[self.state_key]["sp_id"]      = -1


class RevisaoTrigger(TriggerBase):
    """Marco automático de ciclo — dias 20, 25, 35."""
    name      = "REVISAO"
    severity  = "INFO"
    state_key = "revisao"
    sp_id_key = None

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        marcos_raw = cfg.get("revisao_marcos_dias", REVISAO_MARCOS_DIAS)
        self.marcos         = sorted(int(m) for m in marcos_raw)
        self.amarelo_p_risk = float(cfg.get("amarelo_p_risk", AMARELO_P_RISK))
        self.amarelo_signal = float(cfg.get("amarelo_signal", AMARELO_SIGNAL))
        self.limiar_p_risk  = float(cfg.get("limiar_p_risk",  LIMIAR_P_RISK))
        self._marco_ativo: Optional[int] = None

    def default_state(self) -> dict:
        return {"marcos_disparados": []}

    def _get_marco(self, features: TriggerFeatures, state: dict) -> Optional[int]:
        disparados = state[self.state_key].get("marcos_disparados", [])
        for marco in self.marcos:
            if features.age_days >= marco and marco not in disparados:
                return marco
        return None

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        self._marco_ativo = self._get_marco(features, state)
        return self._marco_ativo is not None

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        marco     = self._marco_ativo
        mean_str  = f"{features.mean_3d:.0f}" if not np.isnan(features.mean_3d) else "N/D"
        min_str   = f"{features.min_3d:.0f}"  if not np.isnan(features.min_3d)  else "N/D"
        slope_str = f"{features.slope_7d:+.0f}"

        if features.p_risk < 0.25 and features.sig_score < 0.10:
            estado = "NORMAL — rolo com desempenho dentro do esperado"
        elif features.p_risk < self.amarelo_p_risk and features.sig_score < self.amarelo_signal:
            estado = "ATENÇÃO — sinais leves de desgaste, monitoramento normal"
        elif features.p_risk < self.limiar_p_risk:
            estado = "ELEVADO — degradação em andamento, monitoramento reforçado"
        else:
            estado = "CRÍTICO — condições de disparo RED iminentes"

        proximo = next((m for m in self.marcos if m > marco), "N/A")
        mensagem = (
            f"REVISÃO AUTOMÁTICA FB14 (IC-I1-14) — Marco de Ciclo: Dia {marco}\n"
            f"Rolo ativo há {int(features.age_days)} dias | Estado: {estado}\n"
            f"Força média 72h: {mean_str} N | Mínimo 72h: {min_str} N | "
            f"Tendência: {slope_str} N/dia\n"
            f"Probabilidade de impacto acumulada: {features.p_risk*100:.0f}%"
        )
        acao = (
            f"Revisar gráfico de tendência de força. "
            f"Se forças abaixo de 900 N ou tendência negativa persistente, "
            f"antecipar inspeção do rolo. Próximo marco automático: dia {proximo}."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = features.today.isoformat(),
            acao_recomendada = acao,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.min_3d, 1) if not np.isnan(features.min_3d) else None,
            proj_48h         = round(features.proj_48h, 1) if not np.isnan(features.proj_48h) else None,
            signal_score     = round(features.sig_score, 4),
        )

    def update_state(self, state: dict, ts: str) -> None:
        state[self.state_key]["marcos_disparados"].append(self._marco_ativo)


# ─────────────────────────────────────────────────────────────────────────────
# Motor principal
# ─────────────────────────────────────────────────────────────────────────────
class TriggerEngine:
    """
    Motor de gatilho probabilístico multi-nível — v3.0

    Ordem de avaliação: RISCO → CRITICO → EMERGENCIAL → RED → AMARELO → REVISAO
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
            EmergencialTrigger(cfg),
            RedTrigger(cfg),
            AmarelhoTrigger(cfg),
            RevisaoTrigger(cfg),
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
        """
        Avalia todos os gatilhos para `today`.

        Args:
            media_col: coluna de força para degradação/projeção.
                       min_3d, mediana_3d e n_leituras sempre usam "Media" bruta.
        """
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
                today.date(), "+".join(ev.gatilho for ev in fired), p_risk, sig_score,
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
        self.state["red"]["snooze_until"]   = snooze_fim
        self.state["amarelo"]["last_fired"] = datetime.now().isoformat()
        if sp_client and sp_id and sp_id > 0:
            sp_client.update_list_items(list_name, [sp_id], "Status",    "SNOOZE")
            sp_client.update_list_items(list_name, [sp_id], "SnoozeFim", snooze_fim)
        _save_state(self.state, self.state_path)
        logger.info("RED (ID=%s) em snooze até %s.", sp_id, snooze_fim)

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
        # Monta o payload sempre — necessário mesmo em dry-run para diagnóstico
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
                )
            elif ev.gatilho == "EMERGENCIAL":
                from .card_formatter import build_emergencial_card
                ev.teams_payload = build_emergencial_card(
                    maquina             = ev.maquina,
                    idade_dias          = ev.idade_maintacker,
                    p_risk              = features.p_risk,
                    slope_7d            = ev.slope_forca_7d,
                    forca_min_3d        = features.min_3d if not np.isnan(features.min_3d) else None,
                    proj_48h            = features.proj_48h if not np.isnan(features.proj_48h) else None,
                    media_7d            = features.mean_7d if not np.isnan(features.mean_7d) else None,
                    acao_recomendada    = ev.acao_recomendada,
                    data_disparo        = datetime.fromisoformat(ev.data_disparo[:19]),
                    vida_ref_dias       = eta_ajustado_dias or self.weibull_eta_d,
                    eventos_risco_ciclo = features.eventos_risco_ciclo,
                )
            else:
                from .card_formatter import build_alert_card
                ev.teams_payload = build_alert_card(
                    maquina              = ev.maquina,
                    gatilho              = ev.gatilho,
                    idade_dias           = ev.idade_maintacker,
                    p_risk               = ev.score_atual or 0.0,
                    slope_7d             = ev.slope_forca_7d,
                    forca_min_3d         = ev.forca_minima_3d,
                    proj_48h             = ev.proj_48h,
                    acao_recomendada     = ev.acao_recomendada,
                    data_disparo         = datetime.fromisoformat(ev.data_disparo[:19]),
                    vida_ref_dias        = eta_ajustado_dias or self.weibull_eta_d,
                    n_abaixo_800_ciclo   = features.eventos_risco_ciclo,
                    forca_min_ciclo      = features.forca_min_ciclo,
                )
        except Exception as exc:
            logger.warning("card build falhou (%s) — TeamsPayload omitido.", exc)

        if sp_client is None:
            logger.warning("[dry-run] %s não persistido (sp_client=None).", ev.gatilho)
            return

        _SP_CAMPOS_ATIVOS = {"Title", "Maquina", "TeamsPayload"}
        sp_payload = {k: v for k, v in ev.to_sp_dict().items()
                      if k in _SP_CAMPOS_ATIVOS and v is not None}
        ids = sp_client.insert_list_item(list_name, [sp_payload])
        if ids and ids[0] is not None:
            ev.sp_item_id = ids[0]
            if trigger.sp_id_key is not None:
                self.state[trigger.state_key][trigger.sp_id_key] = ids[0]
            logger.info("%s inserido na lista '%s' (ID=%s).", ev.gatilho, list_name, ids[0])
        else:
            logger.warning("%s inserido mas ID não retornado.", ev.gatilho)


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
        "cond_p_risk":           p_risk    >= LIMIAR_P_RISK,
        "cond_signal":           sig_score >= LIMIAR_SIGNAL_SCORE,
        "cond_idade":            age_days  >= IDADE_MINIMA_DIAS,
        "cond_proj":             proj_48h  <  PROJ_48H_LIMIAR if not np.isnan(proj_48h) else False,
        "cond_critico":          not np.isnan(min_3d) and min_3d < CRITICO_FORCA_MIN,
        "cond_risco":            is_risco,
    }
