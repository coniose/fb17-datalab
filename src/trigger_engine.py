"""
trigger_engine.py v2.3 — Motor de Gatilho Probabilístico Multi-Nível
Referência: SDD_Manutencao_Preditiva_v1.docx

Mudanças v2.2 → v2.3
  - Arquitetura OO: cada gatilho é uma subclasse de TriggerBase com
    check(), build_event() e update_state() — facilita adicionar ou
    remover gatilhos sem alterar o motor central.

  - Novo dataclass TriggerFeatures: snapshot de todas as métricas
    calculadas para um instante, passado para cada gatilho em vez de
    argumentos posicionais avulsos.

  - Novo gatilho OUTLIER_SINAL (avaliado antes de EMERGENCIA):
    detecta leituras anômalas isoladas (<800 N) onde o sinal se recupera
    espontaneamente (n_leituras_abaixo ≤ 1 E mediana_3d > 950 N).
    Pesquisa histórica: 81% dos eventos <800 N são transientes — o time
    sempre continuou rodando, mediana de 34 dias até a próxima troca.

  - EmergenciaTrigger.check() retorna False quando a condição de outlier
    é satisfeita — OUTLIER_SINAL e EMERGENCIA são mutuamente exclusivos.

  - Dois novos helpers de feature: _rolling_median(), _count_below().

  - API pública de TriggerEngine inalterada (compatível com v2.2):
    evaluate(), close_all_by_troca(), snooze(), compute_p_risk_snapshot().
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
# Constantes módulo — valores padrão / fallback
# ─────────────────────────────────────────────────────────────────────────────

WEIBULL_BETA     = 1.181
WEIBULL_ETA_H    = 1297.0
WEIBULL_ETA_D    = WEIBULL_ETA_H / 24.0

LIMIAR_P_RISK          = 0.48
LIMIAR_SIGNAL_SCORE    = 0.22
IDADE_MINIMA_DIAS      = 15
PROJ_48H_LIMIAR        = 800.0
SUSTENTACAO_PROJ_DIAS  = 2
BOOST_SINAL            = 0.65

COOLDOWN_H   = 48
SNOOZE_DIAS  = 5

AMARELO_P_RISK      = 0.35
AMARELO_SIGNAL      = 0.15
AMARELO_COOLDOWN_H  = 72

FORCA_MIN_EMERGENCIA  = 800.0
EMERGENCIA_COOLDOWN_H = 48

REVISAO_MARCOS_DIAS   = [20, 25, 35]

# OUTLIER_SINAL — v2.3
OUTLIER_FORCA_LIMIAR = 800.0   # N — mesmo gate da EMERGENCIA
OUTLIER_MEDIANA_OK   = 950.0   # N — mediana mínima para classificar como outlier
OUTLIER_N_MAX        = 1       # máx de leituras abaixo do limiar (janela 3d)
OUTLIER_COOLDOWN_H   = 48

JANELA_SLOPE_D   = 7
JANELA_MEAN_3D   = 3
JANELA_MEAN_14D  = 14

DEFAULT_LIST_NAME = "Gatilhos_Selagem"

# ─────────────────────────────────────────────────────────────────────────────
# Schema SharePoint (referência)
# ─────────────────────────────────────────────────────────────────────────────
SP_LIST_SCHEMA = {
    "Title":           "Linha única de texto — chave: Maquina|Gatilho|Data",
    "Maquina":         "Linha única de texto",
    "Gatilho":         "Linha única de texto  (RED | AMARELO | EMERGENCIA | REVISAO | OUTLIER_SINAL)",
    "Severidade":      "Linha única de texto  (CRITICA | ALTA | MEDIA | INFO)",
    "Mensagem":        "Várias linhas de texto",
    "IdadeMaintacker": "Número",
    "ScoreAtual":      "Número (decimal) — p_risk",
    "SlopeForca7d":    "Número (decimal)",
    "ForcaMinima3d":   "Número (decimal) — mean_3d ou min_3d",
    "DataDisparo":     "Linha única de texto  (ISO 8601)",
    "AcaoRecomendada": "Várias linhas de texto",
    "Status":          "Opção: ATIVO | FECHADO | SNOOZE",
    "SnoozeFim":       "Linha única de texto  (YYYY-MM-DD, opcional)",
    "TeamsPayload":    "Várias linhas de texto (texto sem formatação) — JSON do Adaptive Card",
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
    data_disparo: str                    # ISO 8601
    acao_recomendada: str
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
            "Title":            f"{self.maquina} | {self.gatilho} | {self.data_disparo[:10]}",
            "Maquina":          self.maquina,
            "Gatilho":          self.gatilho,
            "Severidade":       self.severidade,
            "Mensagem":         self.mensagem,
            "IdadeMaintacker":  int(self.idade_maintacker),
            "DataDisparo":      self.data_disparo,
            "AcaoRecomendada":  self.acao_recomendada,
            "Status":           self.status,
        }
        if self.score_atual is not None:
            d["ScoreAtual"]    = round(float(self.score_atual), 4)
        if self.slope_forca_7d is not None:
            d["SlopeForca7d"]  = round(float(self.slope_forca_7d), 1)
        if self.forca_minima_3d is not None:
            d["ForcaMinima3d"] = round(float(self.forca_minima_3d), 1)
        if self.snooze_fim:
            d["SnoozeFim"]     = self.snooze_fim
        if self.teams_payload:
            d["TeamsPayload"]  = self.teams_payload
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot de métricas — v2.3
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TriggerFeatures:
    today: pd.Timestamp
    age_days: float
    mean_3d: float
    mean_14d: float
    min_3d: float
    mediana_3d: float           # mediana da janela 3d (coluna "Media" bruta)
    n_leituras_abaixo_800: int  # leituras < forca_min_emergencia nos últimos 3d
    slope_7d: float
    proj_48h: float
    sig_score: float
    age_risk: float
    p_risk: float


# ─────────────────────────────────────────────────────────────────────────────
# Estado interno persistido em JSON
# ─────────────────────────────────────────────────────────────────────────────
def _default_state(maquina: str) -> dict:
    return {
        "maquina": maquina,
        "red": {
            "red_sp_id":    None,
            "last_fired":   None,
            "snooze_until": None,
            "proj_window":  [],
        },
        "amarelo": {
            "sp_id":      None,
            "last_fired": None,
        },
        "emergencia": {
            "sp_id":      None,
            "last_fired": None,
        },
        "revisao": {
            "marcos_disparados": [],
        },
        "outlier_sinal": {          # v2.3
            "sp_id":      None,
            "last_fired": None,
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
        if "red" not in data:
            logger.info("Estado v1 detectado em '%s' — migrando para formato v2.3.", path)
            return defaults
        _merge_defaults(data, defaults)
        return data
    return defaults


def _save_state(state: dict, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Carregamento de configuração YAML
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
                    logger.debug("Configuração trigger carregada de '%s'.", p)
                return trig
            except Exception as exc:
                logger.warning("Erro ao carregar config.yaml ('%s'): %s", p, exc)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de feature
# ─────────────────────────────────────────────────────────────────────────────
def _rolling_mean(df: pd.DataFrame, col: str, today: pd.Timestamp, days: int) -> float:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days):t, col].dropna()
    return float(sub.mean()) if len(sub) > 0 else float("nan")


def _rolling_min(df: pd.DataFrame, col: str, today: pd.Timestamp, days: int) -> float:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days):t, col].dropna()
    return float(sub.min()) if len(sub) > 0 else float("nan")


def _rolling_median(df: pd.DataFrame, col: str, today: pd.Timestamp, days: int) -> float:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days):t, col].dropna()
    return float(sub.median()) if len(sub) > 0 else float("nan")


def _count_below(df: pd.DataFrame, col: str, today: pd.Timestamp,
                 days: int, threshold: float) -> int:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=days):t, col].dropna()
    return int((sub < threshold).sum())


def _slope_7d(df: pd.DataFrame, col: str, today: pd.Timestamp) -> float:
    t = _align_tz(today, df.index)
    sub = df.loc[t - pd.Timedelta(days=JANELA_SLOPE_D):t, col].dropna()
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


def _compute_signal_score(mean_3d: float, mean_14d: float, slope_7d: float,
                           boost: float = BOOST_SINAL) -> float:
    if np.isnan(mean_3d) or np.isnan(mean_14d) or mean_14d == 0:
        return 0.0
    deg_signal   = max(0.0, 1.0 - mean_3d / mean_14d)
    slope_danger = float(np.clip(-slope_7d / 50.0, 0.0, 1.0))
    return float(deg_signal * 0.6 + slope_danger * 0.4)


def _compute_proj_48h(mean_3d: float, slope_7d: float) -> float:
    if np.isnan(mean_3d):
        return float("nan")
    return float(mean_3d + slope_7d * 2.0)


def _to_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _align_tz(ts: pd.Timestamp, index: pd.DatetimeIndex) -> pd.Timestamp:
    if index.tz is None:
        return ts.tz_localize(None) if ts.tzinfo is not None else ts
    if ts.tzinfo is None:
        return ts.tz_localize(index.tz)
    return ts.tz_convert(index.tz)


def _in_cooldown(last_fired_iso: Optional[str], cooldown_h: float,
                 today: pd.Timestamp) -> bool:
    if not last_fired_iso:
        return False
    h_desde = (today - _to_utc(last_fired_iso)).total_seconds() / 3600
    return h_desde < cooldown_h


# ─────────────────────────────────────────────────────────────────────────────
# TriggerBase — interface de cada gatilho
# ─────────────────────────────────────────────────────────────────────────────
class TriggerBase(ABC):
    """
    Contrato de um gatilho. Cada subclasse define:
      - name, severity, state_key  (atributos de classe)
      - sp_id_key: chave do sp_id em state[state_key]; None = não persiste
      - check()       — condicional; retorna True quando deve disparar
      - build_event() — constrói o TriggerEvent (mensagem Teams + metadados)
      - update_state()— atualiza state após disparo (padrão: grava last_fired)
      - default_state()— estrutura inicial de state[state_key]
    """
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

class OutlierSinalTrigger(TriggerBase):
    """
    Leitura anômala isolada — v2.3.

    Dispara quando min_3d < limiar mas o evento é isolado (apenas 1 leitura
    abaixo na janela 3d) e o restante do sinal está saudável (mediana > 950 N).
    Emite INFO ao invés de CRITICA para evitar fadiga de alerta.
    """
    name      = "OUTLIER_SINAL"
    severity  = "INFO"
    state_key = "outlier_sinal"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.forca_limiar = float(cfg.get("outlier_forca_limiar", OUTLIER_FORCA_LIMIAR))
        self.mediana_ok   = float(cfg.get("outlier_mediana_ok",   OUTLIER_MEDIANA_OK))
        self.n_max        = int(cfg.get("outlier_n_max",          OUTLIER_N_MAX))
        self.cooldown_h   = float(cfg.get("outlier_cooldown_h",   OUTLIER_COOLDOWN_H))

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        if features.age_days < 5:
            return False
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
        ts          = features.today.isoformat()
        min_str     = f"{features.min_3d:.0f}"
        mediana_str = f"{features.mediana_3d:.0f}"
        mensagem = (
            f"OUTLIER_SINAL FB14 (IC-I1-14) — Leitura Anomala Isolada\n"
            f"Rolo ativo ha {int(features.age_days)} dias | "
            f"Leitura pontual: {min_str} N (abaixo de {self.forca_limiar:.0f} N)\n"
            f"Sinal geral saudavel: mediana 72h = {mediana_str} N\n"
            f"Evento isolado — sem evidencia de degradacao real no momento."
        )
        acao = (
            "Registrar se a leitura anomala se repetiu no proximo turno. "
            f"Se 2+ leituras abaixo de {self.forca_limiar:.0f} N nas proximas 72h, "
            "o sistema escalara para EMERGENCIA automaticamente."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = ts,
            acao_recomendada = acao,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.min_3d, 1),
        )


class EmergenciaTrigger(TriggerBase):
    """
    Chequemate — força crítica real (<800 N, não isolada).

    Retorna False quando a condição de OUTLIER_SINAL é satisfeita,
    garantindo que os dois gatilhos sejam mutuamente exclusivos.
    """
    name      = "EMERGENCIA"
    severity  = "CRITICA"
    state_key = "emergencia"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.forca_min      = float(cfg.get("forca_min_emergencia",  FORCA_MIN_EMERGENCIA))
        self.cooldown_h     = float(cfg.get("emergencia_cooldown_h", EMERGENCIA_COOLDOWN_H))
        self._outlier_med   = float(cfg.get("outlier_mediana_ok",    OUTLIER_MEDIANA_OK))
        self._outlier_n_max = int(cfg.get("outlier_n_max",           OUTLIER_N_MAX))

    def check(self, features: TriggerFeatures, state: dict) -> bool:
        if features.age_days < 5:
            return False
        if np.isnan(features.min_3d) or features.min_3d >= self.forca_min:
            return False
        if _in_cooldown(state[self.state_key].get("last_fired"), self.cooldown_h, features.today):
            return False
        # Defer to OUTLIER_SINAL when the reading is isolated and signal is healthy
        is_outlier = (
            features.n_leituras_abaixo_800 <= self._outlier_n_max
            and not np.isnan(features.mediana_3d)
            and features.mediana_3d > self._outlier_med
        )
        return not is_outlier

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        ts        = features.today.isoformat()
        mean_str  = f"{features.mean_3d:.0f}" if not np.isnan(features.mean_3d) else "N/D"
        slope_str = f"{features.slope_7d:+.0f}"
        min_str   = f"{features.min_3d:.0f}"
        mensagem = (
            f"EMERGENCIA FB14 (IC-I1-14) — Forca de Selagem em Nivel Critico\n"
            f"Rolo ativo ha {int(features.age_days)} dias | "
            f"Minimo registrado 72h: {min_str} N (limite critico: {self.forca_min:.0f} N)\n"
            f"Forca media 72h: {mean_str} N | Tendencia: {slope_str} N/dia\n"
            f"Este nivel de forca compromete diretamente a qualidade da selagem."
        )
        acao = (
            "ACAO IMEDIATA REQUERIDA: verificar forca de selagem no proximo turno. "
            "Se confirmado abaixo de 800 N, programar troca do rolo maintacker. "
            "Inspecionar regulagem de pressao e desgaste do rolo."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = ts,
            acao_recomendada = acao,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.min_3d, 1),
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
            snooze_ts = _to_utc(e["snooze_until"]).normalize()
            if features.today <= snooze_ts:
                return False

        # Mantém a janela de projeção 48h (efeito colateral necessário a cada tick)
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
            "[%s] RED | p_risk=%.3f(C1=%s) sig=%.3f(C2=%s) age=%.0fd(C3=%s) "
            "proj_below=%d/5(C4=%s)",
            features.today.date(), features.p_risk, cond1, features.sig_score, cond2,
            features.age_days, cond3, n_below, cond4,
        )
        return cond1 and cond2 and cond3 and cond4

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        ts        = features.today.isoformat()
        slope_str = f"{features.slope_7d:+.0f}"
        p_pct     = features.p_risk * 100
        proj_str  = f"{features.proj_48h:.0f}" if not np.isnan(features.proj_48h) else "N/D"
        mean_str  = f"{features.mean_3d:.0f}"  if not np.isnan(features.mean_3d)  else "N/D"
        mensagem = (
            f"ALERTA FB14 (IC-I1-14) — Risco de Impacto na Forca de Selagem\n"
            f"Rolo ativo ha {int(features.age_days)} dias | "
            f"Forca media 72h: {mean_str} N | "
            f"Projecao 48h: {proj_str} N (tendencia: {slope_str} N/dia)\n"
            f"Probabilidade de impacto nas proximas 48-72h: {p_pct:.0f}%"
        )
        acao = (
            "Programar inspecao preventiva do rolo. "
            "Verificar forca de selagem no proximo turno. "
            "Registrar resultado: OK / Troca programada / Troca imediata."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = ts,
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
        if (features.p_risk >= self.limiar_p_risk
                and features.sig_score >= self.limiar_signal
                and features.age_days >= self.idade_minima_dias):
            return False
        return True

    def build_event(self, maquina: str, features: TriggerFeatures,
                    state: dict) -> TriggerEvent:
        ts        = features.today.isoformat()
        mean_str  = f"{features.mean_3d:.0f}" if not np.isnan(features.mean_3d) else "N/D"
        slope_str = f"{features.slope_7d:+.0f}"
        p_pct     = features.p_risk * 100
        razao     = "risco em elevacao" if features.p_risk >= self.amarelo_p_risk else "degradacao de sinal detectada"
        mensagem = (
            f"AVISO FB14 (IC-I1-14) — Anomalia Detectada no Sinal de Selagem\n"
            f"Rolo ativo ha {int(features.age_days)} dias | "
            f"Forca media 72h: {mean_str} N | "
            f"Tendencia: {slope_str} N/dia\n"
            f"Probabilidade de impacto atual: {p_pct:.0f}% ({razao})\n"
            f"Monitoramento reforcado recomendado. Nao requer acao imediata."
        )
        acao = (
            "Aumentar frequencia de monitoramento do sinal de forca. "
            "Registrar observacoes no proximo turno. "
            "Aguardar evolucao — sistema ira escalar para RED se tendencia continuar."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = ts,
            acao_recomendada = acao,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.mean_3d, 1) if not np.isnan(features.mean_3d) else None,
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
    sp_id_key = None   # REVISAO não persiste sp_id

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        marcos_raw = cfg.get("revisao_marcos_dias", REVISAO_MARCOS_DIAS)
        self.marcos         = sorted(int(m) for m in marcos_raw)
        self.amarelo_p_risk = float(cfg.get("amarelo_p_risk", AMARELO_P_RISK))
        self.amarelo_signal = float(cfg.get("amarelo_signal", AMARELO_SIGNAL))
        self.limiar_p_risk  = float(cfg.get("limiar_p_risk",  LIMIAR_P_RISK))
        self._marco_ativo: Optional[int] = None  # preenchido em check()

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
        marco = self._marco_ativo
        ts        = features.today.isoformat()
        mean_str  = f"{features.mean_3d:.0f}" if not np.isnan(features.mean_3d) else "N/D"
        min_str   = f"{features.min_3d:.0f}"  if not np.isnan(features.min_3d)  else "N/D"
        slope_str = f"{features.slope_7d:+.0f}"
        p_pct     = features.p_risk * 100

        if features.p_risk < 0.25 and features.sig_score < 0.10:
            estado = "NORMAL — rolo com desempenho dentro do esperado"
        elif features.p_risk < self.amarelo_p_risk and features.sig_score < self.amarelo_signal:
            estado = "ATENCAO — sinais leves de desgaste, monitoramento normal"
        elif features.p_risk < self.limiar_p_risk:
            estado = "ELEVADO — degradacao em andamento, monitoramento reforcado"
        else:
            estado = "CRITICO — condicoes de disparo RED iminentes"

        proximo = next((m for m in self.marcos if m > marco), "N/A")
        mensagem = (
            f"REVISAO AUTOMATICA FB14 (IC-I1-14) — Marco de Ciclo: Dia {marco}\n"
            f"Rolo ativo ha {int(features.age_days)} dias | Estado: {estado}\n"
            f"Forca media 72h: {mean_str} N | Minimo 72h: {min_str} N | "
            f"Tendencia: {slope_str} N/dia\n"
            f"Probabilidade de impacto acumulada: {p_pct:.0f}%"
        )
        acao = (
            f"Revisar grafico de tendencia de forca (ver imagem em anexo). "
            f"Se forcas abaixo de 900 N ou tendencia negativa persistente, "
            f"antecipar inspecao do rolo. Proximo marco automatico: dia {proximo}."
        )
        return TriggerEvent(
            maquina          = maquina,
            gatilho          = self.name,
            severidade       = self.severity,
            mensagem         = mensagem,
            idade_maintacker = int(features.age_days),
            data_disparo     = ts,
            acao_recomendada = acao,
            score_atual      = round(features.p_risk, 4),
            slope_forca_7d   = round(features.slope_7d, 1),
            forca_minima_3d  = round(features.mean_3d, 1) if not np.isnan(features.mean_3d) else None,
            signal_score     = round(features.sig_score, 4),
        )

    def update_state(self, state: dict, ts: str) -> None:
        state[self.state_key]["marcos_disparados"].append(self._marco_ativo)


# ─────────────────────────────────────────────────────────────────────────────
# Motor principal
# ─────────────────────────────────────────────────────────────────────────────
class TriggerEngine:
    """
    Motor de gatilho probabilístico multi-nível — v2.3

    Gatilhos registrados (ordem de avaliação):
      OUTLIER_SINAL — leitura anômala isolada (v2.3)
      EMERGENCIA    — força crítica confirmada
      RED           — gatilho principal probabilístico
      AMARELO       — aviso precoce
      REVISAO       — marco automático de ciclo

    Uso típico:
        engine = TriggerEngine("FB14", state_path=Path("state_fb14.json"))
        events = engine.evaluate(df_hourly, troca_date, sp_client, list_name)
    """

    def __init__(self, maquina: str, state_path: Path) -> None:
        self.maquina    = maquina
        self.state_path = Path(state_path)
        self.state      = _load_state(self.state_path, maquina)
        self.state["maquina"] = maquina

        cfg = _load_trigger_config(self.state_path)

        # Parâmetros escalares ainda usados por snooze() e evaluate()
        self.weibull_beta    = float(cfg.get("weibull_beta",  WEIBULL_BETA))
        self.weibull_eta_h   = float(cfg.get("weibull_eta_h", WEIBULL_ETA_H))
        self.weibull_eta_d   = self.weibull_eta_h / 24.0
        self.boost_sinal     = float(cfg.get("boost_sinal",   BOOST_SINAL))
        self.snooze_dias     = int(cfg.get("snooze_dias",     SNOOZE_DIAS))
        # V = Eta - (d × w) onde d = Eta - mean_vida_ano_vigente.
        # 0.0 = ignorar experiência recente; 1.0 = usar só média do ano.
        self.vida_decay_w    = float(cfg.get("vida_decay_w",  0.8))

        # Gatilhos registrados — a ordem define a prioridade de avaliação
        self.triggers: List[TriggerBase] = [
            OutlierSinalTrigger(cfg),
            EmergenciaTrigger(cfg),
            RedTrigger(cfg),
            AmarelhoTrigger(cfg),
            RevisaoTrigger(cfg),
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # Ponto de entrada
    # ──────────────────────────────────────────────────────────────────────────
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
        Retorna lista de TriggerEvent disparados nesta execução.

        Args:
            media_col: Coluna de força para degradação e projeção.
                       Use "Media_norm" quando normalização por SKU estiver disponível.
                       min_3d, mediana_3d e n_leituras sempre usam "Media" bruta.
        """
        if today is None:
            today = _to_utc(df_hourly.index[-1]).normalize()
        else:
            today = _to_utc(today).normalize()
        troca_date = _to_utc(troca_date).normalize()
        age_days   = float((today - troca_date).days)

        # ── Cálculo de features ────────────────────────────────────────────────
        mean_3d    = _rolling_mean(df_hourly, media_col, today, JANELA_MEAN_3D)
        mean_14d   = _rolling_mean(df_hourly, media_col, today, JANELA_MEAN_14D)
        min_3d     = _rolling_min(df_hourly,  "Media",   today, JANELA_MEAN_3D)
        mediana_3d = _rolling_median(df_hourly, "Media", today, JANELA_MEAN_3D)
        n_abaixo   = _count_below(df_hourly, "Media", today, JANELA_MEAN_3D,
                                  FORCA_MIN_EMERGENCIA)
        slope      = _slope_7d(df_hourly, media_col, today)
        proj_48h   = _compute_proj_48h(mean_3d, slope)
        sig_score  = _compute_signal_score(mean_3d, mean_14d, slope)
        age_risk   = _weibull_age_risk(age_days, self.weibull_beta, self.weibull_eta_d)
        p_risk     = age_risk + (1.0 - age_risk) * sig_score * self.boost_sinal

        features = TriggerFeatures(
            today                = today,
            age_days             = age_days,
            mean_3d              = mean_3d,
            mean_14d             = mean_14d,
            min_3d               = min_3d,
            mediana_3d           = mediana_3d,
            n_leituras_abaixo_800= n_abaixo,
            slope_7d             = slope,
            proj_48h             = proj_48h,
            sig_score            = sig_score,
            age_risk             = age_risk,
            p_risk               = p_risk,
        )

        # ── Vida de referência ajustada pela experiência do ano vigente ──────────
        vida_ref_ajustada = (
            self._compute_vida_ref_ajustada(troca_dates)
            if troca_dates is not None else self.weibull_eta_d
        )

        # ── Avaliação dos gatilhos ─────────────────────────────────────────────
        fired: List[TriggerEvent] = []
        for trigger in self.triggers:
            if trigger.check(features, self.state):
                ev = trigger.build_event(self.maquina, features, self.state)
                trigger.update_state(self.state, ev.data_disparo)
                self._persist(ev, trigger, sp_client, list_name, vida_ref_ajustada)
                fired.append(ev)

        _save_state(self.state, self.state_path)

        if fired:
            labels = [ev.gatilho for ev in fired]
            logger.info(
                "[%s] Disparos: %s | p_risk=%.3f sig=%.3f proj=%.0f min3d=%.0f age=%dd n<800=%d",
                today.date(), "+".join(labels), p_risk, sig_score,
                proj_48h if not np.isnan(proj_48h) else -1,
                min_3d   if not np.isnan(min_3d)   else -1,
                int(age_days), n_abaixo,
            )
        return fired

    # ──────────────────────────────────────────────────────────────────────────
    # Fechamento por troca confirmada
    # ──────────────────────────────────────────────────────────────────────────
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
        logger.info("Estado completo reiniciado por troca confirmada.")

    # ──────────────────────────────────────────────────────────────────────────
    # Snooze após inspeção OK
    # ──────────────────────────────────────────────────────────────────────────
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

    # ──────────────────────────────────────────────────────────────────────────
    # Vida de referência ajustada pelo ano vigente
    # ──────────────────────────────────────────────────────────────────────────
    def _compute_vida_ref_ajustada(self, troca_dates: list) -> float:
        """
        V = Eta - (d × w)  onde  d = Eta - mean_vida_ano_vigente.

        Reduz os "dias restantes" exibidos no card quando os ciclos do ano
        vigente são mais curtos que a vida Weibull histórica, refletindo
        degradação do processo produtivo.

        Se não há ciclos completos no ano vigente, retorna weibull_eta_d
        sem ajuste (fallback conservador).
        """
        if not troca_dates or len(troca_dates) < 2:
            return self.weibull_eta_d

        ano = datetime.now().year
        sorted_dates = sorted(troca_dates)

        duracoes = []
        for i in range(1, len(sorted_dates)):
            inicio = sorted_dates[i - 1]
            fim    = sorted_dates[i]
            if _to_utc(fim).year == ano:
                dur = (_to_utc(fim) - _to_utc(inicio)).total_seconds() / 86400.0
                if dur > 0:
                    duracoes.append(dur)

        if not duracoes:
            return self.weibull_eta_d

        mean_ano = sum(duracoes) / len(duracoes)
        d = self.weibull_eta_d - mean_ano   # positivo = ciclos mais curtos que Eta
        v = self.weibull_eta_d - d * self.vida_decay_w
        return max(1.0, v)

    # ──────────────────────────────────────────────────────────────────────────
    # Persistência no SharePoint
    # ──────────────────────────────────────────────────────────────────────────
    def _persist(self, ev: TriggerEvent, trigger: TriggerBase,
                 sp_client, list_name: str, vida_ref_dias: float = 0.0) -> None:
        if sp_client is None:
            logger.warning("[dry-run] %s nao persistido (sp_client=None).", ev.gatilho)
            return

        try:
            from .card_formatter import build_alert_card
            ev.teams_payload = build_alert_card(
                maquina          = ev.maquina,
                gatilho          = ev.gatilho,
                idade_dias       = ev.idade_maintacker,
                p_risk           = ev.score_atual or 0.0,
                slope_7d         = ev.slope_forca_7d,
                forca_min_3d     = ev.forca_minima_3d,
                proj_48h         = ev.proj_48h,
                acao_recomendada = ev.acao_recomendada,
                data_disparo     = datetime.fromisoformat(ev.data_disparo[:19]),
                vida_ref_dias    = vida_ref_dias or self.weibull_eta_d,
            )
        except Exception as exc:
            logger.warning("card_formatter falhou (%s) — TeamsPayload omitido.", exc)

        # Filtra para apenas os campos que existem na lista SP atual.
        # A lista Gatilhos_Selagem usa apenas Title, Maquina e TeamsPayload;
        # os demais campos do SP_LIST_SCHEMA não foram criados na lista.
        _SP_CAMPOS_ATIVOS = {"Title", "Maquina", "TeamsPayload"}
        sp_payload = {k: v for k, v in ev.to_sp_dict().items()
                      if k in _SP_CAMPOS_ATIVOS and v is not None}
        ids = sp_client.insert_list_item(list_name, [sp_payload])
        if ids and ids[0] is not None:
            ev.sp_item_id = ids[0]
            if trigger.sp_id_key is not None:
                self.state[trigger.state_key][trigger.sp_id_key] = ids[0]
            logger.info(
                "%s inserido na lista '%s' (ID=%s).", ev.gatilho, list_name, ids[0]
            )
        else:
            logger.warning("%s inserido mas ID nao retornado.", ev.gatilho)


# ─────────────────────────────────────────────────────────────────────────────
# Funções auxiliares exportadas
# ─────────────────────────────────────────────────────────────────────────────
def compute_p_risk_snapshot(
    df: pd.DataFrame,
    troca_date: datetime,
    today: pd.Timestamp,
) -> dict:
    """Retorna snapshot completo de indicadores para `today`. Útil para diagnóstico."""
    today      = _to_utc(today).normalize()
    troca_date = _to_utc(troca_date).normalize()
    age_days   = float((today - troca_date).days)
    mean_3d    = _rolling_mean(df, "Media", today, JANELA_MEAN_3D)
    mean_14d   = _rolling_mean(df, "Media", today, JANELA_MEAN_14D)
    min_3d     = _rolling_min(df,  "Media", today, JANELA_MEAN_3D)
    mediana_3d = _rolling_median(df, "Media", today, JANELA_MEAN_3D)
    n_abaixo   = _count_below(df, "Media", today, JANELA_MEAN_3D, FORCA_MIN_EMERGENCIA)
    slope      = _slope_7d(df, "Media", today)
    proj_48h   = _compute_proj_48h(mean_3d, slope)
    sig_score  = _compute_signal_score(mean_3d, mean_14d, slope)
    age_risk   = _weibull_age_risk(age_days)
    p_risk     = age_risk + (1.0 - age_risk) * sig_score * BOOST_SINAL
    ratio_3_14 = (mean_3d / mean_14d) if (not np.isnan(mean_14d) and mean_14d > 0) else float("nan")

    return {
        "today":              today.date(),
        "age_days":           int(age_days),
        "mean_3d":            round(mean_3d,    1) if not np.isnan(mean_3d)    else None,
        "mean_14d":           round(mean_14d,   1) if not np.isnan(mean_14d)   else None,
        "min_3d":             round(min_3d,     1) if not np.isnan(min_3d)     else None,
        "mediana_3d":         round(mediana_3d, 1) if not np.isnan(mediana_3d) else None,
        "n_leituras_abaixo_800": n_abaixo,
        "ratio_3_14":         round(ratio_3_14, 4) if not np.isnan(ratio_3_14) else None,
        "slope_7d":           round(slope,    1),
        "proj_48h":           round(proj_48h, 1) if not np.isnan(proj_48h) else None,
        "age_risk":           round(age_risk,  4),
        "sig_score":          round(sig_score, 4),
        "p_risk":             round(p_risk,    4),
        "cond_p_risk":        p_risk    >= LIMIAR_P_RISK,
        "cond_signal":        sig_score >= LIMIAR_SIGNAL_SCORE,
        "cond_idade":         age_days  >= IDADE_MINIMA_DIAS,
        "cond_proj":          proj_48h  <  PROJ_48H_LIMIAR if not np.isnan(proj_48h) else False,
        "cond_emerg":         min_3d    <  FORCA_MIN_EMERGENCIA if not np.isnan(min_3d) else False,
        "cond_outlier_sinal": (
            n_abaixo <= OUTLIER_N_MAX
            and not np.isnan(mediana_3d)
            and mediana_3d > OUTLIER_MEDIANA_OK
            and not np.isnan(min_3d)
            and min_3d < FORCA_MIN_EMERGENCIA
        ),
    }
