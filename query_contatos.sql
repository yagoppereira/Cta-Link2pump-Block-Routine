-- ============================================================================
-- Contatos elegíveis para o aviso de bloqueio — CIGAM/CTA
-- ============================================================================
-- Substitui gerar_df_completo(). Diferenças em relação ao script atual:
--
--  1. RESPEITA recebeEmailCartaCobranca. O cadastro já marca quem não deve
--     receber cobrança (349 contatos na divisão 10). O script atual ignorava.
--  2. DEDUPLICA e-mail (10.194 contatos -> 6.960 endereços distintos).
--  3. VALIDA formato — hoje entra lixo direto no disparo.
--  4. EXCLUI a divisão 13 (CLIENTE LOCAL): é onde o equipamento está
--     instalado, às vezes uma prestadora, e não é quem paga o título.
--  5. EXCLUI o código 000679 (LINK2PUMP, intercompany).
--  6. NÃO filtra por `ativo`: não existe nenhuma empresa com ativo=False na
--     base. O filtro era inócuo e derrubava as 4 linhas com ativo NULL.
--  7. SEPARA quem pode receber de quem não pode, em vez de silenciar a
--     diferença — a coluna qtd_optout mostra quanto está sendo suprimido.
-- ============================================================================

WITH contatos AS (
  SELECT
    e.codigo,
    e.nomeCompleto,
    e.fantasia,
    e.cnpjCpf,
    REGEXP_REPLACE(COALESCE(e.cnpjCpf, ''), r'\D', '') AS cnpj_limpo,
    e.contato AS contato_principal,
    e.fone,
    e.divisao.codigoDivisao AS divisao,
    LOWER(TRIM(JSON_EXTRACT_SCALAR(x, '$.email')))            AS email,
    JSON_EXTRACT_SCALAR(x, '$.recebeEmailCartaCobranca')      AS rec_carta,
    JSON_EXTRACT_SCALAR(x, '$.recebeEmailCobranca')           AS rec_cobranca,
    JSON_EXTRACT_SCALAR(x, '$.recebeEmailBoleto')             AS rec_boleto
  FROM `hip-bonito-453017-m2.bronze.cigam__empresas` e,
       UNNEST(JSON_EXTRACT_ARRAY(COALESCE(NULLIF(e.contatos_json, ''), '[]'))) x
  WHERE e.divisao.codigoDivisao IN ('10', '11', '12', '90')
    AND e.codigo != '000679'
),

-- Um e-mail pode aparecer em vários contatos do MESMO cliente. Colapsa para
-- um endereço por cliente, mantendo o opt-in se QUALQUER contato o tiver.
validos AS (
  SELECT
    codigo, nomeCompleto, fantasia, cnpjCpf, cnpj_limpo,
    contato_principal, fone, divisao, email,
    MAX(CASE WHEN rec_carta    = 'S' THEN 1 ELSE 0 END) = 1 AS pode_carta_cobranca,
    MAX(CASE WHEN rec_cobranca = 'S' THEN 1 ELSE 0 END) = 1 AS pode_cobranca,
    MAX(CASE WHEN rec_boleto   = 'S' THEN 1 ELSE 0 END) = 1 AS pode_boleto
  FROM contatos
  WHERE email IS NOT NULL
    AND email != ''
    -- validação deliberadamente frouxa: barra lixo óbvio sem rejeitar
    -- domínio válido incomum. Não é RFC 5322.
    AND REGEXP_CONTAINS(email, r'^[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}$')
  GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
)

SELECT
  codigo                AS codigo_cliente,
  nomeCompleto          AS nome_cliente,
  fantasia              AS nome_fantasia,
  cnpjCpf               AS cnpj_cpf,
  cnpj_limpo,                       -- use ESTE lado no merge com a planilha
  contato_principal,
  fone                  AS fone_principal,
  divisao,

  -- destinatários do aviso de bloqueio
  STRING_AGG(DISTINCT IF(pode_carta_cobranca, email, NULL), '; '
             ORDER BY IF(pode_carta_cobranca, email, NULL)) AS emails_cobranca,
  COUNTIF(pode_carta_cobranca)      AS qtd_emails_cobranca,

  -- suprimidos por opt-in negado — monitorar, não enviar
  COUNTIF(NOT pode_carta_cobranca)  AS qtd_optout,

  COUNT(*)                          AS qtd_emails_total
FROM validos
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
-- Tire o HAVING para enxergar quem ficou SEM nenhum destinatário elegível.
-- Esse é o relatório que o comercial precisa ver: cliente inadimplente que
-- não tem para quem mandar aviso é um buraco de processo, não uma linha a menos.
HAVING qtd_emails_cobranca > 0
ORDER BY nome_cliente
