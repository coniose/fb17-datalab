# /design-solucao

Skill de design criativo para novas soluções de monitoramento preditivo com alertas no Teams.

Usado quando o usuário quer construir um novo projeto do zero seguindo o padrão:
**Dados industriais → Algoritmo → Gatilhos → SharePoint List → Power Automate → Teams Adaptive Card**

---

## Como usar esta skill

Quando o usuário invocar `/design-solucao`, conduza uma entrevista estruturada em 5 etapas.
Cada etapa faz perguntas específicas e **não avança** para a próxima antes de ter respostas suficientes.
No final, gere um documento de especificação técnica completo.

---

## Etapa 1 — O problema e o objeto monitorado

Faça as seguintes perguntas:

1. **O que está sendo monitorado?** (peça mecânica, processo, equipamento, linha inteira)
2. **Qual a consequência de não monitorar?** (parada de linha, falha de qualidade, segurança)
3. **Qual a frequência de falha esperada?** (dias, semanas, meses)
4. **Já existe histórico de falhas registrado?** (planilhas, SAP IW38, CMMS, papel)
5. **Quem vai receber o alerta e tomar ação?** (técnico, engenheiro, supervisor, time)

Use as respostas para entender se o problema é candidato ao padrão Weibull + Sinal ou se é um
caso mais simples (threshold direto) ou mais complexo (ML supervisionado).

---

## Etapa 2 — Levantamento de dados disponíveis

Investigue o que existe antes de propor o algoritmo. Pergunte:

### Dados de sinal (Seeq)
- Existe workbook no Seeq com dados da peça ou processo?
- Se sim: qual o nome do workbook? quantas worksheets? quais sinais relevantes?
- Os sinais são em tempo real (PI tags) ou calculados (Seeq formulas)?
- Qual a granularidade temporal disponível? (1s, 1min, 1h)
- Há algum sinal sabidamente corrompido ou com gaps frequentes? (risco de PIException)

> Para descobrir sinais no Seeq Data Lab: `spy.search(worksheet_url)` — só metadados, sem pull.
> Nunca usar `spy.pull(worksheet_url)` para descoberta — pode lançar PIException em sinais corrompidos.

### Dados de vida/histórico (SharePoint / Excel / SAP)
- Existe uma planilha ou lista com datas de manutenção/troca passadas?
- Se for Excel em SharePoint: qual a URL da pasta? qual o nome do arquivo?
- Quantos eventos históricos existem? (mínimo recomendado para Weibull: 8–10 falhas)
- As datas de falha são confirmadas ou estimadas?

### Dados de contexto (produto, receita, SKU)
- O comportamento do sinal muda dependendo do produto fabricado?
- Se sim: existe algum sinal de código de produto no Seeq ou no MES?
- Isso é um Paradoxo de Simpson potencial — normalização por SKU pode ser necessária.

---

## Etapa 3 — Seleção do algoritmo

Com base nas respostas da Etapa 2, proponha o conjunto de algoritmos mais adequado:

### Análise de vida (quando há histórico de falhas)
- **Weibull CDF**: recomendado quando há ≥8 eventos de falha confirmados. Gera `age_risk`.
  - Parâmetros: β (forma), η (escala em dias ou horas)
  - Calcular via MLE (scipy.stats.weibull_min ou censored Weibull)
  - Dados necessários: datas de troca / falha confirmadas

- **Threshold simples de idade**: quando não há histórico suficiente. Define limiar em dias
  com base na recomendação do fabricante ou experiência do técnico.

### Análise de sinal (quando há dados de processo)
- **Slope (tendência)**: regressão linear sobre janela rolling (7d ou 14d). Detecta declínio.
- **Ratio de médias**: `mean_3d / mean_14d`. Detecta aceleração do declínio.
- **Projeção OLS age-gated**: regressão com intercepto por fase de vida. Projeta valor em 48h.
  - Gate de idade (≥15d ou ≥20d) para evitar falsos positivos no acomodamento inicial.
- **Signal score composto**: combinação ponderada de slope e ratio.
  `sig_score = deg×0.6 + slope×0.4` (ajustar pesos ao domínio)

### Normalização de contexto
- **Normalização por SKU/produto**: `Media_norm = Media / stress_factor_sku`
  - Necessária quando o mix de produtos afeta o sinal medido
  - Calibrar por fresh-roller baseline (primeiros 10 dias de cada ciclo por produto)

### Combinação p_risk
```
p_risk = age_risk + (1 − age_risk) × sig_score × boost
```
onde `boost` controla quanto o sinal amplifica o risco etário (ajustar por backtest).

---

## Etapa 4 — Definição dos gatilhos

Defina a hierarquia de gatilhos. O padrão do projeto tem 4 níveis:

| Nível | Nome | Quando disparar | Ação esperada |
|---|---|---|---|
| 1 | EMERGENCIA | Sinal cruza limite absoluto de segurança | Parar / trocar imediatamente |
| 2 | RED | Múltiplas condições simultâneas (risco alto confirmado) | Agendar troca urgente |
| 3 | AMARELO | Condição individual de alerta precoce | Monitorar, planejar troca |
| 4 | REVISAO | Marcos automáticos de ciclo (ex: dias 20, 30, 45) | Inspeção agendada |

Para cada gatilho, defina:
- **Condição exata** (valores numéricos, AND/OR, janela temporal)
- **Cooldown** (quanto tempo esperar antes de disparar novamente)
- **Supressão** (AMARELO não dispara se RED estiver ativo)
- **Mensagem gerencial** (o que o técnico/supervisor vai ler no Teams)
- **Ação recomendada** (texto específico para o card)

Pergunta-chave: **quais condições, se todas verdadeiras ao mesmo tempo, garantem com alta
confiança que a intervenção é necessária?** Esse é o RED. O AMARELO é qualquer uma isolada.

---

## Etapa 5 — Design do Adaptive Card (visual no Teams)

O card é o único produto visível para o operador. Projete-o junto com quem vai usar.

Perguntas:
1. **Quem lê?** (técnico de manutenção, supervisor de planta, gerente de produção)
2. **Que decisão precisa tomar?** (trocar agora, agendar, investigar, ignorar)
3. **Quais números são críticos para essa decisão?** (máximo 4–5 indicadores)
4. **A linguagem deve ser técnica ou gerencial?**

Estrutura recomendada do card (adaptada do padrão FB14):
```
┌─────────────────────────────────────────────┐
│  [SEVERIDADE] [TIPO ALERTA]    [MÁQUINA]    │
│  [Título do processo]    [Data/hora]         │
├─────────────────────────────────────────────┤
│  VIDA DO COMPONENTE (se aplicável)           │
│  ████████░░░░  XX% consumida                │
│  NN dias em operação    ~NN dias p/ troca    │
├─────────────────────────────────────────────┤
│  INDICADORES                                 │
│  [Indicador 1]     [Valor]                  │
│  [Indicador 2]     [Valor]                  │
│  [Indicador 3]     [Valor]                  │
├─────────────────────────────────────────────┤
│  AÇÃO RECOMENDADA                            │
│  [Texto da ação em linguagem do operador]    │
└─────────────────────────────────────────────┘
```

---

## Saída — Documento de especificação técnica

Ao final das 5 etapas, gere um documento estruturado com:

```markdown
# Especificação Técnica — [Nome do Projeto]

## Objeto monitorado
[Peça/processo, máquina, consequência de falha]

## Fontes de dados
| Fonte | Tipo | Localização | Sinais/colunas relevantes |
|---|---|---|---|
| Seeq Workbook | Tempo real | [nome/ID] | [sinal_A, sinal_B] |
| Excel SharePoint | Histórico | [URL/pasta] | [coluna data, coluna evento] |

## Algoritmo selecionado
- Análise de vida: [Weibull / threshold / não aplicável]
- Análise de sinal: [slope + ratio / signal_score / threshold direto]
- Normalização: [por produto / não necessária]
- Fórmula p_risk: [fórmula com parâmetros iniciais sugeridos]

## Gatilhos
[tabela de gatilhos com condições numéricas e ações]

## Card Teams
[esboço textual do card com indicadores definidos]

## Estrutura de arquivos sugerida
[lista de geradores, módulos e notebooks a criar]

## Dependências de infraestrutura
- Lista SharePoint: [nome] | Colunas: [lista]
- Arquivo ENV: SP_URL, SP_USER, SP_PASS (em sharepoint.ev, não versionado)
- Workbook Seeq: [nome, worksheet URL para spy.search()]
- Power Automate: [nome do fluxo]
```

---

## Boas práticas a lembrar durante o design

- **spy.search() vs spy.pull()**: nunca use `spy.pull(worksheet_url)` para descoberta de sinais — use `spy.search(url)` (só metadados, sem risco de PIException). Só após confirmar os IDs corretos, configure `spy.pull(items_df, header="ID")`.
- **spy já autenticado no Data Lab**: não há necessidade de `spy.login()` ou access keys.
- **Testes antes de produção**: qualquer integração com SharePoint deve ir para uma lista/pasta de testes primeiro (`[TESTE]` no título dos itens). Ver `/testar-sp`.
- **ENV nunca vai ao git**: credenciais sempre em arquivo `.ev` gitignored.
- **config.yaml é a fonte de verdade**: todos os parâmetros numéricos ficam no YAML, nunca hardcoded.
- **Uma branch por feature**: nunca desenvolver diretamente em `main`.
