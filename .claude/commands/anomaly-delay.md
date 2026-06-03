# /anomaly-delay

Skill para rodar, interpretar e adaptar a análise de **correção de age_risk por parada de máquina**
e **ponderação de anomalias por métricas de qualidade operacional** (delay, waste, fault).

Notebook: `notebooks/anomaly_delay.ipynb`

---

## O que esta skill faz

1. **Corrige `age_risk_corrected`** — quando a FB14 está parada (`Tempo Delay > 0`), as peças
   mecânicas não acumulam desgaste. A correção desconta as horas paradas do calendário do ciclo
   antes de calcular o CDF Weibull.

2. **Calcula `quality_weight`** — para cada cartão amarelo (`p_risk > critico_p_risk_min` OU
   `min_forca_3d < risco_forca_limiar`), pondera o evento por delay coincidente, waste e presença
   de código de falha. Anomalias durante paradas têm diagnóstico diferente de anomalias em produção plena.

**Sinal chave no Seeq:**
`BRSZ FB14 Quality - FB14_B1_Tempo Delay Calculad`
Workbook: `0F116FED-4C4F-FD60-9EA0-0CA92EE4B765` — sheet FB14

---

## Pré-requisitos

```
[ ] Seeq Data Lab com sessão spy autenticada
[ ] notebooks/02_sinais_forca.csv presente (gerado por 02_sinais_forca.ipynb)
[ ] notebooks/01_weibull_params.json presente (gerado por 01_vida_rul.ipynb)
[ ] config.yaml presente na raiz (para ler critico_p_risk_min e risco_forca_limiar)
```

---

## Como usar esta skill

Quando o usuário invocar `/anomaly-delay`, pergunte:

1. Qual o objetivo? (rodar análise completa / interpretar resultado existente / adaptar para outra máquina)
2. O `anomaly_delay.csv` já existe em `notebooks/`? (se sim, pode pular execução)

Então execute o workflow adequado abaixo.

---

## Workflow 1 — Executar a análise completa

```bash
# Rodar dentro de notebooks/ no terminal do Seeq Data Lab
cd notebooks
jupyter nbconvert --to notebook --execute anomaly_delay.ipynb \
    --output anomaly_delay.ipynb \
    --ExecutePreprocessor.timeout=600
```

**O que verificar célula a célula:**

| Célula | Sinal de sucesso |
|---|---|
| Cell 2 — spy.search | `delay: N sinal(is)` — deve encontrar `BRSZ FB14 Quality - FB14_B1_Tempo Delay Calculad` |
| Cell 3 — pull delay | `delay_min > 0: X horas paradas` — se X = 0 em 4 anos, o sinal está vazio |
| Cell 4 — pull quality | `waste: N leituras`, `fault: N leituras` (erros são avisos, não bloqueantes) |
| Cell 5 — load força | `Ciclos únicos: N` — deve bater com `troca_modulo.csv` |
| Cell 6 — age_effective | `Delta age_risk (original − corrigido): mean > 0` — confirma que paradas foram descontadas |
| Cell 7 — quality_weight | `Cartões amarelos: N` — deve ter ao menos alguns por ciclo |
| Cell 8 — export | `Exportado: anomaly_delay.csv  (N linhas)` |

---

## Workflow 2 — Interpretar anomaly_delay.csv

```python
import pandas as pd
df = pd.read_csv('notebooks/anomaly_delay.csv', parse_dates=['Timestamp'])

# 1. Impacto médio da correção de age_risk
print(df[['age_risk_original', 'age_risk_corrected']].describe())

# 2. Ciclos com mais paradas acumuladas
print(df.groupby('ciclo_id')['stopped_h'].max().sort_values(ascending=False).head(5))

# 3. Cartões amarelos com maior quality_weight
amarelos = df[df['is_yellow_card']].sort_values('quality_weight', ascending=False)
print(amarelos[['Timestamp', 'ciclo_id', 'age_risk_original', 'age_risk_corrected',
               'delay_weight', 'quality_weight']].head(10))
```

**O que faz diferença:**

- `age_risk_corrected < age_risk_original` — ciclos onde a máquina parou; o AVISO/CRITICO
  dispararia mais tarde com a correção (rolo não estava realmente envelhecendo).

- `quality_weight ≈ 0` — cartão amarelo durante produção normal sem waste ou falha →
  evento de força suspeito, merece investigação isolada.

- `quality_weight > 0.5` — cartão amarelo coincidiu com parada pesada OU alto waste →
  evento operacional, não necessariamente degradação do rolo.

---

## Workflow 3 — Adaptar para nova máquina (FB16, FB17...)

1. Alterar `WORKBOOK_ID` no Cell 1 do notebook com o ID do workbook de qualidade da nova máquina.

2. Executar Cell 2 (`spy.search`) para confirmar que os sinais de delay/waste/fault foram encontrados.
   Se o sinal de delay tiver nome diferente, ajustar `QUALITY_GROUPS['delay']` com o padrão correto.

3. O notebook lê `02_sinais_forca.csv` e `01_weibull_params.json` automaticamente — basta ter o pipeline
   da nova máquina rodado ao menos uma vez para gerar esses arquivos.

4. Os thresholds (`THRESHOLD_P_RISK`, `THRESHOLD_FORCA_N`) são lidos de `config.yaml` — não precisam
   ser alterados manualmente.

---

## Padrão Canônico de Pull Seeq

Todo pull de dados no Seeq Data Lab deve seguir este padrão (validado em produção):

```python
from seeq import spy
import pandas as pd

# Timezone — NUNCA hardcodar 'UTC'
user_tz    = spy.session.get_user_timezone()
end_time   = pd.Timestamp.now(tz=user_tz)
start_time = end_time - pd.DateOffset(years=4)  # ou days=N, months=N

# Items DataFrame — uma linha por sinal, com 'Type': 'Signal'
items = pd.DataFrame([
    {'ID': 'D879CAD1-0BD1-4BDA-B215-6FF0E203A8D4', 'Type': 'Signal'},
])

# Pull — datas via .isoformat()
df = spy.pull(
    items,
    start=start_time.isoformat(),
    end=end_time.isoformat(),
    grid='1h',      # ou '1d', None (bruto)
    header='Name',  # ou 'ID'
    quiet=True,
)

if df is not None and not df.empty:
    df.index = pd.to_datetime(df.index, utc=True)
```

**Anti-padrões confirmados como falha:**
- `spy.utils.get_user_timezone(spy.session)` — API antiga, use `spy.session.get_user_timezone()`
- `start="2024-01-01"` (string sem timezone) — ambiguidade de fuso, Seeq pode deslocar dados
- Items DataFrame sem coluna `'Type'` — pull pode falhar silenciosamente

---

## Parâmetros ajustáveis (Cell 1)

| Parâmetro | Default | Quando alterar |
|---|---|---|
| `DELAY_THRESHOLD_MIN` | 0 | Se o sinal de delay tem ruído e pequenos valores não significam parada real |
| `W_DELAY` / `W_WASTE` / `W_FAULT` | 0.40 / 0.35 / 0.25 | Para ponderar diferente a importância de cada fonte de qualidade |
| `JANELA_EVENTO_H` | 2.0 | Janela de ±2h para associar métricas de qualidade ao evento |
| `ANOS_HISTORICO` | 4 | Reduzir para debugging rápido (ex: 1 ano) |

---

## Output consumido por

- `project-kairos/rca/rca_fb14_analise.ipynb` — carrega `anomaly_delay.csv` e substitui
  `anomaly_freq` por `anomaly_freq_weighted` (pesos por `quality_weight`)
- `notebooks/anomaly_scan.ipynb` — usa `anomaly_delay.csv` (col `is_yellow_card`) como fallback
  para extrair datas de disparo quando SharePoint não está disponível
- Futuramente: `src/trigger_engine.py` pode usar `age_risk_corrected` nos gatilhos AVISO/CONFIRMADO

## Notebook relacionado: anomaly_scan.ipynb

Varredura ampla de todos os PI points (`*BRSZA020*`) nos momentos de disparo:
- **Baseline**: mês anterior completo (`grid=1h`)
- **Janelas de anomalia**: N dias antes de cada disparo (padrão 7, configurável via `JANELA_ANTES_D`)
- **Saída**: `anomaly_scan.csv` com z-score por sinal × janela e ranking `Max_AbsZScore`
- Filtra automaticamente Capsules e sinais sem cobertura mínima no baseline
