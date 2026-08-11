-- ═══════════════════════════════════════════════════════════════════════════
-- BI AVOCADO — correção v2.1
-- logistica_rotas: a chave UNIQUE estava colapsando rotas legítimas.
--
-- O estudo TFruits tem, para o mesmo par origem→destino, rotas diferentes:
--   Chile · San Antono → Argentina · Buenos Aires · Direto  ->  3 dias
--   Chile · San Antono → Argentina · Buenos Aires · Santos  -> 26 dias
--
-- A chave anterior (pais_origem, porto_origem, pais_destino, porto_destino,
-- modal) tratava as duas como a mesma linha e descartava uma — justamente a de
-- 3 dias, que é a rota mais rápida de toda a base. Isso movia o card "TT menor"
-- de 3 para 4 dias e a mediana Chile→Argentina de 26 para 32.
--
-- Com rota e armador na chave: 51 linhas -> 51 chaves, zero colisão.
-- ═══════════════════════════════════════════════════════════════════════════

-- 1. rota e armador entram na chave, então não podem ser NULL
--    (em UNIQUE do Postgres, NULL é sempre distinto de NULL — o que deixaria
--    a duplicata passar de novo)
UPDATE logistica_rotas SET rota    = '' WHERE rota    IS NULL;
UPDATE logistica_rotas SET armador = '' WHERE armador IS NULL;

ALTER TABLE logistica_rotas ALTER COLUMN rota    SET DEFAULT '';
ALTER TABLE logistica_rotas ALTER COLUMN armador SET DEFAULT '';
ALTER TABLE logistica_rotas ALTER COLUMN rota    SET NOT NULL;
ALTER TABLE logistica_rotas ALTER COLUMN armador SET NOT NULL;

-- 2. troca a constraint
ALTER TABLE logistica_rotas
    DROP CONSTRAINT IF EXISTS logistica_rotas_pais_origem_porto_origem_pais_destino_port_key;
DO $$
DECLARE c TEXT;
BEGIN
    -- o nome gerado pelo Postgres pode variar; remove qualquer UNIQUE da tabela
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'logistica_rotas'::regclass AND contype = 'u'
    LOOP
        EXECUTE format('ALTER TABLE logistica_rotas DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE logistica_rotas
    ADD CONSTRAINT logistica_rotas_chave UNIQUE
    (pais_origem, porto_origem, pais_destino, porto_destino, modal, rota, armador);

-- 3. limpa para recarregar completo (dado estático de referência, sem perda)
TRUNCATE logistica_rotas;
