-- ═══════════════════════════════════════════════════════════════════════════
-- BI AVOCADO — TFruits
-- Schema Supabase v2 — todas as fontes
--
-- ADITIVO: não mexe em brasil_precos nem em weeknum_excel(), que já estão em
-- produção com 811 registros. Pode rodar por cima do v1 sem perder nada.
--
-- Rode no SQL Editor. É idempotente.
--
-- Padrão do BI Limão em todas as tabelas: extracted_at, UNIQUE de negócio
-- (upsert idempotente), RLS com leitura pública, entrada na
-- v_ultima_atualizacao.
-- ═══════════════════════════════════════════════════════════════════════════


-- ── 1. PREÇOS DE ORIGEM — Chile, Argentina, Uruguai ───────────────────────
-- Os três coletores (palta_santiago, palta_buenosaires, palta_montevideo)
-- geram o mesmo esquema, mudando só o nome da coluna de câmbio local
-- (clp_rate / blue_rate / uyu_rate). Aqui isso vira uma coluna só,
-- `cotacao_local`, com o par identificado em `cotacao_par`.
CREATE TABLE IF NOT EXISTS precos_origem (
    id               BIGSERIAL     PRIMARY KEY,
    data             DATE          NOT NULL,
    pais             TEXT          NOT NULL,     -- Chile | Argentina | Uruguai
    cidade           TEXT          NOT NULL,     -- Santiago | Buenos Aires | Montevideo
    produto          TEXT          NOT NULL,     -- 'Palta Hass' | 'Palta (Abacate)'
    unidade          TEXT          NOT NULL DEFAULT 'USD/kg',
    preco_min_usd    NUMERIC(10,4),
    preco_max_usd    NUMERIC(10,4),
    preco_medio_usd  NUMERIC(10,4),
    cotacao_par      TEXT,                       -- USD/CLP | USD/ARS blue | USD/UYU
    cotacao_local    NUMERIC(12,4),
    fonte            TEXT,
    ano              SMALLINT      GENERATED ALWAYS AS (EXTRACT(YEAR FROM data)::smallint) STORED,
    semana           SMALLINT      GENERATED ALWAYS AS (weeknum_excel(data)) STORED,
    extracted_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (data, pais, cidade, produto)
);
CREATE INDEX IF NOT EXISTS idx_precos_origem_data     ON precos_origem (data);
CREATE INDEX IF NOT EXISTS idx_precos_origem_pais_sem ON precos_origem (pais, ano, semana);

-- Agregação semanal, no mesmo contrato da view do Brasil.
CREATE OR REPLACE VIEW v_precos_origem_semanal AS
SELECT pais, ano, semana,
       MAX(preco_medio_usd)           AS preco_max,
       ROUND(AVG(preco_medio_usd), 4) AS preco_medio,
       MIN(preco_medio_usd)           AS preco_min,
       COUNT(*)                       AS dias,
       MAX(data)                      AS ultima_data,
       MAX(extracted_at)              AS extracted_at
FROM precos_origem
WHERE preco_medio_usd IS NOT NULL
GROUP BY pais, ano, semana;


-- ── 2. NOTÍCIAS — 7 países × 3 categorias ─────────────────────────────────
-- Os 21 CSVs de news têm o mesmo esquema (data, texto, tag, estado, impacto,
-- fonte_url). Uma tabela só, com país e categoria como colunas — não 21
-- tabelas.
--
-- O UNIQUE usa o md5 do texto porque o texto é longo e a manchete é o que
-- identifica a notícia: rodar o coletor duas vezes no mesmo dia não duplica.
CREATE TABLE IF NOT EXISTS news (
    id            BIGSERIAL     PRIMARY KEY,
    data          DATE          NOT NULL,
    pais          TEXT          NOT NULL,        -- Brasil, Chile, Colômbia, Espanha, Israel, Marrocos, Peru
    categoria     TEXT          NOT NULL,        -- geral | regulacao | logistica
    tag           TEXT,
    impacto       TEXT,                          -- Alto | Médio | Baixo
    estado        TEXT,
    texto         TEXT          NOT NULL,
    fonte_url     TEXT,
    texto_hash    TEXT          GENERATED ALWAYS AS (md5(texto)) STORED,
    extracted_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (pais, categoria, data, texto_hash)
);
CREATE INDEX IF NOT EXISTS idx_news_pais_data ON news (pais, data DESC);
CREATE INDEX IF NOT EXISTS idx_news_categoria ON news (categoria);

COMMENT ON TABLE news IS
    'Notícias por país e categoria. Substitui os 21 CSVs de newsletter_<pais>/. '
    'categoria: geral (news_<pais>.csv), regulacao (regulacao_<pais>.csv), logistica (logistica_<pais>.csv).';


-- ── 3. CÂMBIO ─────────────────────────────────────────────────────────────
-- Os CSVs guardam o par no NOME da coluna (usd_brl, usd_clp, usd_cop...) e
-- uma linha só. Aqui o par é dado, não estrutura — assim dá para acumular
-- histórico e plotar série.
CREATE TABLE IF NOT EXISTS cambio (
    id            BIGSERIAL     PRIMARY KEY,
    data          DATE          NOT NULL,
    par           TEXT          NOT NULL,        -- USD/BRL, USD/CLP, USD/COP, USD/EUR, USD/ILS, USD/MAD, USD/PEN
    valor         NUMERIC(14,6) NOT NULL,
    fonte         TEXT          DEFAULT 'yfinance',
    extracted_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (data, par)
);
CREATE INDEX IF NOT EXISTS idx_cambio_par_data ON cambio (par, data DESC);

-- Última cotação de cada par, para os KPIs do dashboard.
CREATE OR REPLACE VIEW v_cambio_atual AS
SELECT DISTINCT ON (par) par, data, valor, extracted_at
FROM cambio ORDER BY par, data DESC;


-- ── 4. BOLETIM CIRAD / TROPISENS ──────────────────────────────────────────
-- O Tropisens_summary.txt é texto corrido em seções. Guardar por seção
-- permite renderizar sem reparsear e manter o histórico semanal.
CREATE TABLE IF NOT EXISTS cirad_resumo (
    id            BIGSERIAL     PRIMARY KEY,
    ano           SMALLINT      NOT NULL,
    semana        SMALLINT      NOT NULL,
    ordem         SMALLINT      NOT NULL,        -- ordem da seção no boletim
    secao         TEXT          NOT NULL,        -- 'Panorama Geral (Europa)', 'Preços – Mercado Europeu', ...
    texto         TEXT          NOT NULL,
    extracted_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (ano, semana, secao)
);
CREATE INDEX IF NOT EXISTS idx_cirad_ano_sem ON cirad_resumo (ano DESC, semana DESC);


-- ── 5. LOGÍSTICA — rotas (dado estático, do estudo TFruits) ───────────────
CREATE TABLE IF NOT EXISTS logistica_rotas (
    id                 BIGSERIAL   PRIMARY KEY,
    pais_origem        TEXT        NOT NULL,
    porto_origem       TEXT        NOT NULL,
    rota               TEXT,
    pais_destino       TEXT        NOT NULL,
    porto_destino      TEXT        NOT NULL,
    modal              TEXT        NOT NULL DEFAULT 'Marítimo',
    transit_time_dias  NUMERIC(5,1),
    armador            TEXT,
    obs                TEXT,
    fonte              TEXT,
    extracted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pais_origem, porto_origem, pais_destino, porto_destino, modal)
);


-- ── 6. JANELA DE PRODUÇÃO MUNDIAL (dado estático) ─────────────────────────
CREATE TABLE IF NOT EXISTS janela_producao (
    id            BIGSERIAL   PRIMARY KEY,
    pais          TEXT        NOT NULL,
    mes_num       SMALLINT    NOT NULL CHECK (mes_num BETWEEN 1 AND 12),
    mes_nome      TEXT        NOT NULL,
    nivel         SMALLINT    NOT NULL CHECK (nivel BETWEEN 0 AND 3),
    ordem_pais    SMALLINT,
    extracted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pais, mes_num)
);


-- ── 7. CLIMA — ainda sem coletor ──────────────────────────────────────────
-- Tabela criada vazia de propósito: o dashboard já consulta e mostra "sem
-- dado" em vez de quebrar quando o coletor entrar.
CREATE TABLE IF NOT EXISTS clima (
    id            BIGSERIAL     PRIMARY KEY,
    data          DATE          NOT NULL,
    pais          TEXT          NOT NULL,
    cidade        TEXT          NOT NULL,
    temp_max      NUMERIC(5,2),
    temp_min      NUMERIC(5,2),
    chuva_mm      NUMERIC(7,2),
    fonte         TEXT,
    extracted_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (data, cidade)
);
CREATE INDEX IF NOT EXISTS idx_clima_cidade_data ON clima (cidade, data DESC);

-- Acumulado de 7 dias por cidade — é o que a aba Newsletters mostra.
CREATE OR REPLACE VIEW v_clima_acumulado_7d AS
SELECT pais, cidade,
       SUM(chuva_mm)  AS acumulado_7d_mm,
       MAX(temp_max)  AS temp_max_7d,
       MIN(temp_min)  AS temp_min_7d,
       MAX(data)      AS ultima_data
FROM clima
WHERE data >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY pais, cidade;


-- ── 8. COMEXSTAT / SHARE BRASIL — ainda sem coletor ───────────────────────
CREATE TABLE IF NOT EXISTS comexstat (
    id             BIGSERIAL     PRIMARY KEY,
    ano            SMALLINT      NOT NULL,
    mes            SMALLINT      NOT NULL CHECK (mes BETWEEN 1 AND 12),
    pais_destino   TEXT          NOT NULL,
    ncm            TEXT          NOT NULL DEFAULT '08044000',
    peso_kg        NUMERIC(16,2),
    valor_fob_usd  NUMERIC(16,2),
    containers     INTEGER,
    extracted_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (ano, mes, pais_destino, ncm)
);
CREATE INDEX IF NOT EXISTS idx_comexstat_ano_mes ON comexstat (ano, mes);


-- ── 9. RLS: LEITURA PÚBLICA EM TUDO ───────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['precos_origem','news','cambio','cirad_resumo',
                             'logistica_rotas','janela_producao','clima','comexstat']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'leitura_publica_'||t, t);
        EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING (true)', 'leitura_publica_'||t, t);
    END LOOP;
END $$;


-- ── 10. STATUS DE TODAS AS TABELAS ────────────────────────────────────────
CREATE OR REPLACE VIEW v_ultima_atualizacao AS
SELECT 'brasil_precos'   AS tabela, MAX(extracted_at) AS ultima_atualizacao, COUNT(*) AS total_registros FROM brasil_precos
UNION ALL SELECT 'precos_origem',   MAX(extracted_at), COUNT(*) FROM precos_origem
UNION ALL SELECT 'news',            MAX(extracted_at), COUNT(*) FROM news
UNION ALL SELECT 'cambio',          MAX(extracted_at), COUNT(*) FROM cambio
UNION ALL SELECT 'cirad_resumo',    MAX(extracted_at), COUNT(*) FROM cirad_resumo
UNION ALL SELECT 'logistica_rotas', MAX(extracted_at), COUNT(*) FROM logistica_rotas
UNION ALL SELECT 'janela_producao', MAX(extracted_at), COUNT(*) FROM janela_producao
UNION ALL SELECT 'clima',           MAX(extracted_at), COUNT(*) FROM clima
UNION ALL SELECT 'comexstat',       MAX(extracted_at), COUNT(*) FROM comexstat;

COMMENT ON VIEW v_ultima_atualizacao IS
    'Status dos ETLs. clima e comexstat ficam em zero até os coletores existirem.';
