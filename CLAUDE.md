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
| `02_sinais_forca.csv` | `02_sinais_forca.ipynb` |
| `03_padroes.csv` | `03_padroes_pretroca.ipynb` |
| `state_fb14.json` | Gerado automaticamente pelo `TriggerEngine` na primeira execução |

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
| `trigger_engine.py` | Core engine (v2.3). `TriggerEngine.evaluate()` computes `p_risk` e dispara alertas multi-nível via arquitetura OO: **OUTLIER_SINAL → EMERGENCIA → RED → AMARELO → REVISAO** |
| `predictor.py` | `build_rul_target()` (assign `target_rul` from swap dates), `load_troca_dates()` |
| `sku_normalizer.py` | `normalizar_media()` — normalizes `Media` by SKU weight to remove Simpson's Paradox; produces `Media_norm` |
| `proj_forca.py` | `adicionar_proj_48h_backtest()` — OLS regression for force projection, age-gated at ≥ 20 days |
| `sharepoint_methods.py` | `SharePointClient` — CRUD against SharePoint REST API |
| `card_formatter.py` | `build_alert_card()` — generates Adaptive Card JSON for Teams |
| `generators/` | Pure-function generators called by `pipeline_producao.ipynb` |

### Trigger engine logic (`trigger_engine.py` v2.3)

`p_risk = age_risk + (1 − age_risk) × signal_score × boost_sinal`

- **age_risk**: Weibull CDF(age_days) — calibrated on genuine cycles (target_rul < 20 h at swap)
- **signal_score**: degradation from `mean_3d / mean_14d` ratio + slope trend

**Arquitetura OO:** cada gatilho é uma subclasse de `TriggerBase` com `check()`, `build_event()` e `update_state()`. `TriggerEngine` itera `self.triggers: List[TriggerBase]` — adicionar ou remover um gatilho é uma linha. Features são computadas uma vez por tick e passadas via `TriggerFeatures` (dataclass).

Trigger conditions (evaluated in priority order):
| Trigger | Condition | Severity |
|---|---|---|
| OUTLIER_SINAL | `min_3d < 800 N` AND `n_leituras_abaixo ≤ 1` AND `mediana_3d > 950 N` — leitura isolada que se recupera | INFO |
| EMERGENCIA | `min_3d < 800 N` AND NOT condição de outlier acima (força crítica confirmada) | CRITICA |
| RED | C1: `p_risk ≥ 0.48` AND C2: `signal_score ≥ 0.22` AND C3: `age ≥ 15d` AND C4: `proj_48h < 800 N` por ≥ 2 dias | ALTA |
| AMARELO | `p_risk ≥ 0.35` OR `signal_score ≥ 0.15` (aviso precoce) | MEDIA |
| REVISAO | Marco automático nos dias 20, 25, 35 do ciclo | INFO |

OUTLIER_SINAL e EMERGENCIA são **mutuamente exclusivos** por condição — nunca disparam juntos.

Pesquisa histórica (26 ciclos): 81 % dos eventos `Media < 800 N` são transientes — o sinal recupera e o time continuou operando (mediana de 34 dias até a próxima troca). Classificar todos como EMERGENCIA gera fadiga de alerta.

State is persisted per-machine in `state_fb14.json`. Cooldowns: RED 48 h, AMARELO 72 h, EMERGENCIA 48 h, OUTLIER_SINAL 48 h; snooze 5 days after operator OK.

All trigger thresholds are overridable via `config.yaml` under the `trigger:` key.

### Configuration

- `config.yaml` — Seeq signal IDs, Weibull parameters, all trigger thresholds (não versionado — copiar de `config.example.yaml`)
- `troca_modulo.csv` — ground truth swap dates; column `Data-base do inicio` (não versionado — gerar via `extrair_troca_modulo.py`)
- `sharepoint.ev` — credentials: `SP_USER=`, `SP_PASS=` (não versionado)
- `state_fb14.json` — live engine state (last fired timestamps, snooze window, proj_window) (não versionado — gerado automaticamente)

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

When `Media_norm` coverage ≥ 50%, the trigger engine uses `media_col="Media_norm"` for degradation scoring. The EMERGENCIA and OUTLIER_SINAL gates always use raw `Media` regardless — they are absolute force thresholds, not relative ones.
