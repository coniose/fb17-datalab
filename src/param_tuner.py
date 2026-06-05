"""
param_tuner.py — Calibração segura de parâmetros de disparo por máquina.

Roda o TriggerEngine em modo dry-run (sp_client=None) contra dados históricos
e avalia cada configuração de parâmetros contra eventos de retenção conhecidos
(ground truth).

Métricas principais:
    advance_days — quantos dias antes da retenção o primeiro alerta disparou.
    score        — cobertura ponderada por peso × antecedência − penalidade FP.

Pesos por tipo_crucial (coluna no CSV de retenções):
    confirmado  → 2.0  (custo alto; degradação óbvia que gerou retenção)
    aviso       → 1.5  (aproximação fim-de-vida com degradação notória)
    risco       → 1.0  (base, escalada por qtd_retida / max_qtd)
    fim_de_vida → 1.0  (previsível pelo Weibull)
    outro       → 1.0

Uso rápido:
    from src.param_tuner import ParamTuner, DEFAULT_GRID
    tuner = ParamTuner.from_files()
    resultados = tuner.grid_search(DEFAULT_GRID)
    print(resultados.head(10))

Uso personalizado:
    import pandas as pd
    from src.param_tuner import ParamTuner

    tuner = ParamTuner(
        df_hourly     = pd.read_csv("notebooks/00_hour_prev.csv", parse_dates=["Timestamp"]),
        troca_dates   = [...],          # list[pd.Timestamp]
        retention_events = [            # ground truth
            {"data": "2025-05-21", "descricao": "Selagem Ruim", "qtd_retida": 2100,
             "tipo_crucial": "confirmado"},
        ],
        config_path   = "config.yaml",
        media_col     = "Media",
        janela_pre_evento = 21,         # dias antes do evento que contam como TP
    )
    df_result = tuner.grid_search(PARAM_GRID)
"""
from __future__ import annotations

import copy
import itertools
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Multiplicadores por tipo_crucial
_TIPO_PESO: dict[str, float] = {
    "confirmado":  2.0,
    "aviso":       1.5,
    "risco":       1.0,
    "fim_de_vida": 1.0,
    "outro":       1.0,
}

# ── Grid padrão de parâmetros a testar ────────────────────────────────────────
DEFAULT_GRID: dict[str, list] = {
    "limiar_p_risk":        [0.35, 0.40, 0.45, 0.48, 0.55],
    "limiar_signal_score":  [0.15, 0.20, 0.22, 0.28],
    "risco_forca_limiar":   [750, 800, 850],
    "proj_48h_limiar":      [750, 800, 850],
}


class ParamTuner:
    """
    Avalia configurações de parâmetros do TriggerEngine contra eventos de
    retenção conhecidos. Totalmente isolado de produção (dry-run).
    """

    def __init__(
        self,
        df_hourly: pd.DataFrame,
        troca_dates: list,
        retention_events: list[dict],
        config_path: str | Path | None = None,
        media_col: str = "Media",
        janela_pre_evento: int = 21,
        df_delay: "pd.DataFrame | None" = None,
    ) -> None:
        """
        df_hourly         — DataFrame com coluna Timestamp (index ou coluna) + Media
        troca_dates       — list[pd.Timestamp] das trocas confirmadas (UTC)
        retention_events  — list[dict] com chaves: data (str), descricao, qtd_retida,
                            tipo_crucial (opcional: risco|aviso|confirmado|fim_de_vida|outro)
        config_path       — caminho para config.yaml (lê parâmetros base)
        media_col         — coluna de força a usar ('Media' ou 'Media_norm')
        janela_pre_evento — dias antes de um evento que um alerta é considerado TP
        df_delay          — anomaly_delay.csv para corrigir horas paradas no Weibull
        """
        self._config_path = Path(config_path or ROOT / "config.yaml")
        self._base_cfg    = self._load_config()
        self._maquina     = self._base_cfg["project"]["maquina"]
        self._media_col   = media_col
        self._janela      = janela_pre_evento
        self._df_delay    = df_delay

        # Normalizar df_hourly
        df = df_hourly.copy()
        if "Timestamp" in df.columns:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True)
            df = df.set_index("Timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
        self._df = df.sort_index()

        # Normalizar troca_dates
        self._troca_dates = [
            t if t.tzinfo else t.replace(tzinfo=pd.Timestamp.now("UTC").tzinfo)
            for t in troca_dates
        ]

        # Normalizar eventos de retenção + calcular pesos
        events_norm = [
            {**ev, "ts": pd.Timestamp(ev["data"], tz="UTC")}
            for ev in retention_events
            if not str(ev.get("data", "")).startswith("#")  # ignora linhas-comentário
        ]
        self._retention_events = self._assign_weights(events_norm)

    # ── Fábrica conveniente ───────────────────────────────────────────────────

    @classmethod
    def from_files(
        cls,
        hourly_csv:        str | Path | None = None,
        troca_csv:         str | Path | None = None,
        retention_csv:     str | Path | None = None,
        delay_csv:         str | Path | None = None,
        config_path:       str | Path | None = None,
        media_col:         str = "Media",
        janela_pre_evento: int = 21,
    ) -> "ParamTuner":
        """
        Carrega os arquivos padrão e retorna um ParamTuner pronto.

        Defaults:
            hourly_csv    → notebooks/00_hour_prev.csv
            troca_csv     → notebooks/troca_modulo.csv
            retention_csv → rca/retencoes_<maquina>_selagem.csv
            delay_csv     → notebooks/anomaly_delay.csv (opcional — corrige horas paradas)
        """
        from src.predictor import load_troca_dates

        hourly_path = Path(hourly_csv or ROOT / "notebooks" / "00_hour_prev.csv")
        troca_path  = Path(troca_csv  or ROOT / "notebooks" / "troca_modulo.csv")

        # auto-detect retention CSV se não fornecido
        if retention_csv is None:
            cfg_path = Path(config_path or ROOT / "config.yaml")
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            maquina = cfg["project"]["maquina"].lower()
            retention_csv = ROOT / "rca" / f"retencoes_{maquina}_selagem.csv"
        retention_path = Path(retention_csv)

        df_hourly = pd.read_csv(hourly_path, parse_dates=["Timestamp"])
        troca_dates = load_troca_dates(troca_path)
        retention_events = pd.read_csv(retention_path, comment="#").to_dict("records")

        # Carrega anomaly_delay se disponível
        df_delay = None
        delay_path = Path(delay_csv or ROOT / "notebooks" / "anomaly_delay.csv")
        if delay_path.exists():
            df_delay = pd.read_csv(delay_path, index_col="Timestamp", parse_dates=True)
            df_delay.index = pd.to_datetime(df_delay.index, utc=True)

        return cls(
            df_hourly=df_hourly,
            troca_dates=troca_dates,
            retention_events=retention_events,
            config_path=config_path,
            media_col=media_col,
            janela_pre_evento=janela_pre_evento,
            df_delay=df_delay,
        )

    # ── API pública ───────────────────────────────────────────────────────────

    def backtest(self, params: dict[str, Any]) -> dict:
        """
        Roda o TriggerEngine em dry-run com os parâmetros dados e retorna métricas.

        Itera ciclo a ciclo (reset de estado entre trocas) e usa a troca ativa
        correta para cada dia — necessário para calcular age_risk com precisão.
        Passa df_delay quando disponível para corrigir horas paradas no Weibull.

        Returns dict com:
            n_disparos          — total de alertas disparados no histórico
            true_positives      — retenções cobertas por alerta prévio
            false_positives     — alertas sem retenção associada
            coverage_pct        — % de eventos cobertos (não ponderada)
            weighted_score      — score ponderado por tipo_crucial
            advance_days_mean   — antecedência média (dias) dos TPs
            advance_days_min    — antecedência mínima dos TPs
            score               — alias de weighted_score (para ordenação)
            detalhes            — list[dict] por evento de retenção
        """
        from src.trigger_engine import TriggerEngine

        cfg       = self._patch_config(params)
        alertas:  list[pd.Timestamp] = []

        # Itera cada ciclo completo (troca[i] → troca[i+1])
        for i, (t_ini, t_fim) in enumerate(zip(self._troca_dates[:-1], self._troca_dates[1:])):
            ciclo_df = self._df[(self._df.index >= t_ini) & (self._df.index < t_fim)]
            if ciclo_df.empty:
                continue

            state_tmp = Path(tempfile.mktemp(suffix=".json"))
            try:
                engine = TriggerEngine(self._maquina, state_tmp, config=cfg)
                dias   = pd.DatetimeIndex(sorted({ts.normalize() for ts in ciclo_df.index}))

                for day in dias:
                    df_ate = self._df[self._df.index <= day + pd.Timedelta(hours=23, minutes=59)]
                    try:
                        evs = engine.evaluate(
                            df_hourly   = df_ate,
                            troca_date  = t_ini.to_pydatetime(),
                            sp_client   = None,
                            today       = day,
                            troca_dates = self._troca_dates,
                            media_col   = self._media_col,
                            df_delay    = self._df_delay,
                        )
                        for _ in evs:
                            alertas.append(day)
                    except Exception:
                        pass
            finally:
                if state_tmp.exists():
                    state_tmp.unlink()

        # Ciclo atual (última troca → hoje) com estado limpo
        t_ini_atual = self._troca_dates[-1]
        ciclo_atual = self._df[self._df.index >= t_ini_atual]
        if not ciclo_atual.empty:
            state_tmp = Path(tempfile.mktemp(suffix=".json"))
            try:
                engine = TriggerEngine(self._maquina, state_tmp, config=cfg)
                dias   = pd.DatetimeIndex(sorted({ts.normalize() for ts in ciclo_atual.index}))
                for day in dias:
                    df_ate = self._df[self._df.index <= day + pd.Timedelta(hours=23, minutes=59)]
                    try:
                        evs = engine.evaluate(
                            df_hourly   = df_ate,
                            troca_date  = t_ini_atual.to_pydatetime(),
                            sp_client   = None,
                            today       = day,
                            troca_dates = self._troca_dates,
                            media_col   = self._media_col,
                            df_delay    = self._df_delay,
                        )
                        for _ in evs:
                            alertas.append(day)
                    except Exception:
                        pass
            finally:
                if state_tmp.exists():
                    state_tmp.unlink()

        return self._score(alertas, params)

    def grid_search(self, param_grid: dict[str, list] | None = None) -> pd.DataFrame:
        """Testa todas as combinações do param_grid e retorna DataFrame ranqueado."""
        grid = param_grid or DEFAULT_GRID
        keys   = list(grid.keys())
        combos = list(itertools.product(*[grid[k] for k in keys]))
        total  = len(combos)
        print(f"[param_tuner] {total} combinações × {len(self._retention_events)} eventos de retenção")

        rows = []
        for i, combo in enumerate(combos, 1):
            params = dict(zip(keys, combo))
            try:
                m = self.backtest(params)
                rows.append({**params, **m})
            except Exception as e:
                rows.append({**params, "erro": str(e)})
            if i % max(1, total // 10) == 0:
                print(f"  {i}/{total} ({100*i//total}%)")

        df = pd.DataFrame(rows)
        if "score" in df.columns:
            df = df.sort_values("score", ascending=False).reset_index(drop=True)
        return df

    def top_configs(self, n: int = 5, param_grid: dict | None = None) -> pd.DataFrame:
        """Atalho: roda grid_search e retorna top-N configurações."""
        return self.grid_search(param_grid).head(n)

    # ── Internos ──────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        with open(self._config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _patch_config(self, params: dict) -> dict:
        """Retorna cópia do config com os parâmetros de teste aplicados."""
        cfg = copy.deepcopy(self._base_cfg)
        for k, v in params.items():
            if k in cfg.get("trigger", {}):
                cfg["trigger"][k] = v
        return cfg

    @staticmethod
    def _assign_weights(events: list[dict]) -> list[dict]:
        """
        Calcula peso final de cada evento:
          peso = tipo_mult × (qtd_retida / max_qtd)   se qtd_retida disponível
          peso = tipo_mult                             caso contrário
        """
        qtds = [float(ev["qtd_retida"]) for ev in events
                if str(ev.get("qtd_retida", "")).replace(".", "").isdigit()]
        max_qtd = max(qtds) if qtds else 1.0

        result = []
        for ev in events:
            tipo = str(ev.get("tipo_crucial", "outro")).lower().strip()
            tipo_mult = _TIPO_PESO.get(tipo, 1.0)

            try:
                qtd_norm = float(ev["qtd_retida"]) / max_qtd
            except (KeyError, TypeError, ValueError):
                qtd_norm = 1.0

            result.append({**ev, "_peso": round(tipo_mult * qtd_norm, 4)})
        return result

    def _score(self, alertas: list[pd.Timestamp], params: dict) -> dict:
        """Cruza alertas com eventos de retenção e calcula métricas ponderadas."""
        detalhes: list[dict] = []
        tp_advances: list[float] = []
        tp_pesos:    list[float] = []
        alertas_usados: set[int] = set()
        soma_pesos = sum(ev["_peso"] for ev in self._retention_events) or 1.0

        for ev in self._retention_events:
            ts_ev      = ev["ts"]
            janela_ini = ts_ev - pd.Timedelta(days=self._janela)
            peso       = ev["_peso"]

            pre = [
                (i, a) for i, a in enumerate(alertas)
                if janela_ini <= a < ts_ev and i not in alertas_usados
            ]
            if pre:
                idx_alerta, primeiro_alerta = pre[0]
                advance = (ts_ev - primeiro_alerta).days
                tp_advances.append(float(advance))
                tp_pesos.append(peso)
                alertas_usados.add(idx_alerta)
                detalhes.append({
                    "evento":          ev["data"],
                    "descricao":       ev.get("descricao", ""),
                    "qtd":             ev.get("qtd_retida", "?"),
                    "tipo_crucial":    ev.get("tipo_crucial", "outro"),
                    "peso":            round(peso, 3),
                    "coberto":         True,
                    "advance_d":       advance,
                    "primeiro_alerta": primeiro_alerta.date().isoformat(),
                })
            else:
                detalhes.append({
                    "evento":          ev["data"],
                    "descricao":       ev.get("descricao", ""),
                    "qtd":             ev.get("qtd_retida", "?"),
                    "tipo_crucial":    ev.get("tipo_crucial", "outro"),
                    "peso":            round(peso, 3),
                    "coberto":         False,
                    "advance_d":       None,
                    "primeiro_alerta": None,
                })

        n_ev    = len(self._retention_events)
        n_tp    = len(tp_advances)
        n_fp    = len(alertas) - len(alertas_usados)
        adv_mean = sum(tp_advances) / len(tp_advances) if tp_advances else 0.0
        adv_min  = min(tp_advances) if tp_advances else 0.0
        coverage = n_tp / n_ev if n_ev else 0.0

        # Score ponderado: Σ(peso_i × advance_factor_i) / Σ(pesos) − penalidade FP
        weighted_num = sum(
            p * min(adv / 14.0, 1.0)
            for p, adv in zip(tp_pesos, tp_advances)
        )
        fp_penalty     = min(n_fp * 0.02, 0.30)
        weighted_score = weighted_num / soma_pesos - fp_penalty

        return {
            "n_disparos":        len(alertas),
            "true_positives":    n_tp,
            "false_positives":   n_fp,
            "coverage_pct":      round(coverage * 100, 1),
            "advance_days_mean": round(adv_mean, 1),
            "advance_days_min":  round(adv_min, 1),
            "score":             round(weighted_score, 4),
            "detalhes":          detalhes,
        }
