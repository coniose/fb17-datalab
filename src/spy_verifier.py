"""
spy_verifier.py — Loop de auto-correção de conexões PI para notebooks fb17-datalab.

Importar em qualquer notebook para verificar IDs e padrões de sinal antes de uso
em código de produção. Fora do Seeq Data Lab retorna resultados mock (sem erro).

Uso rápido:
    from src.spy_verifier import SpyVerifier
    v = SpyVerifier()
    v.verify_ids({"Forca_A": "951157FA-...", "Forca_B": "8D9E2FE1-..."})
    v.search_and_sample({"delay": ["*FB17*Delay*"]}, sample_days=1)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

# ── Constantes padrão do projeto ──────────────────────────────────────────────
DEFAULT_DATASOURCE = "BRSZA020"
DEFAULT_GRID       = "1h"
DEFAULT_SAMPLE_H   = 24      # horas de amostra para verificar granularidade

# ── Estruturas de resultado ───────────────────────────────────────────────────

@dataclass
class SignalCheck:
    name: str
    id_given: str
    found: bool
    id_actual: str | None = None
    signal_name_actual: str | None = None
    datasource: str | None = None
    unit: str | None = None
    note: str = ""

    def status_icon(self) -> str:
        return "✅" if self.found else "❌"

    def __repr__(self) -> str:
        if self.found:
            return (f"{self.status_icon()} {self.name}: '{self.signal_name_actual}' "
                    f"| datasource={self.datasource} | unit={self.unit}")
        return f"{self.status_icon()} {self.name}: ID '{self.id_given}' não encontrado. {self.note}"


@dataclass
class SearchResult:
    pattern: str
    grupo: str
    signals: list[dict] = field(default_factory=list)
    sample_stats: dict = field(default_factory=dict)

    def best_match(self) -> dict | None:
        """Retorna o sinal com mais leituras no período de amostra."""
        if not self.signals:
            return None
        return max(self.signals, key=lambda s: s.get("n_leituras_sample", 0))

    def to_row(self) -> dict:
        bm = self.best_match()
        return {
            "grupo": self.grupo,
            "pattern": self.pattern,
            "n_encontrados": len(self.signals),
            "best_name": bm["name"] if bm else "—",
            "best_id": bm["ID"] if bm else "—",
            "best_unit": bm.get("unit", "—") if bm else "—",
            "gran_mediana_s": bm.get("gran_mediana_s") if bm else None,
            "n_leituras_sample": bm.get("n_leituras_sample", 0) if bm else 0,
        }


@dataclass
class VerificationReport:
    checks: list[SignalCheck] = field(default_factory=list)
    search_results: list[SearchResult] = field(default_factory=list)

    def all_ok(self) -> bool:
        return all(c.found for c in self.checks)

    def summary(self) -> str:
        lines = ["=== SpyVerifier — Relatório ==="]
        if self.checks:
            lines.append("\n[Verificação de IDs]")
            for c in self.checks:
                lines.append(f"  {c}")
        if self.search_results:
            lines.append("\n[Busca por Padrão]")
            rows = [r.to_row() for r in self.search_results]
            df = pd.DataFrame(rows)
            lines.append(df.to_string(index=False))
        if not self.all_ok():
            lines.append("\n⚠️  IDs com problema — não avançar para produção sem corrigir.")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = ["## SpyVerifier Report\n"]
        if self.checks:
            lines.append("### IDs verificados\n")
            lines.append("| Signal | Status | Nome real | Datasource |")
            lines.append("|--------|--------|-----------|------------|")
            for c in self.checks:
                lines.append(f"| {c.name} | {c.status_icon()} | "
                              f"{c.signal_name_actual or '—'} | {c.datasource or '—'} |")
        if self.search_results:
            lines.append("\n### Busca por padrão\n")
            rows = [r.to_row() for r in self.search_results]
            df = pd.DataFrame(rows)
            lines.append(df.to_markdown(index=False))
        return "\n".join(lines)


# ── SpyVerifier ───────────────────────────────────────────────────────────────

class SpyVerifier:
    """
    Verifica conexões PI point e busca novos sinais por padrão.

    Fora do Seeq Data Lab (sem spy disponível) opera em modo degradado:
    - verify_ids() retorna ❌ para todos com nota "spy indisponível"
    - search_and_sample() retorna lista vazia com aviso
    """

    def __init__(self) -> None:
        self._spy: Any = None
        self._spy_available = self._try_import_spy()

    def _try_import_spy(self) -> bool:
        try:
            from seeq import spy as _spy  # noqa: PLC0415
            self._spy = _spy
            return True
        except ImportError:
            warnings.warn(
                "seeq.spy não disponível — SpyVerifier em modo degradado (sem Seeq Data Lab).",
                stacklevel=2,
            )
            return False

    # ── Verificação de IDs ────────────────────────────────────────────────────

    def verify_ids(self, signal_ids: dict[str, str]) -> VerificationReport:
        """
        Verifica se cada ID em signal_ids ainda existe no Seeq.

        Args:
            signal_ids: dict {nome_legivel: seeq_id}  ex: {"Forca_A": "951157FA-..."}

        Returns:
            VerificationReport com status de cada sinal.

        Exemplo:
            from src.spy_verifier import SpyVerifier
            v = SpyVerifier()
            report = v.verify_ids({"Forca_A": "951157FA-...", "Forca_B": "8D9E2FE1-..."})
            print(report.summary())
            assert report.all_ok(), "Sinais não encontrados — revisar IDs no config.yaml"
        """
        checks = []

        if not self._spy_available:
            for name, sid in signal_ids.items():
                checks.append(SignalCheck(
                    name=name, id_given=sid, found=False,
                    note="spy indisponível — executar no Seeq Data Lab"
                ))
            return VerificationReport(checks=checks)

        for name, sid in signal_ids.items():
            try:
                items_df = pd.DataFrame([{"ID": sid, "Type": "Signal"}])
                result = self._spy.search(items_df, quiet=True)
                if result is not None and not result.empty:
                    row = result.iloc[0]
                    checks.append(SignalCheck(
                        name=name,
                        id_given=sid,
                        found=True,
                        id_actual=str(row.get("ID", sid)),
                        signal_name_actual=str(row.get("Name", "")),
                        datasource=str(row.get("Datasource Name", "")),
                        unit=str(row.get("Value Unit Of Measure", "")),
                    ))
                else:
                    # Tentar busca por ID como string no campo Name (fallback)
                    checks.append(SignalCheck(
                        name=name, id_given=sid, found=False,
                        note=f"ID não encontrado via spy.search. "
                             f"Verificar datasource={DEFAULT_DATASOURCE}."
                    ))
            except Exception as e:
                checks.append(SignalCheck(
                    name=name, id_given=sid, found=False,
                    note=f"Erro ao buscar: {e}"
                ))

        report = VerificationReport(checks=checks)
        print(report.summary())
        return report

    # ── Busca por padrão + amostra ────────────────────────────────────────────

    def search_and_sample(
        self,
        patterns: dict[str, list[str]],
        sample_days: int = 1,
        datasource_filter: str | None = DEFAULT_DATASOURCE,
    ) -> VerificationReport:
        """
        Busca sinais por padrão wildcard e amostra dados para medir granularidade.

        Args:
            patterns: dict {grupo: [pattern1, pattern2, ...]}
                      ex: {"delay": ["*FB17*Delay*", "*FB17*Tempo Delay*"]}
            sample_days: quantos dias de dados amostrar para medir granularidade
            datasource_filter: filtrar resultados por datasource (None = sem filtro)

        Returns:
            VerificationReport com SearchResult por padrão encontrado.

        Exemplo:
            v = SpyVerifier()
            report = v.search_and_sample({"delay": ["*FB17*Delay*"]}, sample_days=1)
            best = report.search_results[0].best_match()
            print(f"Sinal encontrado: {best['name']} | ID: {best['ID']}")
        """
        if not self._spy_available:
            warnings.warn("spy indisponível — search_and_sample retorna vazio.", stacklevel=2)
            return VerificationReport()

        now = datetime.now(tz=timezone.utc)
        start_sample = (now - timedelta(days=sample_days)).isoformat()
        end_sample   = now.isoformat()

        search_results: list[SearchResult] = []

        for grupo, pats in patterns.items():
            for pat in pats:
                try:
                    df_s = self._spy.search({"Name": pat}, quiet=True)
                    if df_s is None or df_s.empty:
                        continue

                    # Filtrar datasource se especificado
                    if datasource_filter and "Datasource Name" in df_s.columns:
                        df_s = df_s[
                            df_s["Datasource Name"].str.contains(
                                datasource_filter, case=False, na=False
                            )
                        ]

                    if df_s.empty:
                        continue

                    signals_info = []
                    for _, row in df_s.iterrows():
                        sig = {
                            "name": row.get("Name", ""),
                            "ID": str(row.get("ID", "")),
                            "unit": row.get("Value Unit Of Measure", ""),
                            "datasource": row.get("Datasource Name", ""),
                            "gran_mediana_s": None,
                            "n_leituras_sample": 0,
                        }
                        # Amostrar granularidade
                        try:
                            items_df = pd.DataFrame([{"ID": sig["ID"], "Type": "Signal"}])
                            amostra = self._spy.pull(
                                items_df,
                                start=start_sample,
                                end=end_sample,
                                grid=None,
                                quiet=True,
                            )
                            if amostra is not None and not amostra.empty:
                                intervalos = (
                                    amostra.index.to_series()
                                    .diff()
                                    .dt.total_seconds()
                                    .dropna()
                                )
                                sig["gran_mediana_s"] = (
                                    round(intervalos.median(), 1)
                                    if not intervalos.empty else None
                                )
                                sig["n_leituras_sample"] = len(amostra)
                        except Exception:
                            pass  # amostra falha silenciosamente — sinal ainda listado

                        signals_info.append(sig)

                    if signals_info:
                        search_results.append(SearchResult(
                            pattern=pat,
                            grupo=grupo,
                            signals=signals_info,
                        ))

                except Exception as e:
                    warnings.warn(f"[{grupo}] '{pat}': {e}", stacklevel=2)

        report = VerificationReport(search_results=search_results)
        print(report.summary())
        return report

    # ── Convenience: verificar config.yaml ───────────────────────────────────

    def verify_config(self, config_path: str = "config.yaml") -> VerificationReport:
        """
        Lê o config.yaml do projeto e verifica todos os IDs em signal_ids.

        Args:
            config_path: caminho para config.yaml (relativo ao CWD)

        Uso:
            v = SpyVerifier()
            v.verify_config()  # lê notebooks/config.yaml e verifica IDs
        """
        try:
            import yaml  # noqa: PLC0415
        except ImportError:
            warnings.warn("PyYAML não instalado — instale com: pip install pyyaml", stacklevel=2)
            return VerificationReport()

        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
        except FileNotFoundError:
            warnings.warn(f"config.yaml não encontrado em '{config_path}'", stacklevel=2)
            return VerificationReport()

        signal_ids: dict[str, str] = {}

        # Suporte a estruturas comuns no config.yaml do fb17-datalab
        for key in ("signal_ids", "SIGNAL_IDS", "sinais", "signals"):
            if key in cfg and isinstance(cfg[key], dict):
                signal_ids.update(cfg[key])

        if not signal_ids:
            warnings.warn(
                f"Nenhuma chave 'signal_ids' encontrada em '{config_path}'. "
                "Verificar estrutura do arquivo.",
                stacklevel=2,
            )
            return VerificationReport()

        print(f"Verificando {len(signal_ids)} sinais do {config_path}...")
        return self.verify_ids(signal_ids)
