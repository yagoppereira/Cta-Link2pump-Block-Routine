-- SCHEMA: silver_pier, não silver.
-- Em 30/09/2026 o DW separou os dados financeiros sensíveis em schemas
-- próprios (bronze_pier, silver_pier). A lancamentos_enriquecidos foi junto;
-- a versão antiga parou de atualizar e será excluída.
-- Medido na troca: a auditoria de baixas lia 1.908 clientes no schema velho e
-- 2.082 no novo — 174 clientes e R$ 460 mil que sumiriam em silêncio.
-- ============================================================================
-- Base de inadimplência remontada do DW
-- ============================================================================
-- Substitui o preenchimento manual da planilha "Frequência de Vencimento".
-- Uma linha por cadastro CIGAM COM DÍVIDA PRÓPRIA.
--
-- X90 CONTA COMO ABERTO. Baixa em X90 é contábil, não é pagamento: o título
-- segue devido. O CIGAM zera o `saldo` e mantém o montante em `valor`, e a
-- situação vira 'L'. Consequência: silver.titulos_cigam, que filtra saldo > 0,
-- descarta TODOS os X90 — e a gold.inadimplencia, que bebe dela, também.
-- Medido numa unidade de negócio: 1.164 lançamentos, R$ 828.988,58, 193
-- clientes; e 111 clientes cuja dívida vencida é EXCLUSIVAMENTE X90,
-- R$ 481.910,15, invisíveis para qualquer coisa construída sobre o DW.
-- Por isso esta query lê o bronze direto, não a silver.
--
-- ATENÇÃO ao codigoTipo: os X90 são tipo 'E', não 'R'. Filtrar por 'R'
-- (o que parece óbvio) devolve 147 de 1.313 e passa despercebido.
--
-- FREQUÊNCIA: meses distintos com título vencido, no GRUPO (raiz de CNPJ),
-- INCLUINDO os baixados em X90. Conferido contra os números do Paul em 6 de 6
-- clientes; as duas regras são necessárias juntas — só grupo ou só X90 erra.
--   Benevides 9 · Orlando 8 · LCM(0003-05) 5 · LCM(0001-35) 5 · VAP 5 · Concretos 4
--
-- O GRUPO DÁ A FREQUÊNCIA, O CADASTRO DEFINE QUEM ENTRA. Sem o filtro de
-- dívida própria, uma filial que não deve nada herda a frequência do grupo e
-- entra na régua. `frequencia_propria` fica ao lado para a diferença ser
-- visível em vez de embutida.
--
-- ÚLTIMA UTILIZAÇÃO depende da ALOCAÇÃO estar feita. Cliente sem alocação sai
-- com NULL e `fonte_uso = 'sem alocacao'` — nunca com uma data chutada. Hoje
-- isso atinge mais da metade dos inadimplentes, e é o gargalo real da régua:
-- o levantamento manual do último uso expira a cada campanha, a alocação é
-- feita uma vez e serve para sempre.
--
-- NÃO CALCULA `Renegociado` NEM `Observação`. Não são dados, são conhecimento
-- de quem negocia. O script que escreve a planilha preserva as duas.
-- ============================================================================

DECLARE dias_uso_recente INT64 DEFAULT 90;

-- O GRUPO DÁ O SINAL, O CADASTRO PRECISA MERECER.
--
-- Duas correções na frequência de grupo, ambas pelo mesmo motivo: a frequência
-- serve para descrever o comportamento do grupo, não para condenar qualquer
-- CNPJ que pertença a ele.
--
--   1. Só cadastro COM BOMBA entra no cálculo do grupo. Uma filial sem
--      equipamento, com dívida de venda avulsa, inflava a frequência e
--      arrastava os cadastros operacionais junto. Afeta 27 grupos.
--
--   2. frequencia_propria_minima. Sem ela, um cadastro que deve UM mês entra
--      porque um irmão deve nove: IPIRANGA (própria 1, grupo 18), Aço Verde
--      1588 (1 e 9), Durlicouros 3695 (1 e 8), HOK (1 e 7).
--
-- Medido em freq_grupo >= 7: a regra sem as correções pega 30 clientes; com as
-- duas, 17. Nove dos que saem deviam dois meses ou menos.

WITH emp AS (
  SELECT
    codigo,
    nomeCompleto AS cliente,
    cnpjCpf      AS cnpj,
    SUBSTR(REGEXP_REPLACE(COALESCE(cnpjCpf, ''), r'\D', ''), 1, 8) AS raiz
  FROM `hip-bonito-453017-m2.bronze.cigam__empresas`
  WHERE divisao.codigoDivisao IN ('10', '11', '12', '90')
    AND codigo != '000679'
),

-- Frequência no grão do GRUPO
-- "Título em aberto" vem de DUAS fontes, cada uma onde foi verificada:
--
--   normais -> silver.titulos_cigam. Conferida contra o export do CIGAM:
--              452 de 467 clientes batem NO CENTAVO, 2.724 títulos idênticos.
--
--   X90     -> bronze_pier.cigam__lancamentos, codigoTipo 'E', montante em `valor`.
--              Também conferido no centavo: 1.164 lançamentos / R$ 828.988,58
--              e 2 de juros / R$ 137,99, iguais ao export.
--
-- NÃO ler os normais do bronze. Filtrar situacao='A' AND saldo>0 lá devolve
-- 36.219 lançamentos e R$ 35,4 milhões contra ~1.600 e R$ 1,7 mi reais — um
-- inchaço de mais de dez vezes que ainda não tem explicação. Até alguém
-- entender o que aquelas linhas são, a silver é a fonte confiável.
-- FONTE: silver.titulos_cigam, e só ela.
--
-- O modelo já resolve o que eu levei dias supondo que estivesse quebrado:
-- carência de dia útil, fuso de São Paulo, X90 com valor original, e o bug do
-- CIGAM de não atualizar o `saldo` do título quando a baixa entra por
-- codigoPartida. NÃO reconstruir isso a partir do bronze: ler `saldo > 0` lá
-- conta como aberto milhares de títulos já pagos (36.219 contra ~1.600 reais).
--
-- EXCLUSÃO OBRIGATÓRIA: X91 e X92.
--   X90  Títulos Impagáveis      baixa contábil, dívida segue devida -> ENTRA
--   X91  Cobrança Jurídica       jurídico assumiu                    -> FORA
--   X92  Recuperação Judicial    crédito sujeito ao plano            -> FORA
--   X99  Perda Efetiva           criado, sem uso                     -> FORA
--
-- X91/X92 não é preferência de processo. Cobrar cliente em recuperação
-- judicial sobre crédito anterior ao pedido é problema jurídico, e cliente em
-- cobrança jurídica não pode receber comunicação paralela do financeiro.
-- Hoje: 11 cadastros, R$ 317.732,69 — entre eles AUTOTRANS (X92) e
-- ÉDIPO, SVP AGRÍCOLA e ELECTRA (X91).
--
-- A titulos_cigam já não traz esses lançamentos. A exclusão aqui é do CLIENTE
-- inteiro: quem tem qualquer título em X91/X92 sai da régua, mesmo que tenha
-- outros títulos normais em aberto. Metade do débito no jurídico e a outra
-- metade recebendo carta de bloqueio é a pior combinação possível.
sob_tutela_juridica AS (
    SELECT DISTINCT codigoEmpresa
    FROM `hip-bonito-453017-m2.silver_pier.lancamentos_enriquecidos`
    WHERE codigoPortador IN ('X91', 'X92', 'X99')
      AND SAFE_CAST(valor AS FLOAT64) > 0
),

abertos AS (
    SELECT
        t.codigoEmpresa,
        t.codigoLancamento,
        t.fatura,
        t.nf,
        t.dataVencimento AS venc,
        t.codigoPortador,
        t.codigoPortador = 'X90' AS baixa_contabil,
        t.saldo          AS em_aberto
    FROM `hip-bonito-453017-m2.silver.titulos_cigam` t
    WHERE t.saldo > 0
      AND t.codigoEmpresa NOT IN (SELECT codigoEmpresa FROM sob_tutela_juridica)
),

-- Quem tem parque instalado: só esses contam para a frequência do grupo.
cadastros_com_bomba AS (
  SELECT DISTINCT cliente_cigam_pagante AS codigo
  FROM `hip-bonito-453017-m2.gold.bombas_alocadas`
),

freq_grupo AS (
  SELECT
    e.raiz,
    COUNT(DISTINCT IF(cb.codigo IS NOT NULL,
                      DATE_TRUNC(a.venc, MONTH), NULL))        AS frequencia,
    COUNT(DISTINCT DATE_TRUNC(a.venc, MONTH))                  AS frequencia_grupo_bruta,
    COUNT(DISTINCT e.codigo)                                   AS cadastros_no_grupo,
    COUNT(DISTINCT IF(cb.codigo IS NOT NULL, e.codigo, NULL))  AS cadastros_com_bomba
  FROM abertos a
  JOIN emp e ON e.codigo = a.codigoEmpresa
  LEFT JOIN cadastros_com_bomba cb ON cb.codigo = a.codigoEmpresa
  WHERE a.venc < CURRENT_DATE('America/Sao_Paulo')
  GROUP BY 1
),

-- Dívida no grão do CADASTRO: é quem entra na régua
divida AS (
  SELECT
    a.codigoEmpresa AS codigo,
    COUNT(DISTINCT DATE_TRUNC(a.venc, MONTH))          AS frequencia_propria,
    -- Meses vencidos DENTRO DO ANO CORRENTE. Dívida antiga sozinha não
    -- sustenta bloqueio: um cliente que parou de pagar em 2023, quitou o
    -- corrente e carrega resíduo velho tem perfil diferente de quem está
    -- deixando de pagar agora. O segundo é caso de régua; o primeiro é
    -- cobrança de outra natureza e vai para controle paralelo.
    COUNT(DISTINCT IF(EXTRACT(YEAR FROM a.venc)
                      = EXTRACT(YEAR FROM CURRENT_DATE('America/Sao_Paulo')),
                      DATE_TRUNC(a.venc, MONTH), NULL))  AS meses_ano_corrente,
    COUNT(*)                                           AS titulos_abertos,
    ROUND(SUM(a.em_aberto), 2)                         AS em_atraso,
    -- Faturas vencidas, para casar com o DOC da aba ACORDOS (que grava
    -- fatura+parcela; lá o split corta no '/').
    STRING_AGG(DISTINCT CAST(a.fatura AS STRING), ';'
               ORDER BY CAST(a.fatura AS STRING))      AS faturas_vencidas,
    COUNTIF(a.baixa_contabil)                          AS titulos_x90,
    ROUND(SUM(IF(a.baixa_contabil, a.em_aberto, 0)), 2) AS valor_x90,
    -- Cliente cuja dívida é SÓ X90 é invisível para a silver e para a gold.
    LOGICAL_AND(a.baixa_contabil)                      AS so_baixa_contabil,
    MIN(a.venc)                                        AS vencimento_mais_antigo
  FROM abertos a
  WHERE a.venc < CURRENT_DATE('America/Sao_Paulo')
  GROUP BY 1
),

parque AS (
  SELECT
    cliente_cigam_pagante AS codigo,
    COUNT(*)                                   AS bombas,
    COUNT(DISTINCT serial_equipamento)         AS equipamentos,
    STRING_AGG(DISTINCT CAST(serial_equipamento AS STRING), '; '
               ORDER BY CAST(serial_equipamento AS STRING)) AS seriais,
    COUNT(DISTINCT cliente_id)                 AS sistemas
  FROM `hip-bonito-453017-m2.gold.bombas_alocadas`
  GROUP BY 1
),

-- Só as bombas de quem tem dívida entram no scan de abastecimento: sem esse
-- recorte a query varre a tabela inteira e fica cara.
bombas_relevantes AS (
  SELECT DISTINCT b.cliente_cigam_pagante AS codigo, b.bomba_id
  FROM `hip-bonito-453017-m2.gold.bombas_alocadas` b
  WHERE b.cliente_cigam_pagante IN (SELECT codigo FROM divida)
),

uso AS (
  SELECT br.codigo, MAX(a.data) AS ultima_utilizacao
  FROM `hip-bonito-453017-m2.silver.abastecimentos_validos` a
  JOIN bombas_relevantes br
    ON CAST(br.bomba_id AS STRING) = CAST(a.bomba_id AS STRING)
  WHERE a.data >= DATE_SUB(CURRENT_DATE('America/Sao_Paulo'), INTERVAL 730 DAY)
  GROUP BY 1
),

rx AS (
  SELECT codigo, mrr, situacao, segmento
  FROM `hip-bonito-453017-m2.gold.raio_x_cliente`
)

SELECT
  e.codigo,
  e.cliente,
  e.cnpj,
  e.raiz                                    AS cnpj_raiz,

  f.frequencia,                             -- do GRUPO, só cadastros com bomba
  d.frequencia_propria,                     -- só deste cadastro — precisa merecer
  d.meses_ano_corrente,                     -- quantos desses meses são deste ano
  f.frequencia_grupo_bruta,                 -- com os sem-bomba, para comparar
  f.cadastros_no_grupo,
  f.cadastros_com_bomba,

  d.em_atraso,
  d.faturas_vencidas,       -- casa com o DOC da aba ACORDOS (cortado no '/')
  d.titulos_abertos,
  d.titulos_x90,
  d.valor_x90,
  d.so_baixa_contabil,
  d.vencimento_mais_antigo,
  ROUND(rx.mrr, 2)                          AS recorrente,

  u.ultima_utilizacao,
  CASE
    WHEN p.codigo IS NULL              THEN 'sem alocacao'
    WHEN u.ultima_utilizacao IS NULL   THEN 'alocado, sem abastecimento'
    ELSE 'ok'
  END                                       AS fonte_uso,
  CASE
    WHEN p.codigo IS NULL THEN NULL         -- não afirmar o que não se sabe
    WHEN u.ultima_utilizacao >= DATE_SUB(CURRENT_DATE('America/Sao_Paulo'),
                                         INTERVAL dias_uso_recente DAY) THEN TRUE
    ELSE FALSE
  END                                       AS usa_equipamento,

  COALESCE(p.bombas, 0)                     AS bombas,
  COALESCE(p.equipamentos, 0)               AS equipamentos,
  p.seriais,
  COALESCE(p.sistemas, 0)                   AS sistemas,

  rx.situacao,
  rx.segmento,
  CURRENT_DATE('America/Sao_Paulo')         AS data_referencia

FROM divida d
JOIN emp e        ON e.codigo = d.codigo
LEFT JOIN freq_grupo f ON f.raiz = e.raiz
LEFT JOIN parque p     ON p.codigo = d.codigo
LEFT JOIN uso u        ON u.codigo = d.codigo
LEFT JOIN rx           ON rx.codigo = d.codigo
ORDER BY f.frequencia DESC, d.em_atraso DESC
