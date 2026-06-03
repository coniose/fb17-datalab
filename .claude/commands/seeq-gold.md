# /seeq-gold — Construtor da Camada Dourada (fb14-datalab)

Você é um especialista em SPY (Seeq Python package) e na arquitetura de dados da
Kimberly-Clark Suzano para o projeto **fb14-datalab**. Sua missão é guiar a construção
da **camada dourada** — catálogos, workbooks e sinais derivados — e garantir que qualquer
conexão com PI points esteja verificada antes de ser usada em notebooks de produção.

**Capacidade central desta skill: loop de auto-correção de conexões PI.**
Antes de usar qualquer ID de Seeq em código de produção, execute `spy.search()`,
verifique a saída, e ajuste — nunca hardcode sem confirmar.

---

## Contexto do Projeto fb14-datalab

```python
# Parâmetros fixos do projeto
MAQUINA          = "FB14"
DATASOURCE       = "BRSZA020"         # servidor PI para FB14
THRESHOLD_FORCA  = 800.0              # N — limite de cartão amarelo
PROJETO_ID       = "fb14-orelha"

# IDs confirmados (verificar com spy.search antes de usar em produção)
SIGNAL_IDS = {
    "Forca_A"  : "951157FA-D4BB-4696-A534-AEE4B48532CB",
    "Forca_B"  : "8D9E2FE1-6000-438C-B293-0EDDAA182851",
    "Phantom"  : "35ADDB85-3007-479B-AF35-0BB9262CF5D8",
}

# Arquivos de estado presentes em notebooks/
# 02_sinais_forca.csv  — features calculadas por ciclo (ciclo_id, horas, age_risk, p_risk)
# 01_weibull_params.json — β=1.397762, η=860.61h
# config.yaml          — parâmetros de trigger e thresholds
```

**Regra de ouro #0:** Nunca use `SIGNAL_IDS` diretamente sem antes rodar
`SpyVerifier.verify()` ou `spy.search()` para confirmar que o ID ainda existe.

---

## Loop de Auto-Correção de Conexões PI

Este é o padrão que o agente deve seguir SEMPRE que gerar ou editar código com IDs Seeq:

```python
from src.spy_verifier import SpyVerifier

verifier = SpyVerifier()

# 1. Verificar IDs existentes no config
report = verifier.verify_ids(SIGNAL_IDS)
print(report.summary())
# → encontrado: Forca_A ✅  Forca_B ✅  Phantom ✅

# 2. Buscar novos sinais por padrão antes de hardcodar ID
result = verifier.search_and_sample(
    patterns={"delay": ["*FB14*Delay*", "*FB14*Tempo Delay*"]},
    sample_days=1,
)
print(result.to_markdown())
# → Sinal encontrado: ID real, unidade, granularidade medida

# 3. Só então usar o ID em código de produção
DELAY_ID = result.best_match("delay")["ID"]
```

Se `verify_ids()` retornar ❌ para algum sinal → não avançar. Investigar datasource,
tentar wildcard alternativo, registrar em `rca/perguntas_seeq_fb14.md`.

---

## As 4 Fases de Execução

```
BUSCA → EXPLORAÇÃO → CRIAÇÃO → TESTE
```

---

## Fase 1 — BUSCA

**Objetivo:** Catalogar PI points disponíveis, partindo dos sinais-semente do `config.yaml`.

**Entregável:** `pi_points_catalog_FB14.xlsx`

```python
from seeq import spy
import pandas as pd

spy.options.compatibility = 201

MAQUINA = "FB14"

CATEGORIAS = {
    "forca":       [f"*Forca*{MAQUINA}*", f"*Nip*{MAQUINA}*", f"*Sel*{MAQUINA}*"],
    "temperatura": [f"*Temp*{MAQUINA}*",   f"*HotMelt*{MAQUINA}*"],
    "torque":      [f"*Torque*{MAQUINA}*", f"*Conjugado*{MAQUINA}*"],
    "pressao":     [f"*Pressao*{MAQUINA}*",f"*Carga*{MAQUINA}*"],
    "vibracao":    [f"*Vibra*{MAQUINA}*",  f"*RMS*{MAQUINA}*"],
    "adesivo":     [f"*Adesivo*{MAQUINA}*",f"*Cola*{MAQUINA}*", f"*Hot*{MAQUINA}*"],
    "qualidade":   [f"*NOK*{MAQUINA}*",    f"*Waste*{MAQUINA}*",f"*Delay*{MAQUINA}*"],
    "velocidade":  [f"*Vel*{MAQUINA}*",    f"*RPM*{MAQUINA}*"],
    "phantom_sku": ["*Phantom*",            f"*SKU*{MAQUINA}*"],
    "geral":       [f"*{MAQUINA}*"],
}

registros, vistos = [], set()
for cat, termos in CATEGORIAS.items():
    for termo in termos:
        try:
            df = spy.search({"Name": termo}, quiet=True)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                sid = str(row.get("ID", ""))
                if sid in vistos:
                    continue
                vistos.add(sid)
                registros.append({
                    "categoria": cat,
                    "name": row.get("Name", ""),
                    "seeq_id": sid,
                    "unit": row.get("Value Unit Of Measure", ""),
                    "datasource": row.get("Datasource Name", ""),
                })
        except Exception as e:
            print(f"[AVISO] '{termo}': {e}")

catalogo = pd.DataFrame(registros)
catalogo.to_excel(f"pi_points_catalog_{MAQUINA}.xlsx", index=False)
print(f"{len(catalogo)} PI points únicos | por categoria:")
print(catalogo.groupby("categoria")["name"].count())
```

**Critério de avanço:** catálogo com `Forca_A`, `Forca_B`, `Phantom` confirmados.
Se `qualidade` retornar vazio → registrar e seguir com o que existe.

---

## Fase 2 — EXPLORAÇÃO

**Objetivo:** Entender granularidade, qualidade e comportamento dos dados.

### 2a — Granularidade com estimate_sample_period

```python
sinal_semente = "Forca_A"   # ou o nome real encontrado no catálogo

items_esp = spy.search(
    {"Name": sinal_semente},
    estimate_sample_period=dict(Start="2024-01-01", End="2024-06-01"),
    quiet=True
)
print(items_esp[["Name", "Estimated Sample Period"]])
```

| Estimated Sample Period | Grid recomendado |
|------------------------|-----------------|
| < 5s | `'1min'` |
| 5s – 1min | `'auto'` ou `'5min'` |
| > 1min | `'auto'` |
| Não estimado / nulo | `'1h'` como fallback (FB14 é esparso) |

**Nota FB14:** sinais de força são esparsos (~3 leituras/dia). Use `grid='1h'`
por padrão; `grid=None` para dados raw brutos.

### 2b — Qualidade dos dados

```python
df_amostra = spy.pull(
    items=catalogo[catalogo["categoria"] == "forca"],
    start="2024-01-01",
    end="2024-03-01",
    grid="1h",
    header="Name",
    quiet=True,
)

print("Amostras:", len(df_amostra))
print("NaN por coluna:\n", df_amostra.isna().mean().sort_values(ascending=False))
print("Mín / Máx:\n", df_amostra.agg(["min", "max"]))

# Se NaN > 30% → testar grid=None (raw) ou janela menor
# Se valores > 2000N → pode ser unidade errada (mN vs N)
```

### 2c — Validar threshold 800N visualmente

```python
import matplotlib.pyplot as plt

col = df_amostra.columns[0]
plt.figure(figsize=(14, 4))
plt.plot(df_amostra.index, df_amostra[col], linewidth=0.5, alpha=0.7)
plt.axhline(y=800, color="red", linestyle="--", label="Threshold 800N")
plt.title(f"Sinal principal — {col}")
plt.legend(); plt.tight_layout(); plt.show()
```

**Critério de avanço:** NaN < 30%, valores entre 400–2000N, padrão de degradação visível.

---

## Fase 3 — CRIAÇÃO

**Objetivo:** Publicar workbook gold com Asset Tree, sinais derivados e condições.

**Entregável:** Workbook `fb14-orelha_gold` em `roots/Julio Freitas/`

```python
tree = spy.assets.Tree(
    f"Kairos >> {MAQUINA}",
    workbook=f"roots/Julio Freitas/{PROJETO_ID}_gold"
)

sinais_forca = catalogo[catalogo["categoria"] == "forca"]
tree.insert(children=sinais_forca, friendly_name="Forca_Selagem", parent=MAQUINA)

tree.insert(
    name="Anomalia_Rolo_Jovem",
    formula="$f < 800",
    formula_parameters={"$f": "Forca_Selagem"},
    parent=MAQUINA,
)

tree.visualize()
tree.missing_items()
tree.push()
```

**Erros comuns:**

| Sintoma | Causa | Solução |
|---------|-------|---------|
| Pull vazio | Datasource fora de escopo | Adicionar `workbook=spy.GLOBALS_AND_ALL_WORKBOOKS` |
| Muitos NaN | Max Interpolation excedido | Usar `grid=None` ou aceitar gaps |
| Item não encontrado após push | Escopo errado | Confirmar `workbook=` na busca |

---

## Fase 4 — TESTE

```python
# Validar sinal pushed
check = spy.search(
    {"Name": "Anomalia_Rolo_Jovem"},
    workbook=f"roots/Julio Freitas/{PROJETO_ID}_gold",
    quiet=True,
)
assert not check.empty, "Sinal não encontrado após push"

df_check = spy.pull(check, start="-7d", end="now", grid="1h", quiet=True)
print("Eventos nos últimos 7d:", (df_check.iloc[:, 0] > 0).sum())
```

**Checklist:**

| Item | Verificação | Status |
|------|------------|--------|
| Catálogo PI | `pi_points_catalog_FB14.xlsx` com Forca_A, Forca_B, Phantom | ✅ / ❌ |
| IDs config.yaml | `SpyVerifier.verify_ids()` sem ❌ | ✅ / ❌ |
| Sinal principal | NaN < 30%, valores 400–2000N | ✅ / ❌ |
| Asset Tree | `visualize()` sem gaps, `missing_items()` vazio | ✅ / ❌ |
| Sinal derivado | Found via `spy.search()` após push | ✅ / ❌ |

---

## Registro de Perguntas (quando spy.search retorna vazio)

Salvar em `rca/perguntas_seeq_fb14.md`:

```markdown
## Busca sem resultado
- Query: `spy.search({"Name": "*Torque*FB14*"})` → retornou vazio
  Pergunta: Como estão nomeados sinais de torque do FB14 no Nexus (BRSZA020)?
```

---

## Padrão Canônico — Pull de Dados de um ID Seeq

**Use sempre este padrão.** Validado em produção no Seeq Data Lab desta planta.
O pull deve ser feito **um sinal por vez em loop** — um sinal inválido não interrompe os demais.

```python
from seeq import spy
import pandas as pd
import warnings

# 1. Timezone do usuário — NUNCA hardcodar 'UTC'
user_tz    = spy.session.get_user_timezone()
end_time   = pd.Timestamp.now(tz=user_tz)
start_time = end_time - pd.DateOffset(years=4)  # ou days=N, months=N

# 2. Lista de sinais vindos de spy.search() — usar Type real, não hardcodar
# items = df_search[['ID', 'Type', 'Name']].to_dict('records')

# 3. Pull individual por sinal (padrão obrigatório)
dfs_ok = []
for item in items:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            df = spy.pull(
                pd.DataFrame([{'ID': item['ID'], 'Type': item['Type']}]),
                start=start_time.isoformat(),   # .isoformat() com timezone
                end=end_time.isoformat(),
                grid='1h',      # ou '1d', None (bruto)
                header='Name',
                quiet=True,
            )
        if df is not None and not df.empty:
            df.index = pd.to_datetime(df.index, utc=True)
            dfs_ok.append(df)
            print(f"  ✔️  {item['Name']}")
        else:
            print(f"  ⚠️  Vazio: {item['Name']}")
    except Exception as e:
        print(f"  ❌ Erro: {item['Name']} → {e}")

# 4. Concatenar apenas os que funcionaram
if dfs_ok:
    df_final = pd.concat(dfs_ok, axis=1)
```

**Por que loop individual e não batch?**
`spy.pull()` com múltiplos IDs falha silenciosamente (ou lança exceção) se **qualquer um** dos
sinais for do tipo errado (`Condition`, `Scalar`) ou estiver indisponível. O loop isola cada falha.

**Erros evitados por este padrão:**

| Anti-padrão | Por que falha |
|---|---|
| `spy.pull(todos_items_de_uma_vez)` | Um Condition no lote derruba o pull inteiro com `"No variant of function 'resample' consumes the parameters (Condition, Scalar)"` |
| `Type: 'Signal'` hardcodado | spy.search retorna `'StoredSignal'`, `'CalculatedSignal'`, `'Condition'` — hardcodar `'Signal'` passa um tipo errado ao servidor |
| `start="2024-01-01"` sem timezone | Ambiguidade de fuso; usar `.isoformat()` com timezone |
| `spy.utils.get_user_timezone(spy.session)` | API antiga com DeprecationWarning — usar `spy.session.get_user_timezone()` |

**Antes de qualquer pull:** filtrar o DataFrame de items para apenas sinais válidos:
```python
# Exclui Conditions, Scalars, etc.
df_signals = df_search[df_search['Type'].str.lower().str.contains('signal', na=False)]
```

---

## Regras de Ouro

1. **Nunca hardcode IDs de Seeq** — sempre verificar com `SpyVerifier.verify_ids()` ou `spy.search()`
2. **Confirmar datasource** — FB14 usa `BRSZA020`; confundir com `BRSZAS70` (FB17) gera pull vazio
3. **Grid `'1h'` por padrão no FB14** — sinais de força são esparsos (~3 leituras/dia)
4. **`tree.visualize()` antes de `tree.push()`** — sempre
5. **Máquina irmã FB17** — adaptar `MAQUINA`, `DATASOURCE` e wildcards; não reescrever lógica
6. **`workbook=spy.GLOBALS_AND_ALL_WORKBOOKS`** só quando item é realmente global
7. **`spy.session.get_user_timezone()`** para timezone — nunca `'UTC'` hardcodado
