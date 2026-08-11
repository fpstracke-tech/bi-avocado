-- ═══════════════════════════════════════════════════════════════════════════
-- BI AVOCADO — TFruits
-- Schema Supabase — fatia 1: Preços Brasil
--
-- Rode no SQL Editor do projeto Supabase do BI Avocado.
-- É idempotente: pode rodar de novo sem quebrar nada.
--
-- Padrão herdado do BI Limão: extracted_at + UNIQUE por chave de negócio,
-- RLS com leitura pública (anon key no frontend) e view v_ultima_atualizacao
-- alimentando a tela de status.
-- ═══════════════════════════════════════════════════════════════════════════


-- ── 1. SEMANA NO PADRÃO WEEKNUM DO EXCEL ──────────────────────────────────
-- O Power BI original agrupa por WEEKNUM do Excel: a semana começa no DOMINGO
-- e o ano tem 53 semanas. Não é ISO 8601. Isso foi comprovado contra os
-- rótulos do relatório (auditoria de 11/08/2026):
--
--   R$ 23,33 em 10/11/2024  -> S46 aqui   | ISO daria S45  (relatório mostra 46)
--   S1/2024                 -> R$ 6,54    | ISO daria 11,22 (não existe no relatório)
--   R$ 18,57 em 05/12/2024  -> S49        | confere
--   R$ 16,81 em 10/12/2024  -> S50        | confere
--
-- A função é IMMUTABLE de propósito: é usada em coluna gerada, e assim a
-- semana nunca pode divergir da data. EXTRACT(DOW) devolve 0 para domingo.
CREATE OR REPLACE FUNCTION weeknum_excel(d DATE)
RETURNS SMALLINT
LANGUAGE sql
IMMUTABLE STRICT
AS $$
    SELECT (
        floor(
            ( EXTRACT(DOY FROM d)::int - 1
              + EXTRACT(DOW FROM make_date(EXTRACT(YEAR FROM d)::int, 1, 1))::int
            ) / 7.0
        ) + 1
    )::smallint;
$$;

COMMENT ON FUNCTION weeknum_excel(DATE) IS
    'Semana no padrão WEEKNUM do Excel (começa no domingo, 53 semanas). '
    'Validada contra 832 datas reais e contra os rótulos do Reporte Avocado_vfinal.pbix.';


-- ── 2. PREÇOS BRASIL ──────────────────────────────────────────────────────
-- Uma linha por (data, mercado, produto). Recebe tanto a carga histórica do
-- Brasil_historico.xlsx (2024–2025) quanto a coleta diária do
-- noticiasagricolas.com.br (2026+).
--
-- O UNIQUE é o que torna o ETL idempotente: rodar duas vezes no mesmo dia
-- não duplica, e um preço corrigido na fonte SOBRESCREVE o antigo. Isso
-- resolve a classe de problema que gerou a divergência da S23/2026, em que o
-- coletor append-only nunca atualizava um valor já gravado.
CREATE TABLE IF NOT EXISTS brasil_precos (
    id            BIGSERIAL     PRIMARY KEY,
    data          DATE          NOT NULL,
    mercado       TEXT          NOT NULL,            -- 'Ceagesp/SP', 'Ceasa - Campinas/SP', ...
    produto       TEXT          NOT NULL,            -- 'Avocado/ Hass/ Fuerte A'
    unidade       TEXT          NOT NULL DEFAULT 'kg',
    preco_kg      NUMERIC(8,4)  NOT NULL,
    fonte         TEXT          NOT NULL,            -- 'noticiasagricolas.com.br' | 'Brasil_historico.xlsx'
    ano           SMALLINT      GENERATED ALWAYS AS (EXTRACT(YEAR FROM data)::smallint) STORED,
    semana        SMALLINT      GENERATED ALWAYS AS (weeknum_excel(data)) STORED,
    extracted_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (data, mercado, produto)
);

CREATE INDEX IF NOT EXISTS idx_brasil_precos_data     ON brasil_precos (data);
CREATE INDEX IF NOT EXISTS idx_brasil_precos_ano_sem  ON brasil_precos (ano, semana);
CREATE INDEX IF NOT EXISTS idx_brasil_precos_produto  ON brasil_precos (produto);

COMMENT ON TABLE brasil_precos IS
    'Preço diário do abacate no atacado brasileiro. 2024–2025 vem do Brasil_historico.xlsx '
    '(Hass/Fuerte A no Ceagesp/SP, confirmado em 11/08/2026); 2026+ vem do coletor diário.';


-- ── 3. VIEW SEMANAL ───────────────────────────────────────────────────────
-- O dashboard consome esta view, não a tabela: são ~160 linhas em vez de
-- milhares, e a agregação fica no banco, com a mesma regra do Power BI.
--
-- preco_max é o que o Power BI plota (medida 'Máximo de Preco'). Os campos
-- medio/min vão junto porque o máximo superestima o patamar da semana — na
-- S21/2026 os dias vão de 4,03 a 6,46 e o gráfico mostra 6,46.
--
-- O filtro de produto protege a série: se algum dia entrar 'Abacate 1'
-- (abacate comum) na tabela, ele não contamina a curva de Hass.
CREATE OR REPLACE VIEW v_brasil_precos_semanal AS
SELECT
    ano,
    semana,
    MAX(preco_kg)                    AS preco_max,
    ROUND(AVG(preco_kg), 4)          AS preco_medio,
    MIN(preco_kg)                    AS preco_min,
    COUNT(*)                         AS dias,
    MIN(data)                        AS primeira_data,
    MAX(data)                        AS ultima_data,
    MAX(extracted_at)                AS extracted_at
FROM brasil_precos
WHERE produto ILIKE '%hass%'
GROUP BY ano, semana;

COMMENT ON VIEW v_brasil_precos_semanal IS
    'Agregação semanal de brasil_precos (só Hass), semana no padrão WEEKNUM do Excel. '
    'preco_max reproduz a medida Máximo de Preco do Power BI.';


-- ── 4. RLS: LEITURA PÚBLICA ───────────────────────────────────────────────
-- O frontend usa a anon key, então precisa de SELECT liberado. A escrita
-- continua exclusiva da service_role key, que só existe nos secrets do
-- GitHub Actions.
ALTER TABLE brasil_precos ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "leitura_publica_brasil_precos" ON brasil_precos;
CREATE POLICY "leitura_publica_brasil_precos"
    ON brasil_precos FOR SELECT USING (true);


-- ── 5. STATUS DOS ETLs ────────────────────────────────────────────────────
-- Mesmo contrato do BI Limão: uma linha por tabela, com contagem e último
-- carregamento. Vai virar a tela de status do dashboard.
-- Conforme as próximas abas entrarem, acrescente um UNION ALL aqui.
CREATE OR REPLACE VIEW v_ultima_atualizacao AS
SELECT 'brasil_precos' AS tabela,
       MAX(extracted_at) AS ultima_atualizacao,
       COUNT(*)          AS total_registros
FROM brasil_precos;


-- ── 6. CONFERÊNCIA PÓS-CARGA ──────────────────────────────────────────────
-- Rode depois de importar o histórico e a primeira coleta. Os valores
-- esperados vêm da auditoria de 11/08/2026 — se algum não bater, a carga ou
-- a função de semana está errada.
--
--   SELECT ano, semana, preco_max FROM v_brasil_precos_semanal
--    WHERE (ano, semana) IN ((2024,46), (2024,1), (2024,49), (2025,15), (2026,6))
--    ORDER BY ano, semana;
--
--   esperado:  2024 S1  ->  6.5400
--              2024 S46 -> 23.3300
--              2024 S49 -> 18.5700
--              2025 S15 -> 21.6400
--              2026 S6  -> 12.5100
