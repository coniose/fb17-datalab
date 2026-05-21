# /testar-sp

Skill para testar integrações com SharePoint de forma isolada, sem poluir dados de produção.

Resolve o problema histórico de arquivos de teste espalhados pelo projeto com risco de deletar
algo que outro script depende.

---

## Filosofia de testes SharePoint

**Produção** = lista `Gatilhos_Selagem` (ou qualquer lista de produção), sem prefixo nos títulos.
**Teste** = mesma lista, mas itens com `[TESTE]` no título, OU lista separada `Gatilhos_Selagem_DEV`.

Todo item de teste deve poder ser removido com um único comando sem impacto em produção.

---

## Como usar esta skill

Quando o usuário invocar `/testar-sp`, pergunte:

1. O que está sendo testado? (card novo, novo gatilho, nova coluna, integração completa)
2. Qual lista SharePoint? (produção ou lista de desenvolvimento)
3. Quantos itens de teste precisam ser criados?

Então execute o workflow adequado abaixo. Todo código deve ser executado em célula de notebook
no Seeq Data Lab — nunca como script Python avulso no terminal.

---

## Workflow 1 — Inserir item de teste com prefixo [TESTE]

Use quando quiser validar que o card chega no Teams ou que a escrita na lista funciona.

```python
# Rodar em célula de notebook no Seeq Data Lab
import sys
sys.path.insert(0, '..')
from dotenv import dotenv_values
from src.sharepoint_methods import SharePointClient
from src.card_formatter import build_alert_card
from datetime import datetime

# Carregar credenciais do arquivo .ev (nunca hardcode)
creds = dotenv_values('sharepoint.ev')          # raiz do projeto
sp    = SharePointClient(creds['SP_URL'], creds['SP_USER'], creds['SP_PASS'])

# Construir payload de teste
payload = build_alert_card(
    maquina          = "TEST",
    gatilho          = "RED",          # ou AMARELO / EMERGENCIA
    idade_dias       = 27,
    p_risk           = 0.63,
    slope_7d         = -8.2,
    forca_min_3d     = 812.0,
    proj_48h         = 870.0,
    acao_recomendada = "Item de teste — ignorar. Pode deletar.",
    data_disparo     = datetime.now(),
)

item = {
    "Title":       f"[TESTE] FB14 | RED | {datetime.now():%Y%m%d-%H%M}",
    "Maquina":     "TEST",
    "TeamsPayload": payload,
}

ids = sp.insert_list_item("Gatilhos_Selagem", [item])
print(f"Item de teste criado — ID: {ids[0]}")
print("Aguarde ~30s e verifique o canal Teams.")
print(f"Para remover: sp.delete_list_item('Gatilhos_Selagem', {ids[0]})")
```

---

## Workflow 2 — Limpar itens de teste da lista

Remove todos os itens com `[TESTE]` no título. Seguro para produção.

```python
# Rodar em célula de notebook no Seeq Data Lab
import sys
sys.path.insert(0, '..')
from dotenv import dotenv_values
from src.sharepoint_methods import SharePointClient

creds = dotenv_values('sharepoint.ev')
sp    = SharePointClient(creds['SP_URL'], creds['SP_USER'], creds['SP_PASS'])

# Listar todos os itens de teste
items = sp.get_list_items("Gatilhos_Selagem", filter="startswith(Title, '[TESTE]')")
ids   = [it["ID"] for it in items]

if not ids:
    print("Nenhum item de teste encontrado.")
else:
    print(f"Encontrados {len(ids)} itens de teste: {ids}")
    for item_id in ids:
        sp.delete_list_item("Gatilhos_Selagem", item_id)
        print(f"  Deletado: ID={item_id}")
    print("Limpeza concluída.")
```

---

## Workflow 3 — Simular o pipeline completo em modo dry-run

Roda o `TriggerEngine` com dados reais mas sem escrever no SharePoint.

```python
# Rodar em célula de notebook no Seeq Data Lab
import sys
sys.path.insert(0, '..')
from pathlib import Path
from src.trigger_engine import TriggerEngine
import pandas as pd

NB_DIR = Path(".")  # dentro de notebooks/

df_hourly = pd.read_csv(NB_DIR / "00_hour_prev.csv", parse_dates=["Timestamp"])
df_hourly["Timestamp"] = pd.to_datetime(df_hourly["Timestamp"], utc=True)
df_hourly = df_hourly.set_index("Timestamp").sort_index()

engine = TriggerEngine(maquina="FB14", state_path=NB_DIR / "state_fb14_test.json")

# sp_client=None → dry-run (nada é escrito no SP)
events = engine.evaluate(
    df_hourly  = df_hourly,
    troca_date = pd.Timestamp("2026-05-06", tz="UTC"),
    sp_client  = None,
    list_name  = "Gatilhos_Selagem",
)

if events:
    for ev in events:
        print(f"[{ev.gatilho}] {ev.severidade}")
        print(f"  {ev.mensagem.splitlines()[0]}")
else:
    print("Nenhum gatilho disparado (dry-run).")
```

> Use `state_fb14_test.json` (arquivo separado) para não contaminar o estado de produção
> em `state_fb14.json`.

---

## Checklist antes de testar com SharePoint real

```
[ ] Credenciais carregadas de .ev (nunca hardcode)
[ ] Título do item inclui prefixo [TESTE] ou timestamp para identificação
[ ] Lista alvo está correta (não confundir DEV com produção)
[ ] Sabe como deletar os itens após o teste (ID retornado por insert_list_item)
[ ] Power Automate está configurado para não enviar [TESTE] ao Teams OU você quer ver o card
```

---

## Regra de ouro

> Se você não sabe se pode deletar o arquivo/item, é porque o arquivo/item não foi criado
> com isolamento suficiente. Refaça o teste usando este workflow.
