-- ═══════════════════════════════════════════════════════════════════════════
-- BI Avocado — v3: Preços Europa (CIRAD / FruiTrop)
-- ═══════════════════════════════════════════════════════════════════════════
-- Rodar no SQL Editor do Supabase. Idempotente (IF NOT EXISTS em tudo).
--
-- Substitui o processo atual, que é: o relatório chega por e-mail, alguém
-- salva o PDF à mão, e um PRINT da página é colado no Power BI. Não existe
-- série histórica — só a foto da semana.
--
-- O relatório é um PDF COM CAMADA DE TEXTO. As tentativas anteriores em disco
-- (cirad_extract_weekly.py, CSVs marcados 'page4_chart_digitized') liam PIXEL
-- DE GRÁFICO para recuperar anos anteriores. Isso não é necessário: o número
-- oficial da semana está em tabela na página 1 e sai exato com pdfplumber.
-- Coletando toda semana com upsert, a série se constrói sozinha.
--
-- Validado contra "CIRAD avocado report Week 31-2026.pdf" em 12/08/2026.


-- ── 1. PREÇO DE REFERÊNCIA EU — Hass grade 18 ─────────────────────────────
-- Uma linha por semana. É o KPI da capa do relatório.
--
-- ATENÇÃO à semana: o relatório da semana 31 traz o preço da semana 30. A
-- coluna `semana` é a do PREÇO, não a do relatório — as duas ficam guardadas
-- para dar rastreabilidade. Na virada de ano o relatório da S1 traz preço da
-- S52 do ano anterior, e o ETL resolve o ano por isso.
CREATE TABLE IF NOT EXISTS europa_cirad_precos (
    id                BIGSERIAL   PRIMARY KEY,
    ano               SMALLINT    NOT NULL,
    semana            SMALLINT    NOT NULL,
    grade             TEXT        NOT NULL DEFAULT 'Hass 18',
    preco_eur         NUMERIC(8,2) NOT NULL,
    variacao_eur      NUMERIC(8,2),          -- delta vs semana anterior
    variacao_media_pct NUMERIC(6,1),         -- vs média das 2 safras anteriores
    unidade           TEXT        NOT NULL DEFAULT 'EUR/caixa 4kg',
    relatorio_ano     SMALLINT,
    relatorio_semana  SMALLINT,
    arquivo           TEXT,                  -- nome do PDF de origem
    fonte             TEXT        NOT NULL DEFAULT 'CIRAD / FruiTrop — EU Reference Price',
    extracted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ano, semana, grade)
);

CREATE INDEX IF NOT EXISTS idx_europa_cirad_precos_ano_semana
    ON europa_cirad_precos (ano, semana);


-- ── 2. PREÇO POR CALIBRE E ORIGEM ─────────────────────────────────────────
-- A grade da página 4: Peru, Kenya, África do Sul, e as variedades verdes,
-- por calibre. Este dado NÃO existe no Power BI hoje — o print da capa só
-- mostra o número agregado.
--
-- O relatório cota FAIXA (ex: "9.50/10.50") e às vezes põe entre parênteses
-- cotações excepcionais (ex: "(9.00) 9.50/10.50"). preco_min/preco_max são a
-- faixa principal; texto_original guarda a célula como veio, para quando o
-- formato fugir do padrão (a coluna "Green" mistura dois grupos de calibre na
-- mesma célula).
CREATE TABLE IF NOT EXISTS europa_cirad_calibre (
    id             BIGSERIAL   PRIMARY KEY,
    ano            SMALLINT    NOT NULL,
    semana         SMALLINT    NOT NULL,
    variedade      TEXT        NOT NULL DEFAULT 'Hass',
    origem         TEXT        NOT NULL,
    calibre        TEXT        NOT NULL,
    preco_min      NUMERIC(8,2),
    preco_max      NUMERIC(8,2),
    unidade        TEXT        NOT NULL,     -- 'EUR/caixa 4kg' ou 'EUR/kg'
    texto_original TEXT,
    arquivo        TEXT,
    fonte          TEXT        NOT NULL DEFAULT 'CIRAD / FruiTrop — prices by grade',
    extracted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ano, semana, variedade, origem, calibre)
);

CREATE INDEX IF NOT EXISTS idx_europa_cirad_calibre_ano_semana
    ON europa_cirad_calibre (ano, semana);


-- ── 3. RLS — leitura pública, escrita só com chave secreta ─────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['europa_cirad_precos', 'europa_cirad_calibre'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'leitura_publica_' || t, t);
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR SELECT TO anon, authenticated USING (true)',
            'leitura_publica_' || t, t);
    END LOOP;
END $$;


-- ── 4. VIEW SEMANAL PARA O DASHBOARD ──────────────────────────────────────
-- A aba Europa consome esta. Mantém o mesmo contrato das outras
-- (ano, semana, valor) para o front não precisar de caso especial.
CREATE OR REPLACE VIEW v_europa_cirad_semanal AS
SELECT ano,
       semana,
       grade,
       preco_eur          AS preco,
       variacao_eur,
       variacao_media_pct,
       unidade,
       relatorio_semana,
       arquivo,
       extracted_at
FROM europa_cirad_precos
ORDER BY ano, semana;

COMMENT ON VIEW v_europa_cirad_semanal IS
    'EU Reference Price Hass grade 18, €/caixa 4kg, por semana. A semana é a do '
    'PREÇO, não a do relatório: o relatório da S31 traz o preço da S30.';


-- ── 5. HEARTBEAT ──────────────────────────────────────────────────────────
-- Sem isso, "o cron morreu" e "não saiu relatório novo" ficam iguais na tela.
CREATE OR REPLACE VIEW v_europa_cirad_status AS
SELECT COUNT(*)              AS semanas,
       MIN(ano * 100 + semana) AS primeira,
       MAX(ano * 100 + semana) AS ultima,
       MAX(extracted_at)     AS ultima_atualizacao
FROM europa_cirad_precos;


-- ── 6. CONFERÊNCIA PÓS-CARGA ──────────────────────────────────────────────
-- SELECT * FROM v_europa_cirad_semanal ORDER BY ano DESC, semana DESC LIMIT 10;
-- SELECT * FROM v_europa_cirad_status;
--
-- Esperado depois de carregar o relatório da semana 31/2026:
--   ano=2026 semana=30 grade='Hass 18' preco=10.00 variacao_eur=-0.22
--   variacao_media_pct=18.0 relatorio_semana=31
--   europa_cirad_calibre: 25 linhas (11 calibres x Peru/Kenya/SAR/Green)
