# Skill: Anomaly Delay — Correção de age_risk e Ponderação por Qualidade

## Objetivo

Cruzar dados de **delay/parada de máquina** com o consumo de peças mecânicas para:

1. **Corrigir `age_risk`** — quando a máquina está parada as peças não acumulam
   desgaste; descontar horas paradas do calendário do ciclo produz um Weibull mais
   fidedigno à vida real da peça.

2. **Ponderar anomalias** — quedas de força abaixo do limiar que coincidem com alto
   delay, waste ou código de falha registrado têm peso diagnóstico diferente de
   leituras isoladas em produção plena.

Fórmula de correção:
```
stopped_h        = Σ horas com delay > DELAY_THRESHOLD desde inicio_ciclo até timestamp
age_effective_h  = horas_desde_troca - stopped_h  (min 0)
age_risk_corr    = 1 - exp(-(age_effective_h / η)^β)
```

Fórmula de ponderação:
```
delay_weight   = clip(Σ delay na janela ±2h / p95_global_delay,  0, 1)
waste_weight   = clip(Σ waste na janela ±2h / p95_global_waste,  0, 1)
fault_weight   = 1.0 se fault_code > 0 na janela ±2h, senão 0.0
quality_weight = 0.40×delay + 0.35×waste + 0.25×fault
```

---

## Contexto de Projeto (preencher antes de executar)

| Parâmetro | FB14 | FB16/FB17 (preencher) |
|---|---|---|
| `MAQUINA_ID` | `FB14` | — |
| `WORKBOOK_ID` | `0F116FED-4C4F-FD60-9EA0-0CA92EE4B765` | URL da worksheet do Seeq |
| `DELAY_THRESHOLD_MIN` | `0` (qualquer delay conta) | Ajustar se sinal tem ruído |
| `PARTE_MECANICA` | Rolo Maintacker | — |

---

## Referência Rápida — Sinais de Qualidade Conhecidos

| Projeto | Sinal Delay | Sinal Waste | Sinal Fault | Sinal Uptime |
|---|---|---|---|---|
| FB14 | `BRSZ FB14 Quality - FB14_B1_Tempo Delay Calculad` | a descobrir | a descobrir | a descobrir |
| FB16/FB17 | buscar via `*FB16*Delay*` | buscar via `*FB16*Waste*` | buscar via `*FB16*Fault*` | buscar via `*FB16*Uptime*` |

---

## Passo 1 — Descobrir sinais de qualidade

Executar Cell 2 do notebook `anomaly_delay.ipynb` no Seeq Data Lab.

```python
# spy.search por grupo — confirmar IDs antes do pull
QUALITY_GROUPS = {
    'delay':  ['*FB14*Delay*', '*FB14*Tempo Delay*', '*FB14_B1_Tempo*'],
    'waste':  ['*FB14*Waste*', '*FB14*Refugo*', '*FB14*NOK*'],
    'fault':  ['*FB14*Fault*', '*FB14*Falha*', '*FB14*Cod*Falha*', '*FB14*Parada*'],
    'uptime': ['*FB14*Uptime*', '*FB14*Disponib*', '*Bagger*FB14*', '*FB14*OEE*'],
}
```

**Critério de aceitação:** ao menos 1 sinal de `delay` encontrado com nome contendo "Tempo Delay".

---

## Passo 2 — Pull sinal de delay (grid=1h)

Cell 3. Pull com `grid='1h'` para compatibilidade com os sinais de força.

Consolidar múltiplos sinais de delay em coluna única `delay_min` (média se houver mais de um).

**Verificar:** `delay_min > 0` deve ter X horas paradas — se X = 0 em 4 anos, o sinal está vazio.

---

## Passo 3 — Carregar dados de força (02_sinais_forca.csv)

Cell 5. Lê `02_sinais_forca.csv` sem re-pull do Seeq.

Colunas necessárias: `Timestamp`, `ciclo_id`, `horas_desde_troca`, `age_risk`, `p_risk`, `min_forca_3d`.

Filtrar `ciclo_id >= 0` (exclui leituras antes da primeira troca registrada).

---

## Passo 4 — Corrigir age_effective (Weibull + stopped_h)

Cell 6. Parâmetros Weibull de `01_weibull_params.json` (calibrado para a máquina — não usar config.yaml).

Para cada ciclo, calcular `stopped_h` cumulativo até cada timestamp e derivar `age_effective_h`.

---

## Passo 5 — Calcular quality_weight

Cell 7. Filtrar cartões amarelos: `p_risk > critico_p_risk_min` OU `min_forca_3d < risco_forca_limiar`.

Para cada cartão, calcular pesos na janela ±2h. Normalizar por p95 global para invariância de escala.

---

## Passo 6 — Exportar anomaly_delay.csv

Cell 8. Exporta para `notebooks/anomaly_delay.csv`.

---

## Formato de Saída

| Coluna | Tipo | Descrição |
|---|---|---|
| `Timestamp` | datetime UTC | Horário da leitura |
| `ciclo_id` | int | Índice do ciclo de troca (≥ 0) |
| `horas_desde_troca` | float | Age calendário em horas |
| `stopped_h` | float | Horas paradas acumuladas no ciclo até este timestamp |
| `age_effective_h` | float | `horas_desde_troca − stopped_h` (mín 0) |
| `age_risk_original` | float [0,1] | Weibull CDF sem correção de parada |
| `age_risk_corrected` | float [0,1] | Weibull CDF com `age_effective_h` |
| `p_risk` | float [0,1] | Score combinado do trigger engine |
| `min_forca_3d` | float | Mínimo de força nos últimos 3 dias |
| `is_yellow_card` | bool | True se cartão amarelo neste timestamp |
| `delay_weight` | float [0,1] | Peso de delay na janela ±2h |
| `waste_weight` | float [0,1] | Peso de waste na janela ±2h |
| `fault_weight` | float {0,1} | 1.0 se fault_code > 0 na janela |
| `quality_weight` | float [0,1] | Score ponderado de qualidade |

---

## Checklist de Entrega

```
[ ] Cell 2: sinal BRSZ FB14 Quality - FB14_B1_Tempo Delay Calculad encontrado
[ ] Cell 6: age_risk_corrected < age_risk_original para ciclos com stopped_h > 0
[ ] Cell 7: quality_weight ∈ [0, 1] em todos os registros com is_yellow_card = True
[ ] Cell 8: anomaly_delay.csv exportado com todas as colunas do formato de saída
[ ] Inspeção: age_effective_h ≥ 0 em todos os registros
[ ] Inspeção: paradas acumuladas fazem sentido operacional (ex: parada semanal de manutenção)
```

---

## Output consumido por

- `project-kairos/rca/rca_fb14_analise.ipynb` — substitui `anomaly_freq` por
  `anomaly_freq_weighted` usando `quality_weight`
- `fb14-datalab/src/trigger_engine.py` — futuramente: usar `age_risk_corrected`
  nos gatilhos AVISO/CONFIRMADO em vez de `age_risk` instantâneo

---

## Adaptando para outra máquina

1. Copiar `anomaly_delay.ipynb` para o novo repositório de máquina
2. Alterar `WORKBOOK_ID` para o workbook de qualidade da nova máquina
3. Ajustar `QUALITY_GROUPS` com os padrões de nome corretos para a planta
4. Manter `DELAY_THRESHOLD_MIN = 0` até confirmar que o sinal de delay é limpo
5. Calibrar Weibull via `01_vida_rul.ipynb` antes de rodar a correção de age
