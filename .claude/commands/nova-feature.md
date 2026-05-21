# /nova-feature

Skill de workflow para implementar uma nova feature seguindo os padrões do projeto de
mensageria preditiva (Seeq → Algoritmo → SharePoint → Teams).

Este projeto roda no **Seeq Data Lab** — `spy` já está autenticado, sem necessidade de login.

---

## Como usar esta skill

Quando o usuário invocar `/nova-feature [nome-da-feature]`, execute o workflow abaixo.
Se o nome não for fornecido, pergunte antes de começar.

---

## Passo 1 — Criar a branch

```bash
git checkout main
git pull origin main
git checkout -b feature/[nome-da-feature]
```

Convenção de nomes de branch:
- `feature/nome-curto` — nova funcionalidade
- `fix/descricao-do-bug` — correção de bug
- `refactor/escopo` — refatoração sem mudança de comportamento
- `experiment/hipotese` — experimento descartável

---

## Passo 2 — Definir o escopo antes de implementar

Antes de criar qualquer arquivo, confirme em uma frase:

> "Vou implementar [X] que faz [Y], sem alterar [Z]."

Regras de scope discipline:
- Não introduzir separações estruturais (dividir módulos, criar abstrações) sem confirmação explícita
- Não refatorar código adjacente ao escopo
- Não inferir requisitos não mencionados — perguntar

---

## Passo 3 — Estrutura de arquivos

### Para um novo gerador de dados (extrai dados do Seeq):
```
src/generators/gen_[nome].py    # função run() padronizada
notebooks/[N]_[nome].ipynb      # notebook exploratório (opcional)
```

### Para uma nova análise ou algoritmo:
```
src/[nome_algoritmo].py         # módulo com função principal
notebooks/[N]_[nome].ipynb      # notebook de desenvolvimento/validação
```

### Para um novo tipo de gatilho ou regra de negócio:
```
src/trigger_engine.py           # adicionar ao motor existente (não criar novo arquivo)
config.example.yaml             # documentar os novos parâmetros com valores padrão
```

### Para um novo tipo de card ou saída visual:
```
src/card_formatter.py           # adicionar nova função build_*_card()
```

---

## Passo 4 — Padrões de implementação

### Gerador de dados (gen_*.py)
Todo gerador deve ter a assinatura:
```python
def run(
    output_path: str | Path = _ROOT / "notebooks" / "[nome].csv",
    config_path: str | Path | None = None,
) -> pd.DataFrame:
    """Descrição em uma linha do que extrai."""
    ...
    df.to_csv(output_path, index=False)
    return df
```

### Seeq Data Lab — acesso a `spy`
- `spy` já está autenticado — nunca chamar `spy.login()`
- Para descobrir sinais: `spy.search(worksheet_url)` (só metadados, sem risco de PIException)
- Para puxar dados: `spy.pull(items_df, header="ID")` com IDs explícitos confirmados
- Nunca usar `spy.pull(worksheet_url)` direto para descoberta

### Timestamps
- **Sempre UTC-aware**: `pd.to_datetime(col, utc=True)`
- Nunca misturar tz-aware e tz-naive no mesmo DataFrame
- Usar `pd.Timestamp.now(tz="UTC")` para timestamps do momento atual

### Parâmetros numéricos
- Nunca hardcode de limiares no código — sempre ler de `config.yaml`
- Adicionar os novos parâmetros ao `config.example.yaml` com comentário explicativo

### Credenciais
- SharePoint: sempre ler de arquivo `.ev` via `dotenv`
- Nunca commitar arquivo `.ev` ou qualquer arquivo com credenciais

### Integração SharePoint durante desenvolvimento
- Usar lista de teste dedicada ou prefixo `[TESTE]` nos títulos dos itens
- Ver `/testar-sp` para o workflow completo de teste isolado

---

## Passo 5 — Checklist antes do commit

```
[ ] Novos parâmetros numéricos adicionados ao config.example.yaml
[ ] Nenhum hardcode de credenciais, URLs de produção ou limiares
[ ] Timestamps todos UTC-aware
[ ] Nenhum arquivo .ev, config.yaml, *.csv, state_*.json staged
[ ] Mensagem de commit descreve a mudança específica (não "update")
[ ] Nenhuma célula de notebook acidentalmente comentada
[ ] Após editar módulo Python importado por notebook: reiniciar kernel ou importlib.reload()
```

Verificar arquivos staged antes de commitar:
```bash
git status
git diff --staged --name-only
```

---

## Passo 6 — Commit e push

```bash
git add src/[arquivos relevantes]
git add config.example.yaml    # se houver novos parâmetros
git add notebooks/[novo_notebook].ipynb

# Nunca: git add -A ou git add .  (pode incluir config.yaml, .ev, CSVs)

git commit -m "feat([escopo]): [descrição específica da mudança]"
git push origin feature/[nome-da-feature]
```

---

## Passo 7 — Pull Request

O PR deve ir para `main`.

Descrição do PR deve incluir:
1. O que a feature faz e por que foi implementada
2. Como testar no Data Lab (qual notebook executar, o que verificar na saída)
3. Parâmetros novos no config.yaml e seus valores padrão
4. Se há migração de estado (`state_*.json`) necessária
