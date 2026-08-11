# BI Avocado — TFruits

Dashboard de inteligência de mercado do abacate Hass. Conversão do relatório
Power BI `Reporte Avocado_vfinal.pbix` (16 páginas) para HTML single-file, com
coleta automatizada e histórico no Supabase.

Segue o mesmo modelo do **BI Limão**: `supabase_upsert.py` por REST, tabelas com
`extracted_at` + `UNIQUE` de negócio, RLS de leitura pública, view
`v_ultima_atualizacao` para status, ETLs em GitHub Actions com cron.

---

## Estado atual

| Aba | Fonte | Situação |
|---|---|---|
| Preços Brasil | Supabase `brasil_precos` | **automatizado** — histórico carregado + coleta diária |
| Preços Chile | `Preco_historico.xlsx` + `palta_santiago_precos.csv` | embutido no HTML, auditoria pendente |
| Preços Uruguai / Argentina | `Consolidado_forecast.xlsx` + `palta_buenosaires_precos.csv` | embutido, auditoria pendente |
| Preços Europa / CIRAD | boletim CIRAD | sem fonte estruturada |
| Newsletters Brasil / Chile | `news_*.csv` | embutido, coletores existem mas rodam na mão |
| Newsletters Marrocos / Israel / Colômbia / Espanha / Peru | — | sem coletor |
| Clima | — | sem coletor (o BI Limão tem `etl_clima_openweather.py` para reaproveitar) |
| Transit Time | `TFRUITS Estudo de rotas.xlsx` | embutido, é dado estático |
| Share Brasil | — | sem coletor (o BI Limão tem `etl_comexstat.py` para adaptar ao NCM 0804.40.00) |
| Janela de Produção | `janela_avocado_ordenada_producao_ordempais.csv` | embutido; faltam Chile e EUA no CSV |

O `index.html` tenta ler o Supabase e, se não conseguir, usa o bloco de dados
embutido. Nunca abre em branco.

---

## Montagem, na ordem

### 1. Criar o projeto no Supabase

Projeto novo, dedicado ao BI Avocado. Guarde as três credenciais em
**Project Settings → API**:

| Credencial | Onde vai |
|---|---|
| Project URL | secret `SUPABASE_URL` e constante `SUPA_URL` no `index.html` |
| `anon` key | constante `SUPA_ANON` no `index.html` (só leitura, pode ir no front) |
| `service_role` key | secret `SUPABASE_KEY` — **nunca** no `index.html` |

### 2. Criar o schema

SQL Editor → cole o conteúdo de `supabase_schema.sql` → Run. Cria a função
`weeknum_excel`, a tabela `brasil_precos`, a view `v_brasil_precos_semanal`, a
policy de leitura pública e a view de status. É idempotente.

### 3. Carregar o histórico 2024–2025

```bash
cp .env.example .env          # preencha com URL + service_role key
pip install -r requirements.txt
python import_historico_brasil.py --arquivo /caminho/Brasil_historico.xlsx
```

O script confere quatro âncoras da auditoria antes de gravar (R$ 23,33 em
10/11/2024, R$ 21,64 em 07/04/2025, R$ 18,57 em 05/12 e R$ 16,81 em 10/12). Se
alguma não fechar, ele aborta em vez de gravar dado errado. Rode uma vez só.

### 4. Primeira coleta de 2026

```bash
python etl_brasil_precos.py --dry-run    # confere o scraping sem gravar
python etl_brasil_precos.py              # grava
```

### 5. Conferir

```sql
SELECT ano, semana, preco_max FROM v_brasil_precos_semanal
 WHERE (ano, semana) IN ((2024,1),(2024,46),(2024,49),(2025,15),(2026,6))
 ORDER BY ano, semana;
-- esperado: 6.54 | 23.33 | 18.57 | 21.64 | 12.51
```

### 6. Automatizar

No GitHub: **Settings → Secrets and variables → Actions → New repository secret**

- `SUPABASE_URL`
- `SUPABASE_KEY` (service_role)

O workflow `.github/workflows/etl_precos.yml` roda de segunda a sábado às 09:00
BRT. Dá para disparar na mão em **Actions → ETL Preços — Brasil → Run workflow**,
com a opção `dry_run` para testar sem gravar.

### 7. Ligar o dashboard no banco

No `index.html`, no topo do `<script>`:

```js
const SUPA_URL  = 'https://SEU_PROJETO.supabase.co';
const SUPA_ANON = 'eyJhbGciOi...';   // anon key
```

Enquanto estiverem com o valor `PREENCHER`, o dashboard usa os dados embutidos
sem tentar a rede. O rodapé da sidebar mostra qual fonte está em uso.

### 8. Deploy

Vercel apontando para este repo, output estático. O `index.html` é
self-contained — não tem build.

---

## Arquivos

| Arquivo | O que faz |
|---|---|
| `index.html` | O dashboard. Single-file, Chart.js via CDN. |
| `supabase_schema.sql` | DDL completo da fatia de Preços Brasil. |
| `supabase_upsert.py` | Helper de upsert por REST. Cópia do helper do BI Limão. |
| `import_historico_brasil.py` | Carga única do `Brasil_historico.xlsx`. |
| `etl_brasil_precos.py` | Coleta diária da Notícias Agrícolas. |
| `etl_master.py` | Orquestrador, para quando houver mais de um ETL. |
| `.github/workflows/etl_precos.yml` | Cron + issue automática na falha. |

---

## Duas decisões que valem saber

**A semana não é ISO.** O Power BI agrupa por `WEEKNUM` do Excel — semana
começando no domingo, ano com 53 semanas. Isso foi comprovado contra os rótulos
do relatório: o pico de R$ 23,33 é 10/11/2024, que cai na S46 nessa convenção e
na S45 em ISO; o relatório mostra S46. A regra vive na função
`weeknum_excel()` do Postgres e em colunas geradas, então a semana nunca pode
divergir da data. A mesma função está replicada em Python no
`import_historico_brasil.py`, e as duas foram conferidas contra 832 datas reais.

**O ETL virou upsert, não append.** O coletor antigo era append-only com dedupe
por `Data+CEASA+Produto`, ou seja: um preço corrigido na fonte depois da
primeira coleta nunca entrava. É a explicação mais provável para a única
divergência que sobrou na auditoria — o relatório mostra R$ 6,52 na S23/2026
quando o máximo real da semana é R$ 6,60. Com `UNIQUE (data, mercado, produto)` +
`Prefer: resolution=merge-duplicates`, a última coleta vence e correções entram.
