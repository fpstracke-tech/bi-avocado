-- ═══════════════════════════════════════════════════════════════════════════
-- BI Avocado · v5 · clima (Open-Meteo) e Comexstat
-- Rodar uma vez no SQL Editor do Supabase (projeto lbgeneauerjeskiwbgpj).
-- Idempotente: pode rodar de novo sem efeito colateral.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── CLIMA: colunas que o painel do PBI usa e a v2 não tinha ────────────────
ALTER TABLE clima ADD COLUMN IF NOT EXISTS regiao        TEXT;      -- Cidade_visual do PBI
ALTER TABLE clima ADD COLUMN IF NOT EXISTS continente    TEXT;
ALTER TABLE clima ADD COLUMN IF NOT EXISTS grupo         TEXT;      -- produtor | brasil
ALTER TABLE clima ADD COLUMN IF NOT EXISTS lat           NUMERIC(7,3);
ALTER TABLE clima ADD COLUMN IF NOT EXISTS lon           NUMERIC(7,3);
ALTER TABLE clima ADD COLUMN IF NOT EXISTS umidade_pct   NUMERIC(5,1);
ALTER TABLE clima ADD COLUMN IF NOT EXISTS weather_code  SMALLINT;
ALTER TABLE clima ADD COLUMN IF NOT EXISTS descricao     TEXT;

-- A v2 somava os 7 dias PASSADOS. O PBI mostra a PREVISÃO: hoje até hoje+7
-- (DAX "Acumulado 7 dias (mm)"). Corrigido aqui; o limite de alerta (10 mm)
-- é o da medida "Descrição".
DROP VIEW IF EXISTS v_clima_acumulado_7d;
CREATE VIEW v_clima_acumulado_7d AS
SELECT pais, cidade,
       COALESCE(MAX(regiao), cidade)        AS regiao,
       MAX(continente)                      AS continente,
       MAX(grupo)                           AS grupo,
       ROUND(SUM(chuva_mm), 1)              AS acumulado_7d_mm,
       MAX(temp_max)                        AS temp_max_7d,
       MIN(temp_min)                        AS temp_min_7d,
       MAX(data)                            AS ultima_data,
       CASE WHEN SUM(chuva_mm) >= 10 THEN 'Chuva significativa prevista'
            ELSE 'Sem chuva relevante' END  AS descricao,
       MAX(extracted_at)                    AS extracted_at
FROM clima
WHERE data BETWEEN CURRENT_DATE AND CURRENT_DATE + 7
GROUP BY pais, cidade;

-- Série diária da previsão, para os gráficos de temperatura e chuva
CREATE OR REPLACE VIEW v_clima_previsao AS
SELECT data, pais, cidade, COALESCE(regiao, cidade) AS regiao, continente, grupo,
       temp_max, temp_min, chuva_mm, umidade_pct, descricao
FROM clima
WHERE data BETWEEN CURRENT_DATE AND CURRENT_DATE + 7;

GRANT SELECT ON v_clima_acumulado_7d, v_clima_previsao TO anon, authenticated;

-- ── COMEXSTAT: share por mês com a mesma conta do DAX ──────────────────────
-- Total Containers = CEILING(SUM(kg) / 23760). O arredondamento é no NÍVEL da
-- agregação, não por linha: o total do mês é ceil(soma do mês), não a soma dos
-- ceils por país (em jan/2026 isso é 7 contra 44).
CREATE OR REPLACE VIEW v_comexstat_mensal AS
SELECT ano, mes, pais_destino,
       SUM(peso_kg)                         AS peso_kg,
       SUM(valor_fob_usd)                   AS valor_fob_usd,
       CEIL(SUM(peso_kg) / 23760.0)::INT    AS containers,
       MAX(extracted_at)                    AS extracted_at
FROM comexstat
GROUP BY ano, mes, pais_destino;

GRANT SELECT ON v_comexstat_mensal TO anon, authenticated;
