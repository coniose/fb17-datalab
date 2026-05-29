# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Predictive maintenance system for the FB14 sealing roll (*rolo maintacker*) at a Kimberly-Clark plant. The system pulls force signals from **Seeq**, computes degradation features, runs a probabilistic trigger engine, and posts maintenance alerts to a **SharePoint** list (`Gatilhos_Selagem`) that feeds a Power Automate → Teams notification flow.

**Este repo é executado exclusivamente no ambiente Seeq Data Lab.** Não há dependências de dev local, mock seeq ou ambiente virtual separado.

## Ambiente Seeq Data Lab

- **`spy` já está autenticado** — o kernel Jupyter injeta a sessão automaticamente. Nunca chamar `spy.login()`.
- **Servidor Seeq:** `https://kcc.seeq.site` (OAuth 2.0 via `kcc.okta.com` — login por usuário/senha não suportado).
- **Operação via notebooks** — toda lógica Seeq deve rodar dentro de células de notebook ou via `nbconvert`. Scripts Python avulsos no terminal não têm sessão `spy`.
- **Credenciais SharePoint** ficam em `sharepoint.ev` (não versionado): `SP_USER=`, `SP_PASS=`. `SP_URL` é opcional — default `https://kimberlyclark.sharepoint.com/Sites/H945`.

## Executar o pipeline

```bash
# Pipeline completo de produção (agendado diariamente às 08:00)
jupyter nbconvert --to notebook --execute notebooks/pipeline_producao.ipynb

# Notebooks individuais (ordem obrigatória)
jupyter nbconvert --to notebook --execute notebooks/00_gerar_hour_prev.ipynb
jupyter nbconvert --to notebook --execute notebooks/01_vida_rul.ipynb
jupyter nbconvert --to notebook --execute notebooks/02_sinais_forca.ipynb
jupyter nbconvert --to notebook --execute notebooks/03_padroes_pretroca.ipynb
jupyter nbconvert --to notebook --execute notebooks/04_pipeline_mensageria.ipynb
```

Notebooks em ordem para refresh completo:
1. `00_gerar_hour_prev.ipynb` — pull Seeq signals, compute `target_rul` → `00_hour_prev.csv`
2. `01_vida_rul.ipynb` — Weibull fit, `score_roll7d`, RUL percentiles → `01_vida_rul.csv`
3. `02_sinais_forca.ipynb` — force signal features → `02_sinais_forca.csv`
4. `03_padroes_pretroca.ipynb` — cycle patterns → `03_padroes.csv`
5. `04_pipeline_mensageria.ipynb` — backtest trigger engine + write to SharePoint

`pipeline_producao.ipynb` orquestra os passos 1–4 mais a avaliação de gatilhos como um único job Seeq.

## Arquivos de dados (não versionados)

Os CSVs são gerados pelo pipeline e **não estão no repositório**. Após clone, executar o pipeline completo para gerá-los:

| Arquivo | Gerado por |
|---|---|
| `troca_modulo.csv` | `python extrair_troca_modulo.py --iw38 iw38.csv` (geração manual via SAP IW38) |
| `00_hour_prev.csv` | `00_gerar_hour_prev.ipynb` |
| `01_vida_rul.csv` | `01_vida_rul.ipynb` |
| `01_weibull_params.json` | `01_vida_rul.ipynb` (parâmetros Weibull calibrados) |
| `02_sinais_forca.csv` | `02_sinais_forca.ipynb` |
| `03_padroes.csv` | `03_padroes_pretroca.ipynb` |
| `state_fb14.json` | Gerado automaticamente pelo `TriggerEngine` na primeira execução (em `notebooks/`) |

## Architecture

### Data flow

```
Seeq (Forca_A, Forca_B signals)
  └─ src/generators/gen_hour_prev.py → 00_hour_prev.csv
       ├─ src/generators/gen_vida_rul.py  → 01_vida_rul.csv  (Weibull / RUL)
       ├─ src/generators/gen_sinais.py    → 02_sinais_forca.csv (force features)
       └─ src/generators/gen_padroes.py  → 03_padroes.csv (cycle patterns)
            └─ src/trigger_engine.py → TriggerEvent → SharePoint list
```

### Key source modules (`src/`)

| Module | Role |
|---|---|
| `trigger_engine.py` | Core engine (v4.0). `TriggerEngine.evaluate()` computes `p_risk` e dispara alertas via dois gatilhos OO: **RISCO** e **CRITICO** (com sub-níveis `AVISO`, `CONFIRMADO`, `FIM_DE_VIDA`) |
| `connector.py` | `load_config()` lê `config.yaml`; `pull_data()` chama `spy.pull()` com os IDs do config |
| `preprocessing.py` | `add_media()` — média de Forca_A/B; `filter_delta_outliers()` — filtro IQR no Delta_AB |
| `predictor.py` | `build_rul_target()` (assign `target_rul` from swap dates), `load_troca_dates()` |
| `sku_normalizer.py` | `normalizar_media()` — normalizes `Media` by SKU weight to remove Simpson's Paradox; produces `Media_norm`. Catálogo estático `SKU_CATALOG` + calibração automática por phantom via `calibrar_fatores_phantom()` |
| `proj_forca.py` | `adicionar_proj_48h_backtest()` — OLS regression for force projection, age-gated at ≥ 20 days |
| `sharepoint_methods.py` | `SharePointClient` — CRUD against SharePoint REST API |
| `card_formatter.py` | `build_risco_card()` e `build_critico_card()` — generate Adaptive Card JSON for Teams |
| `features.py` | `build_features()` — rolling mean, std e slope por janela (7d, 14d) para cada sinal |
| `generators/` | Pure-function generators called by `pipeline_producao.ipynb` |

### Trigger engine logic (`trigger_engine.py` v4.0)

`p_risk = age_risk + (1 − age_risk) × signal_score × boost_sinal`

- **age_risk**: Weibull CDF(age_days) — calibrated on genuine cycles (target_rul < 20 h at swap)
- **signal_score**: `deg_signal × 0.6 + slope_danger × 0.4` onde `deg_signal = 1 − mean_3d/mean_14d`

**Arquitetura OO:** dois gatilhos concretos herdam de `TriggerBase`. `TriggerEngine` itera `self.triggers: List[TriggerBase]` — adicionar um gatilho é uma linha. Features são computadas uma vez por tick e passadas via `TriggerFeatures` (dataclass).

Trigger conditions (evaluated in priority order):
| Gatilho | Sub-nível | Condição | Severidade |
|---|---|---|---|
| RISCO | — | `min_3d < 800 N` — disparo incondicional; qualquer leitura abaixo do limiar gera registro no SharePoint (cooldown 48 h para evitar spam) | INFO |
| CRITICO | FIM_DE_VIDA | `age_days ≥ eta_ajustado − 5d` (prioridade máxima, ignora snooze) | CRITICA |
| CRITICO | CONFIRMADO | `p_risk ≥ 0.48` AND `signal_score ≥ 0.22` AND `age ≥ 15d` AND `proj_48h < 800 N` por ≥ 2 dias **OU** `min_3d < 800 N` AND `p_risk ≥ 0.40` AND não é outlier isolado | ALTA |
| CRITICO | AVISO | `p_risk ≥ 0.35` OR `signal_score ≥ 0.15` (AND age ≥ 15d, AND não já CONFIRMADO) | MEDIA |

RISCO e CRITICO podem disparar simultaneamente para o mesmo evento — RISCO como registro operacional obrigatório, CRITICO como escalação de severidade. `_is_outlier` no `CriticoTrigger` (interno, usa `risco_n_max` e `risco_mediana_ok` do config) decide se o sub-800 é isolado o suficiente para não escalar para CRITICO/CONFIRMADO.

`eta_ajustado` é recalculado a cada ciclo: `Eta − (d × vida_decay_w)` onde `d = Eta_dias − mean_vida_ano_vigente`. Isso adapta o limiar de FIM_DE_VIDA ao desempenho real do ano corrente.

State is persisted per-machine in `notebooks/state_fb14.json`. Cooldowns: CONFIRMADO 48 h, AVISO 72 h, FIM_DE_VIDA 48 h, RISCO 48 h; snooze 5 days after operator OK. Public API: `evaluate()`, `close_all_by_troca()`, `snooze()`, `compute_p_risk_snapshot()`.

All trigger thresholds are overridable via `config.yaml` under the `trigger:` key.

### Notebooks de análise / desenvolvimento

Os notebooks `05_*`, `06_*`, `07_*` e `10_*` são ferramentas de pesquisa e diagnóstico, **não fazem parte do pipeline de produção**:

| Notebook | Finalidade |
|---|---|
| `05_simulador_ciclo.ipynb` | Simulação de cenários de degradação |
| `05_swap_analysis.ipynb` | Análise histórica de trocas |
| `06_modelo_estatistico.ipynb` | Experimentos de modelagem |
| `06_retrospectiva_mensagens.ipynb` | Backtest de alertas históricos |
| `06_teste_visual_cards.ipynb` | Preview visual dos Adaptive Cards |
| `07_sku_ajuste.ipynb` | Calibração de fatores SKU |
| `10_story_dashboard.ipynb` | Dashboard narrativo para stakeholders |

### Configuration

- `config.yaml` — Seeq signal IDs, Weibull parameters, all trigger thresholds (não versionado — copiar de `config.example.yaml`)
- `troca_modulo.csv` — ground truth swap dates; column `Data-base do inicio` (não versionado — gerar via `extrair_troca_modulo.py`)
- `sharepoint.ev` — credentials: `SP_USER=`, `SP_PASS=` (não versionado)
- `notebooks/state_fb14.json` — live engine state (last fired timestamps, snooze window, proj_window) (não versionado — gerado automaticamente com migração automática de versões anteriores)

### SharePoint — lista `Gatilhos_Selagem`

A lista SP em produção tem **apenas 3 campos ativos**:

| Campo interno | Tipo | Conteúdo |
|---|---|---|
| `Title` | Texto | `"FB14 \| GATILHO \| YYYY-MM-DD"` |
| `Maquina` | Texto | `"FB14"` |
| `TeamsPayload` | Texto longo | JSON do Adaptive Card (gerado por `card_formatter.py`) |

Os demais campos do `SP_LIST_SCHEMA` no código (`Gatilho`, `Severidade`, `Mensagem`, etc.) **não existem na lista**. O método `_persist()` filtra o payload para `_SP_CAMPOS_ATIVOS = {"Title", "Maquina", "TeamsPayload"}` antes de inserir. Não adicionar esses campos ao `to_sp_dict()` sem primeiro criá-los na lista SP.

O Power Automate monitora novos itens na lista e posta o `TeamsPayload` (Adaptive Card) no canal do Teams.

### SKU normalization

When `Media_norm` coverage ≥ 50%, the trigger engine uses `media_col="Media_norm"` for degradation scoring. The RISCO and CRITICO/CONFIRMADO (força crítica path) gates always use raw `Media` regardless — they are absolute force thresholds, not relative ones. `sku_normalizer.py` suporta dois modos: catálogo estático `SKU_CATALOG` (por código de produto) e calibração automática por phantom code via `normalizar_media_phantom()`.
