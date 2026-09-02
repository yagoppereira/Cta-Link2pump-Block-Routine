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
--  * SÓ VENCIDO. O aviso de bloqueio cobra o que está em atraso. Sem este
--    filtro entram as parcelas futuras do contrato — 154 títulos vencendo até
--    2027 no caso da Luiz Costa — e o quadro do e-mail deixa de descrever
--    inadimplência.
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
  t.codigoContrato,
  -- Tipo de pendência = descrição da conta financeira. Confere com a coluna
  -- "Tipo Cobrança" do relatório do CIGAM: 100103 Licenciamentos,
  -- 100104 Aluguel, 100105 Instalações, 100101 Venda de equipamentos e
  -- perifericos, 100102 Adesões. Nada de ponte aqui — está tudo no DW.
  COALESCE(NULLIF(TRIM(cc.DESCRICAO), ''), '—') AS tipo_pendencia,
  t.situacao,
  t.codigoPortador,
  @data_envio                      AS data_base_calculo
FROM `hip-bonito-453017-m2.silver.titulos_cigam` t
LEFT JOIN `hip-bonito-453017-m2.bronze.cigam__cadastro_conta_financeira` cc
  ON cc.codigoConta = t.codigoConta
WHERE t.codigoEmpresa IN UNNEST(@lista_clientes)
  AND t.saldo > 0
  AND t.dataVencimento < @data_envio
  -- X90 ENTRA. Baixa em X90 é contábil, não pagamento: o título segue devido,
  -- e a régua seleciona o cliente contando com ele. Excluir aqui criava duas
  -- definições de dívida no mesmo pipeline — a Construtora Luiz Costa foi
  -- selecionada por R$ 6.758 de X90 vencido, e o e-mail dela mostraria
  -- R$ 248.515 de parcelas futuras sem citar um centavo da dívida real.
  -- X91/X92 não chegam aqui: a régua já exclui esses clientes inteiros.
ORDER BY t.codigoEmpresa, t.dataVencimento, t.codigoLancamento
