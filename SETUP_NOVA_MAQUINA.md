# SETUP_NOVA_MAQUINA.md

Guia de implementação para Claude Code configurar o sistema de manutenção preditiva em uma nova máquina.
Siga os passos em ordem. Não avance para o próximo passo sem confirmar que o anterior foi concluído.

> **Nota para o agente:** Este repositório é a instalação de referência (FB14). Ao adaptar para
> uma nova máquina (FB17, FB22…), **nunca sobrescreva** `config.yaml` nem `init_project.ipynb`
> desta instalação. Clone o repositório em um diretório separado (`fb17-datalab/`) e edite apenas
> os arquivos listados no Passo 2. Os arquivos centrais desta instalação são referência de produção
> e não devem ser alterados por automações de setup de outra máquina.

---

## Arquitetura de dados: dois workbooks Seeq

O pipeline FB14 lê dados de **dois workbooks Seeq distintos**. Cada nova máquina precisará
identificar os equivalentes em seu ambiente:

### Workbook 1 — Força e Phantom (selagem)

Contém os sinais de força de selagem e o código de produto (Phantom/SKU):

| Sinal | Padrão de nome FB14 | Descrição |
|---|---|---|
| Forca_A | `BRSZ FB14*Forca*A*` | Força lado A do rolo maintacker |
| Forca_B | `BRSZ FB14*Forca*B*` | Força lado B do rolo maintacker |
| Phantom | `BRSZ FB14*Phantom*` ou `*product*code*` | Código do SKU em produção |

O **Workbook ID** deste workbook é gravado em `config.yaml` → `project.workbook_id` pelo
`init_project.ipynb`. Ele é usado por todos os generators (`gen_hour_prev`, `gen_sinais`, etc.).

### Workbook 2 — Delay, Bagger e Waste (contexto operacional)

Contém os sinais de estado operacional da linha usados pelo `anomaly_delay.ipynb`:

| Sinal | Padrão de nome FB14 | Descrição |
|---|---|---|
| Delay calculado | `BRSZ FB14*B1*Tempo Delay Calculado*` | Minutos de parada acumulados |
| Waste | `BRSZ FB14*B1*Waste*` ou `*refugo*` | Contagem de eventos de refugo |
| Fault code | `BRSZ FB14*B1*Fault*` ou `*codigo*falha*` | Código de falha ativo |

O **Workbook ID** deste segundo workbook é gravado em `config.yaml` → `project.delay_workbook_id`
pelo `init_project.ipynb` (cell 7–9, seção "Workbook de Delay"). Ele alimenta exclusivamente
`anomaly_delay.ipynb` e não é necessário para o pipeline de força rodar.

> **FB17 — PENDENTE:** O Workbook ID do workbook de delay da FB17 ainda não foi identificado.
> Antes de executar `anomaly_delay.ipynb` na FB17, abrir o Seeq Data Lab, localizar o workbook
> que contém os sinais de Delay/Bagger/Waste da FB17 e registrar o ID em:
> `config.yaml → project.delay_workbook_id`. O pipeline de força (Workbook 1) pode rodar
> normalmente enquanto isso — `anomaly_delay.ipynb` simplesmente não executará até que
> `delay_workbook_id` seja preenchido.

---

## Relação entre os workbooks e os notebooks

```
Workbook 1 (Força/Phantom)
  └─ init_project.ipynb → config.yaml (project.workbook_id)
       └─ gen_hour_prev → gen_vida_rul → gen_sinais → gen_padroes
            └─ pipeline_producao.ipynb (TriggerEngine → SharePoint)
            └─ 04_pipeline_mensageria.ipynb (backtest + diagnóstico)

Workbook 2 (Delay/Bagger/Waste)
  └─ init_project.ipynb → config.yaml (project.delay_workbook_id)
       └─ anomaly_delay.ipynb → anomaly_delay.csv
            └─ pipeline_producao.ipynb (df_delay → age_risk_corrected + quality_weight)
            └─ 04_pipeline_mensageria.ipynb (df_delay → backtest com correção)
```

**O pipeline de força é independente do workbook de delay.** Se `anomaly_delay.csv` não existir,
o `TriggerEngine` usa automaticamente o `age_risk` bruto sem correção (fallback transparente).

---

## Contexto do sistema

O pipeline monitora **força de selagem** de um rolo maintacker via dois sinais do Seeq (Forca_A e Forca_B),
normaliza os valores pelo produto em produção (sinal Phantom), e dispara alertas para uma lista SharePoint
quando os sinais indicam degradação. A cadência de troca do rolo segue uma distribuição Weibull calibrada
historicamente por máquina.

```
Seeq (Forca_A, Forca_B, Phantom)           Seeq (Delay, Waste, Fault)
  └─ pipeline_producao.ipynb [1h]              └─ anomaly_delay.ipynb [manual/diário]
       ├─ Generators: extrai, processa               └─ anomaly_delay.csv
       ├─ TriggerEngine v4.0:                              ↓ df_delay (opcional)
       │    age_risk_corrected (se df_delay)    ──────────┘
       │    quality_weight gate (CONFIRMADO)
       │    RISCO / CRITICO → SharePoint
       └─ Power Automate → Teams Adaptive Card

04_pipeline_mensageria.ipynb  [uso manual]
  └─ Backtest histórico de gatilhos + diagnóstico do ciclo atual (usa df_delay se disponível)
```

**Três arquivos que o operador mantém manualmente:**
- `troca_modulo.csv` — data de cada troca do rolo (ground truth do ciclo)
- `sharepoint.ev` — credenciais SharePoint (nunca versionado)
- `config.yaml` — IDs dos sinais Seeq e parâmetros do engine (gerado por `init_project.ipynb`, depois editado)

---

## Pré-requisitos (confirmar antes de começar)

```
[ ] Acesso ao Seeq Data Lab da planta (https://kcc.seeq.site ou URL equivalente)
[ ] Worksheet do Seeq com os 3 sinais: força A, força B, phantom/produto
[ ] Lista SharePoint criada com os campos: Title (texto), Maquina (texto), TeamsPayload (texto longo)
[ ] Credenciais SharePoint disponíveis: SP_USER, SP_PASS
[ ] Power Automate configurado para monitorar a lista e postar no canal Teams
[ ] Histórico de trocas da máquina (pelo menos datas aproximadas desde 2025)
```

---

## Passo 1 — Clonar o repositório

```bash
# Substitua fb17 pelo código da nova máquina (fb17, fb22, etc.)
git clone https://github.com/coniose/fb14-datalab.git fb17-datalab
cd fb17-datalab
```

---

## Passo 2 — Adaptar os dois notebooks principais

Estes são os únicos dois arquivos com o nome da máquina hardcoded. Editar antes de qualquer execução.

### `notebooks/pipeline_producao.ipynb` — Cell 2

Localizar a célula de configuração (segunda célula de código) e alterar:

```python
MAQUINA     = "FB17"          # ← nome da nova máquina (igual ao worksheet_name do Seeq)
PATH_STATE  = _NB_DIR / "state_fb17.json"   # ← estado do engine para esta máquina
```

### `notebooks/04_pipeline_mensageria.ipynb` — Cell 1

Localizar a célula de configuração (primeira célula de código) e alterar:

```python
MAQUINA = "FB17"
```

---

## Passo 3 — Descobrir os sinais no Seeq e gerar config.yaml

Abrir e executar `init_project.ipynb` célula a célula:

1. **Cell 3** — colar a URL da worksheet do Seeq que contém os sinais da máquina:
   ```python
   WORKSHEET_URL = "https://kcc.seeq.site/.../workbook/XXXX/worksheet/YYYY"
   ```

2. **Cell 5** — executa `spy.workbooks.pull()` e classifica automaticamente os sinais:
   - Nomes com `phantom`, `product`, `code`, `sku`, `mes`, `bagger` → sinal de **Phantom**
   - Demais sinais → sinais de **Força** (Forca_A e Forca_B por ordem de exibição)

3. **Cell 7** — confirmar a seleção. Se a ordem estiver errada, ajustar os índices:
   ```python
   IDX_FORCA_A = 0   # índice do sinal Forca_A na lista de sinais de força
   IDX_FORCA_B = 1   # índice do sinal Forca_B
   IDX_PHANTOM = 0   # índice do sinal Phantom
   ```

4. **Cell 11** — gera `config.yaml` automaticamente com os IDs corretos e parâmetros padrão.

**Após executar, verificar que `config.yaml` foi criado na raiz do projeto com:**
```yaml
project:
  maquina: FB17        # ← deve corresponder ao nome definido no Passo 2
  workbook_id: "..."   # preenchido automaticamente
```

Se `project.maquina` não corresponder ao nome definido no Passo 2, corrigir manualmente.

---

## Passo 3-B — Identificar o Workbook de Delay (contexto operacional)

Este passo é **independente** do Passo 3. O pipeline de força funciona sem ele. Mas para ativar
a correção de `age_risk` por paradas e o filtro de `quality_weight`, o segundo workbook precisa
ser registrado.

1. No Seeq Data Lab, abrir o workbook que contém os sinais de delay/bagger/waste da máquina.
2. Copiar o **Workbook ID** da URL: `https://kcc.seeq.site/.../workbook/**<ID>**/...`
3. Adicionar ao `config.yaml`:
   ```yaml
   project:
     workbook_id: "..."          # já preenchido pelo Passo 3
     delay_workbook_id: "XXXX-XXXX-XXXX"   # ← preencher aqui
   ```
4. Abrir `notebooks/anomaly_delay.ipynb` e verificar a cell de configuração:
   ```python
   WORKBOOK_ID_DELAY = cfg["project"]["delay_workbook_id"]
   ```
5. Executar `anomaly_delay.ipynb` no browser do Seeq Data Lab (requer sessão `spy` autenticada).
   O notebook gera `notebooks/anomaly_delay.csv` com `stopped_h`, `age_effective_h`, `quality_weight`.
6. A partir da próxima execução, `pipeline_producao.ipynb` detecta o CSV e ativa a correção
   automaticamente — sem nenhuma alteração adicional de código.

> **FB17 — PENDENTE:** `delay_workbook_id` ainda não foi identificado para FB17.
> Executar os passos 1–3 acima antes de rodar `anomaly_delay.ipynb`.

---

## Passo 4 — Gerar troca_modulo.csv bootstrap

Para uma máquina nova sem histórico preciso, gerar datas sintéticas mensais a partir de 2025.
O pipeline aceita essas datas e o operador as corrige manualmente à medida que as trocas reais ocorrem.

Executar no terminal dentro de `notebooks/`:

```python
import pandas as pd
from pathlib import Path

# Ajustar: data da primeira troca conhecida + intervalo médio esperado
inicio   = pd.Timestamp("2025-01-15")
fim      = pd.Timestamp("2026-12-31")
intervalo_dias = 30   # intervalo médio entre trocas (ajustar pela máquina)

datas = pd.date_range(start=inicio, end=fim, freq=f"{intervalo_dias}D")
df = pd.DataFrame({"Data-base do inicio": datas.strftime("%Y-%m-%d"),
                   "tipo": "revestimento"})
df.to_csv("notebooks/troca_modulo.csv", index=False)
print(f"{len(df)} datas geradas: {datas[0].date()} → {datas[-1].date()}")
```

**Importante:** Essas datas são placeholder. O operador deve corrigi-las com as datas reais
assim que disponíveis. O pipeline se auto-adapta quando `troca_modulo.csv` é atualizado —
basta salvar o arquivo e o próximo ciclo horário detecta a mudança automaticamente.

---

## Passo 5 — Configurar credenciais SharePoint

Criar o arquivo `sharepoint.ev` na **raiz do projeto** (nunca no git):

```bash
# sharepoint.ev  — não commitar
SP_USER=usuario@empresa.com
SP_PASS=senha_aqui
SP_URL=https://empresa.sharepoint.com/Sites/NOME_DO_SITE
```

Verificar que `.gitignore` já contém `*.ev` — se não, adicionar.

**Verificar a conexão:**
```python
from dotenv import dotenv_values
from src.sharepoint_methods import SharePointClient

creds = dotenv_values("sharepoint.ev")
sp = SharePointClient(creds["SP_URL"], creds["SP_USER"], creds["SP_PASS"])
print(sp.list_all_lists())   # deve incluir 'Gatilhos_Selagem'
```

---

## Passo 6 — Primeira execução do pipeline

```bash
cd notebooks
jupyter nbconvert --to notebook --execute pipeline_producao.ipynb \
    --output pipeline_producao.ipynb \
    --ExecutePreprocessor.timeout=600
```

**O que verificar na saída:**

| Célula | O que deve aparecer |
|---|---|
| Cell 4 | `[0/4]…[4/4]` sem erros fatais — pull Seeq OK |
| Cell 6 | `Media_norm: N/N leituras (≥50%)` — phantom calibrado |
| Cell 8 | Última troca e idade do rolo corretas |
| Cell 10 | `SharePoint: conectado (https://...)` |
| Cell 11 | `Avaliando YYYY-MM-DD | rolo com N dias` e resultado dos gatilhos |

Se Cell 11 mostrar `Nenhum gatilho disparado` e o rolo for novo — correto.
Se Cell 10 mostrar `modo dry-run` — verificar `sharepoint.ev` e `ESCREVER_SP = True`.

---

## Passo 7 — Ativar o agendamento horário no Seeq

O pipeline já contém a linha de agendamento na primeira célula de `pipeline_producao.ipynb`:

```python
spy.jobs.schedule("every 1 hour")
```

Basta executar o notebook **uma vez via interface do Seeq Data Lab** (não via `nbconvert` no terminal)
para registrar o job no scheduler. Após isso o Seeq executa automaticamente a cada hora com sessão
`spy` autenticada.

**Para confirmar que o job foi registrado:**
```python
from seeq import spy
spy.jobs.status()   # deve listar pipeline_producao.ipynb com status SCHEDULED
```

---

## Passo 8 — Calibração Weibull (após ≥ 8 trocas reais confirmadas)

Os parâmetros padrão (`weibull_beta: 1.181`, `weibull_eta_h: 1297.0`) foram calibrados para FB14.
Cada máquina tem seu próprio padrão de desgaste — calibrar quando houver dados suficientes.

Executar `notebooks/01_vida_rul.ipynb` e checar `01_weibull_params.json`:

```json
{ "beta": 1.2, "eta_h": 1150.0 }
```

Atualizar `config.yaml`:
```yaml
trigger:
  weibull_beta: 1.2       # ← valor calibrado
  weibull_eta_h: 1150.0   # ← valor calibrado
```

**Regra de bolso por máquina:** se os alertas CRITICO/FIM_DE_VIDA chegam cedo demais (rolo ainda bom),
aumentar `weibull_eta_h`. Se chegam tarde, diminuir. `vida_decay_w: 0.8` ajusta automaticamente
pelo histórico do ano corrente.

---

## Passo 9 — Calibração do limiar de força (risco_forca_limiar)

O limiar de 800 N foi validado para FB14. Para outras máquinas, o valor pode ser diferente.
Verificar com o técnico de manutenção qual a força mínima aceitável para a selagem.

Atualizar `config.yaml`:
```yaml
trigger:
  risco_forca_limiar: 750.0    # ← ajustar para a máquina
  critico_forca_min:  750.0    # ← manter igual ao risco_forca_limiar
  proj_48h_limiar:    750.0    # ← manter igual
```

---

## Verificação final

```
[ ] config.yaml gerado com IDs corretos (MAQUINA = nome da máquina)
[ ] pipeline_producao.ipynb Cell 2: MAQUINA e PATH_STATE atualizados
[ ] 04_pipeline_mensageria.ipynb Cell 1: MAQUINA atualizado
[ ] troca_modulo.csv existe em notebooks/ com datas desde 2025
[ ] sharepoint.ev existe na raiz, não versionado
[ ] Pipeline executou sem erros nas cells 4–11
[ ] Job registrado no Seeq scheduler (spy.jobs.status())
[ ] Teste de escrita SP: um item [TESTE] apareceu na lista e no Teams
```

---

## Referência rápida de arquivos por máquina

| Arquivo | Versionado? | Gerado por |
|---|---|---|
| `config.yaml` | Não | `init_project.ipynb` → editar manualmente depois |
| `notebooks/troca_modulo.csv` | Não | Script do Passo 4 → operador mantém |
| `sharepoint.ev` | Não | Criado manualmente |
| `notebooks/state_fb17.json` | Não | Gerado automaticamente na primeira execução |
| `notebooks/00_hour_prev.csv` … `03_padroes.csv` | Não | Gerado pelo pipeline |
| `notebooks/01_weibull_params.json` | Não | `01_vida_rul.ipynb` |

---

## Diagnóstico — quando o pipeline não dispara

1. Rodar `04_pipeline_mensageria.ipynb` — backtest do ciclo atual mostra o que o engine vê
2. Checar `compute_p_risk_snapshot()` na Cell 13 — indica quais condições estão falhando
3. Verificar `notebooks/state_fb17.json` — se `cycle_start` é mais recente que a última troca no CSV, reiniciar com `close_all_by_troca()`
4. Se suspeitar de threshold errado, ajustar em `config.yaml` e reexecutar — sem reiniciar o kernel
