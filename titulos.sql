-- ============================================================================
-- Títulos da campanha de aviso de bloqueio — CIGAM/CTA
-- ============================================================================
-- Parametrizada. No Colab, substitua os dois DECLARE ou passe via
-- query_parameters do cliente BigQuery.
--
-- DECISÕES:
--
--  * FONTE: silver.titulos_cigam, não gold.inadimplencia. A gold só carrega
--    VENCIDOS, e o extrato que vamos reproduzir mostra a carteira pendente
--    inteira (o exemplo da LGA tinha dois títulos vencendo em 31/08). A silver
--    traz vencido e a vencer, com saldo > 0.
--
--  * dias_atraso é calculado AQUI, com DATE_DIFF a partir do vencimento.
--    NÃO use a coluna diasAtraso da tabela: ela conta a partir de
--    data_inicio_inadimplencia, que embute carência de dias úteis. São dois
--    relógios diferentes e misturá-los conta a carência duas vezes.
--
--  * A âncora é data_envio (decisão do usuário), não data_limite. O valor do
--    e-mail é a POSIÇÃO na data de envio, e serve de ponto de partida da
--    negociação — não é um valor de quitação. Consequência a aceitar: se o
--    cliente pagar exatamente esse valor no último dia do prazo, sobra
--    resíduo de juros. Por isso o texto diz "posição em DD/MM", nunca
--    "pague X e quita".
--
--  * GREATEST(0, ...) replica o MAX(0; ...) da planilha: título a vencer sai
--    com 0 dias e, portanto, sem encargo.
--
--  * Base de juros = saldo (confirmado), não `valor`. Para título com
--    pagamento parcial os dois divergem, e é o saldo que está em aberto.
--
--  * OS JUROS NÃO SÃO CALCULADOS AQUI. Ficam em encargos.py, com Decimal e
--    ROUND_HALF_UP. Reimplementar a fórmula em SQL criaria duas verdades que
--    divergem no centavo, e o BigQuery não tem o arredondamento meio-pra-cima
--    aplicado ao juro diário ANTES da multiplicação.
-- ============================================================================

-- Parâmetros vêm do Python (query_parameters), não interpolados na string:
--   @data_envio      DATE
--   @lista_clientes  ARRAY<STRING>
-- Para rodar à mão no console do BigQuery, troque os @ por:
--   DECLARE data_envio DATE DEFAULT CURRENT_DATE('America/Sao_Paulo');
--   DECLARE lista_clientes ARRAY<STRING> DEFAULT ['000479'];

SELECT
  t.codigoEmpresa                  AS codigo_cliente,
  t.nomeCompleto                   AS nome_cliente,
  t.codigoLancamento,                                   -- chave do título no log
  t.fatura                         AS doc,
  t.nf                             AS nfse,             -- nulo em aluguel
  t.dataVencimento,
  t.saldo,                                              -- base do cálculo
  GREATEST(0, DATE_DIFF(@data_envio, t.dataVencimento, DAY)) AS dias_atraso,
  t.dataVencimento < @data_envio    AS vencido,
  t.codigoContrato,
  t.situacao,
  t.codigoPortador,
  @data_envio                      AS data_base_calculo
FROM `hip-bonito-453017-m2.silver.titulos_cigam` t
WHERE t.codigoEmpresa IN UNNEST(@lista_clientes)
  AND t.saldo > 0
  -- X90 é baixa contábil (write-off interno). Cobrar por cima de perda
  -- estimada é decisão de crédito, não de pipeline — fica de fora por padrão.
  AND COALESCE(t.codigoPortador, '') != 'X90'
ORDER BY t.codigoEmpresa, t.dataVencimento, t.codigoLancamento
