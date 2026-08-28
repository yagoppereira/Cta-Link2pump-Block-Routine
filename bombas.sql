-- ============================================================================
-- Bombas afetadas pelo bloqueio — ótica PAGANTE
-- ============================================================================
-- Alimenta o quadro "equipamentos afetados" do aviso. Lê o DW direto, não a
-- planilha Bombas_Alocadas: o disparo não pode depender de alguém ter
-- atualizado uma aba naquela manhã.
--
-- DECISÕES:
--
--  * ÓTICA PAGANTE. A dívida é de quem paga, então o bloqueio segue
--    cliente_cigam_pagante. A ótica local responde outra pergunta ("onde a
--    bomba está") e usá-la aqui bloquearia quem não deve nada.
--
--  * serial_equipamento NÃO É CHAVE. Um equipamento atende duas bombas
--    (canal 1 e 2). Contar bomba por serial subestima; contar equipamento por
--    linha superestima. Por isso o quadro traz bomba + canal, e a contagem de
--    equipamentos usa COUNT(DISTINCT serial).
--
--  * TERCEIRO DE VERDADE vs OUTRA FILIAL. Comparar apenas os códigos marca a
--    LCM como "bomba em terceiro" quando o local é outro cadastro da própria
--    LCM. A raiz do CNPJ separa os dois casos. Só o terceiro real vira
--    destaque no e-mail — e é o caso que gera ligação no suporte de uma
--    empresa que não deve nada.
--
--  * NÃO EXISTE FLAG DE ALOCAÇÃO DESATUALIZADA, e não é esquecimento.
--    alocacao_atualizada_em mede quando o REGISTRO foi editado, não quando
--    alguém verificou a bomba no cliente: 1.011 bombas (16,6% da view) têm
--    a mesma data, 21/02/2026, que é carga inicial. Um alerta em cima disso
--    dispararia em um sexto da base sem significar nada.
--    Consequência para o texto: o e-mail fala de equipamentos VINCULADOS AO
--    CONTRATO, nunca "instalados em X". A primeira afirmação o dado sustenta;
--    a segunda não.
-- ============================================================================

-- Parâmetro vem do Python (query_parameters):
--   @lista_clientes  ARRAY<STRING>
-- Para rodar à mão: DECLARE lista_clientes ARRAY<STRING> DEFAULT ['003106'];

-- O conflito é medido sobre a população INTEIRA do sistema, não só sobre a
-- lista: é justamente o pagante de fora que corre risco de ser cortado.
WITH alvo AS (
  SELECT codigo FROM UNNEST(@lista_clientes) AS codigo
),
sistemas AS (
  SELECT DISTINCT b.cliente_id
  FROM `hip-bonito-453017-m2.gold.bombas_alocadas` b
  JOIN alvo a ON a.codigo = b.cliente_cigam_pagante
),
inadimplentes AS (
  SELECT DISTINCT codigoEmpresa AS codigo
  FROM `hip-bonito-453017-m2.silver.titulos_cigam`
  WHERE saldo > 0 AND COALESCE(codigoPortador,'') != 'X90'
    AND dataVencimento < CURRENT_DATE('America/Sao_Paulo')
),
conflito AS (
  SELECT
    x.cliente_id,
    COUNT(*)                                        AS bombas_no_sistema,
    COUNTIF(a.codigo IS NULL)                       AS bombas_de_terceiros,
    COUNTIF(a.codigo IS NULL AND i.codigo IS NULL)  AS bombas_de_adimplentes,
    ARRAY_AGG(DISTINCT IF(a.codigo IS NULL AND i.codigo IS NULL,
                          e.nomeCompleto, NULL) IGNORE NULLS) AS adimplentes
  FROM `hip-bonito-453017-m2.gold.bombas_alocadas` x
  JOIN sistemas s USING (cliente_id)
  LEFT JOIN alvo a ON a.codigo = x.cliente_cigam_pagante
  LEFT JOIN inadimplentes i ON i.codigo = x.cliente_cigam_pagante
  LEFT JOIN `hip-bonito-453017-m2.bronze.cigam__empresas` e
    ON e.codigo = x.cliente_cigam_pagante
  GROUP BY 1
)
SELECT
  b.cliente_cigam_pagante              AS codigo_cliente,
  b.cliente_id,
  b.bomba_nome,
  b.canal,
  b.serial_equipamento,
  b.equipamento_descricao,

  -- Onde a bomba está fisicamente
  b.cliente_cigam_local,
  loc.nomeCompleto                     AS local_nome,
  loc.cnpjCpf                          AS local_cnpj,

  -- TRUE só quando o local é outra EMPRESA, não outra filial do mesmo grupo
  b.cliente_cigam_local != b.cliente_cigam_pagante
    AND SUBSTR(REGEXP_REPLACE(COALESCE(loc.cnpjCpf,''), r'\D',''), 1, 8)
      != SUBSTR(REGEXP_REPLACE(COALESCE(pag.cnpjCpf,''), r'\D',''), 1, 8)
                                       AS instalada_em_terceiro,

  DATE(b.alocacao_atualizada_em)        AS alocacao_registrada_em,
  b.id_op_operacional,

  -- conflito do sistema em que esta bomba está
  c.bombas_no_sistema,
  c.bombas_de_terceiros,
  c.bombas_de_adimplentes,
  c.bombas_de_adimplentes = 0          AS bloqueio_de_sistema_seguro,
  ARRAY_TO_STRING(c.adimplentes, ' | ') AS adimplentes_no_sistema

FROM `hip-bonito-453017-m2.gold.bombas_alocadas` b
LEFT JOIN `hip-bonito-453017-m2.bronze.cigam__empresas` pag
  ON pag.codigo = b.cliente_cigam_pagante
LEFT JOIN `hip-bonito-453017-m2.bronze.cigam__empresas` loc
  ON loc.codigo = b.cliente_cigam_local
LEFT JOIN conflito c ON c.cliente_id = b.cliente_id
WHERE b.cliente_cigam_pagante IN UNNEST(@lista_clientes)
ORDER BY b.cliente_id, codigo_cliente, instalada_em_terceiro DESC, b.bomba_nome, b.canal
