-- ═══════════════════════════════════════════════════════════════════════════
-- BI Avocado · v7 · oferta semanal na Europa (UE + Reino Unido) por origem
-- Página "Projeção de volume Europa" e volume semanal Brasil / Peru.
-- Rodar uma vez no SQL Editor do Supabase (projeto lbgeneauerjeskiwbgpj).
-- Idempotente.
--
-- Fonte: SAAGA, Excel semanal EU-UK-SUPPLY (avocado.co.za/export-market).
-- Unidade: mil caixas de 4 kg, semana de CHEGADA no mercado europeu.
-- status: realizado (embarque confirmado) | projetado
-- estimativa_inicial: projeção do começo do ano, para medir o erro.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS saaga_oferta_europa (
    ano                 INT         NOT NULL,
    semana              INT         NOT NULL CHECK (semana BETWEEN 1 AND 53),
    origem              TEXT        NOT NULL,
    variedade           TEXT        NOT NULL CHECK (variedade IN ('Hass','Green')),
    caixas_mil          NUMERIC(12,3),
    status              TEXT        NOT NULL CHECK (status IN ('realizado','projetado')),
    estimativa_inicial  NUMERIC(12,3),
    edicao_semana       INT,
    atualizado_em       DATE,
    arquivo             TEXT,
    extracted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ano, semana, origem, variedade)
);

ALTER TABLE saaga_oferta_europa ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS leitura_publica_saaga ON saaga_oferta_europa;
CREATE POLICY leitura_publica_saaga ON saaga_oferta_europa FOR SELECT USING (true);
GRANT SELECT ON saaga_oferta_europa TO anon, authenticated;
