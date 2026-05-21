# Configuração: Mensageria Teams via SharePoint + Power Automate

Este documento cobre os dois passos de infraestrutura necessários para que os alertas
do sistema de selagem cheguem ao Teams como cards visuais (tabelas, barra de vida,
indicadores em linguagem gerencial).

---

## Parte 1 — Coluna TeamsPayload na lista SharePoint

A lista `Gatilhos_Selagem` precisa de uma nova coluna que armazene o JSON do card
gerado automaticamente pelo Python antes de cada alerta.

### Passo a passo

1. Abra o SharePoint e navegue até o site de manutenção
2. Clique em **Conteúdo do site** → selecione a lista **Gatilhos_Selagem**
3. No cabeçalho da lista, clique em **+ Adicionar coluna**
4. Selecione o tipo **Várias linhas de texto**
5. Preencha os campos:

   | Campo | Valor |
   |---|---|
   | Nome | `TeamsPayload` |
   | Descrição | JSON do Adaptive Card — lido pelo Power Automate |
   | Tipo de texto | **Texto sem formatação** (não rich text, não enhanced) |
   | Número máximo de caracteres | `10000` (o card típico tem ~4.400 caracteres) |
   | Exibir na visualização padrão | Não (campo interno, não precisa aparecer na grade) |

6. Clique em **Salvar**

> **Por que "Texto sem formatação"?**  
> O campo vai receber JSON puro. Se o SharePoint tentar interpretar o conteúdo
> como HTML (rich text), ele pode escapar caracteres como `"` e quebrar o JSON
> que o Power Automate vai ler.

---

## Parte 2 — Fluxo Power Automate

O fluxo é intencionalmente simples: um gatilho + uma ação. Toda a lógica de
formatação já aconteceu no Python.

### Criar o fluxo

1. Acesse **Power Automate** → **Criar** → **Fluxo de nuvem automatizado**
2. Nome sugerido: `Selagem — Alerta Teams`
3. Gatilho: busque **SharePoint** → **Quando um item é criado**

### Configurar o gatilho

| Campo | Valor |
|---|---|
| Endereço do Site | URL do site SharePoint (ex: `https://lplcc.sharepoint.com/sites/FBMaintenance`) |
| Nome da Lista | `Gatilhos_Selagem` |

### Adicionar a ação de postagem

1. Clique em **+ Nova etapa**
2. Busque **Microsoft Teams** → **Postar um cartão em um bate-papo ou canal**
3. Preencha:

   | Campo | Valor |
   |---|---|
   | Postar como | Usuário do fluxo (ou Bot — depende do tenant) |
   | Postar em | Canal |
   | Team | Selecione o time de manutenção (ex: `FB14 - Manutenção`) |
   | Canal | Selecione o canal de alertas (ex: `Alertas-Selagem`) |
   | Adaptive Card | Clique no campo → **Expressão** → cole: `triggerOutputs()?['body/TeamsPayload']` |

   > **Alternativa sem expressão:** use o botão de conteúdo dinâmico e selecione
   > **TeamsPayload** diretamente da lista de campos do gatilho SharePoint.

4. Clique em **Salvar**

### Adicionar condição (opcional mas recomendado)

Para não postar no Teams alertas de status SNOOZE ou FECHADO que eventualmente
sejam re-inseridos, adicione uma condição antes da ação Teams:

1. Insira **Condição** entre o gatilho e a ação Teams
2. Configure: `triggerOutputs()?['body/Status']` **é igual a** `ATIVO`
3. Mova a ação Teams para o ramo **Em caso afirmativo**

---

## Parte 3 — Verificar se está funcionando

### Teste manual com item sintético

O script abaixo insere um item de teste diretamente na lista para você ver o card
chegar no Teams sem precisar esperar um gatilho real do engine:

```python
# Rodar do diretório raiz do projeto: python -m scripts.testar_card_manual
# (ou colar diretamente em um notebook)

import os, sys
sys.path.insert(0, '.')
from dotenv import dotenv_values
from src.sharepoint_methods import SharePointClient
from src.card_formatter import build_alert_card
from datetime import datetime

creds   = dotenv_values('sharepoint.ev')
sp      = SharePointClient(creds['SP_URL'], creds['SP_USER'], creds['SP_PASS'])

payload = build_alert_card(
    maquina          = "FB14",
    gatilho          = "RED",
    idade_dias       = 27,
    p_risk           = 0.63,
    slope_7d         = -8.2,
    forca_min_3d     = 812.0,
    proj_48h         = 870.0,
    acao_recomendada = (
        "Programar troca preventiva do rolo maintacker esta semana. "
        "Força abaixo de 800 N pode causar falhas de selagem com impacto "
        "direto na qualidade do produto."
    ),
    data_disparo     = datetime.now(),
)

item = {
    "Title":            "FB14 | RED | TESTE",
    "Maquina":          "FB14",
    "Gatilho":          "RED",
    "Severidade":       "ALTA",
    "Mensagem":         "Item de teste — pode deletar",
    "IdadeMaintacker":  27,
    "DataDisparo":      datetime.now().isoformat(),
    "AcaoRecomendada":  "Teste de card visual — ignorar",
    "Status":           "ATIVO",
    "ScoreAtual":       0.63,
    "TeamsPayload":     payload,
}

ids = sp.insert_list_item("Gatilhos_Selagem", [item])
print(f"Item criado — ID: {ids[0]}")
print("Aguarde ~30s e verifique o canal Teams.")
```

### O que você deve ver no Teams

```
┌──────────────────────────────────────────────┐
│  🔴 ALERTA VERMELHO              FB14         │
│  Força de Selagem — Rolo Maintacker           │
│                               12/05/2026 14:35│
├──────────────────────────────────────────────┤
│  VIDA DO MAINTACKER                           │
│  ████████████░░░░░░░░  60% consumida          │
│  27 dias em operação          ~18 dias p/ troca│
├──────────────────────────────────────────────┤
│  INDICADORES                                  │
│  Tendência de Força      ▼ Declínio leve      │
│  Força Projetada (48h)   870 N                │
│  Força Mínima (3 dias)   812 N                │
│  Risco de Parada         Alto (63%)           │
├──────────────────────────────────────────────┤
│  AÇÃO RECOMENDADA                             │
│  Programar troca preventiva desta semana...   │
└──────────────────────────────────────────────┘
```

---

## Resumo da arquitetura

```
Python (trigger_engine.py)
  └─ build_alert_card()          # formata em linguagem gerencial
       └─ TeamsPayload = JSON    # 4.3 KB de Adaptive Card

SharePoint List (Gatilhos_Selagem)
  └─ insert_list_item({..., TeamsPayload: "{ ... }"})

Power Automate (fluxo: Selagem — Alerta Teams)
  └─ Gatilho: novo item na lista
       └─ Ação: Post Adaptive Card no Teams
            └─ Card = triggerOutputs()?['body/TeamsPayload']
```

**Princípio:** quem tem os dados faz o formato. O Power Automate é apenas um relay —
sem condicionais, sem formatação, sem lógica de negócio.
