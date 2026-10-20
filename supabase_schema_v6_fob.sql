-- ═══════════════════════════════════════════════════════════════════════════
-- BI Avocado · v6 · preço de exportação FOB/FOT mensal (página Panorama Mundial)
-- Rodar uma vez no SQL Editor do Supabase (projeto lbgeneauerjeskiwbgpj).
-- Idempotente: pode rodar de novo sem efeito colateral.
--
-- Fontes gravadas por etl_fob.py:
--   comexstat  Brasil, exportação de abacate (NCM 0804.40.00) por via de
--              transporte. Valor FOB declarado em US$ e kg líquido.
--              Via rodoviária = FOT (mesmo valor, entregue no caminhão).
--   census     Importações dos EUA de abacate Hass (HTS 0804.40.00.20 e .40)
--              por país de origem.
--              "Customs value" exclui frete, seguro e tarifas americanas:
--              é o valor no porto de origem, ou seja, FOB do exportador.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS fob_mensal (
    fonte         TEXT        NOT NULL,          -- comexstat | census
    origem        TEXT        NOT NULL,          -- país exportador (pt-BR)
    destino       TEXT        NOT NULL,          -- Mundo | EUA
    via           TEXT        NOT NULL,          -- TODAS | MARITIMA | AEREA | RODOVIARIA
    incoterm      TEXT        NOT NULL,          -- FOB | FOT
    ano           INT         NOT NULL,
    mes           INT         NOT NULL CHECK (mes BETWEEN 1 AND 12),
    peso_kg       NUMERIC(16,2),
    valor_usd     NUMERIC(16,2),
    extracted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fonte, origem, destino, via, ano, mes)
);

ALTER TABLE fob_mensal ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS leitura_publica_fob_mensal ON fob_mensal;
CREATE POLICY leitura_publica_fob_mensal ON fob_mensal FOR SELECT USING (true);

-- US$/kg pronto para o painel. Linhas com menos de 1 tonelada no mês ficam
-- de fora: um embarque de amostra distorce o preço médio.
CREATE OR REPLACE VIEW v_fob_mensal AS
SELECT fonte, origem, destino, via, incoterm, ano, mes,
       peso_kg, valor_usd,
       ROUND(valor_usd / NULLIF(peso_kg, 0), 4) AS usd_kg,
       extracted_at
FROM fob_mensal
WHERE peso_kg >= 1000;

GRANT SELECT ON fob_mensal, v_fob_mensal TO anon, authenticated;
