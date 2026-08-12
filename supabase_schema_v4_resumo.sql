-- ═══════════════════════════════════════════════════════════════════════════
-- BI Avocado — v4: rastreabilidade do Resumo Executivo CIRAD / Tropisens
-- ═══════════════════════════════════════════════════════════════════════════
-- Rodar no SQL Editor do Supabase. Idempotente.
--
-- A tabela `cirad_resumo` (v2) já guarda ano, semana, ordem, secao e texto. O
-- que falta é dizer COMO aquele texto apareceu. O resumo é o único dado do
-- projeto escrito por um modelo de linguagem, então ele é o único em que
-- "de onde veio" não é dedutível da fonte: dois textos diferentes podem sair
-- do mesmo PDF. Sem essas colunas não há como responder, três meses depois,
-- se um número estranho no resumo veio do boletim, do banco ou do modelo.

ALTER TABLE cirad_resumo
    ADD COLUMN IF NOT EXISTS arquivo          TEXT,      -- PDF de origem
    ADD COLUMN IF NOT EXISTS relatorio_semana SMALLINT,  -- semana da EDIÇÃO
    ADD COLUMN IF NOT EXISTS modelo           TEXT,      -- modelo que escreveu
    ADD COLUMN IF NOT EXISTS metodo           TEXT,      -- como o texto foi produzido
    ADD COLUMN IF NOT EXISTS conferido        BOOLEAN;   -- passou na conferência de cifras


COMMENT ON COLUMN cirad_resumo.relatorio_semana IS
    'Semana da edição do boletim. A coluna `semana` é a do PREÇO — o boletim '
    'da S31 pode publicar o preço da S30, e o resumo fala do preço.';

COMMENT ON COLUMN cirad_resumo.conferido IS
    'TRUE = toda cifra em euro citada no resumo foi encontrada em '
    'europa_cirad_precos / europa_cirad_calibre da mesma semana. O ETL não '
    'grava com FALSE: se a conferência falha, nada entra e o job falha. A '
    'coluna existe para o dashboard poder mostrar o selo e para linha antiga, '
    'carregada à mão antes desta versão, ficar visivelmente NULL.';


-- ── VIEW DO RESUMO CORRENTE ───────────────────────────────────────────────
-- A tela mostra só a semana mais recente. A view resolve isso no banco em vez
-- de o front baixar o histórico inteiro para descartar quase tudo.
CREATE OR REPLACE VIEW v_cirad_resumo_atual AS
WITH ultima AS (
    SELECT ano, semana
    FROM cirad_resumo
    ORDER BY ano DESC, semana DESC
    LIMIT 1
)
SELECT c.ano, c.semana, c.ordem, c.secao, c.texto,
       c.arquivo, c.relatorio_semana, c.modelo, c.metodo, c.conferido,
       c.extracted_at
FROM cirad_resumo c
JOIN ultima u ON u.ano = c.ano AND u.semana = c.semana
ORDER BY c.ordem;

COMMENT ON VIEW v_cirad_resumo_atual IS
    'Seções do resumo da semana mais recente, em ordem. É o que a aba Resumo '
    'CIRAD consome.';


-- RLS: a view herda a política da tabela base, que já tem leitura pública
-- (criada na v2). Nada a fazer aqui.

-- ── CONFERÊNCIA ───────────────────────────────────────────────────────────
-- SELECT ano, semana, relatorio_semana, modelo, conferido, extracted_at
-- FROM v_cirad_resumo_atual LIMIT 1;
-- SELECT ordem, secao, left(texto, 60) FROM v_cirad_resumo_atual;
