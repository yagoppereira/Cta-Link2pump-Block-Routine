"""
Aviso de bloqueio por inadimplência — orquestrador.

QUATRO FASES, executadas em momentos diferentes:

    preparar(c)      D0            lê tudo, calcula, CONGELA e escreve a prévia
    disparar(c)      D0            lê o congelado e envia. Nunca reconsulta o BQ.
    reconciliar(c)   D+prazo       única releitura do BQ: quem pagou sai
    gerar_cards(c)   D+prazo       texto dos cards do Pipefy, do congelado

Entre preparar e disparar tem uma pessoa conferindo. Entre reconciliar e
gerar_cards também. Não é cerimônia: a lista é feita à mão e aviso de bloqueio
não tem desfazer.

POR QUE O CONGELAMENTO NÃO É OPCIONAL
gold.inadimplencia é reescrita durante o dia — medimos 23% dos clientes
mudando entre duas consultas separadas por 15 minutos. E o serial some de
gold.bombas_alocadas quando o bloqueio é concluído. Uma consulta nova na hora
do disparo enviaria algo diferente do que foi conferido, e o card pediria
bloqueio de menos bombas do que o aviso comunicou.

O QUE NÃO FOI TESTADO
Toda a parte de gspread, BigQuery e SMTP. O ambiente onde este código foi
escrito não tem credencial nem saída de rede. A lógica pura (encargos, fila,
resolução de lista e de vendedor, template, card) tem teste e paridade
confirmada contra a planilha real. Rode a fase A com 2 ou 3 clientes antes.
"""

from datetime import date
import re
import types
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

import campanha_v2
import campanha_v2 as cfg   # apelido histórico; os dois nomes valem
import encargos
import envio
import io_sheets as io
import pipefy
import template
import vendedores as vend

# ---------------------------------------------------------------- configuração

PROJECT_ID = "hip-bonito-453017-m2"

ID_DESTINO = "1KkE6_D_-xug1LSxQYJ9v5vjUtjK72uzawL0pBfC4h3A"      # planilha 1 (nossa)
# Planilha 2 ("Central Inadimplência por Carteira de Vendedor") saiu do
# circuito: o cruzamento cliente<->vendedor dela já chega pronto na coluna
# `vendedor` da Contatos_Emails. Mantido só como referência de onde o dado
# nasce; o pipeline não abre mais essa planilha.
ID_VENDEDORES = "1KSd7cdHUiotmE6WAePq0WnXzhLZz8vggwPkcn1ZGBv4"
ABA_BASE_CLIENTES = "Base_Clientes"   # carteira vigente, mantida pelo comercial

ABA_INPUT = "Campanha_Input"
ABA_PREVIA = "Campanha_Previa"
ABA_TITULOS = "Campanha_Titulos"
ABA_BOMBAS = "Campanha_Bombas"
ABA_LOG = "Campanha_Log"
ABA_TRIAGEM = "Triagem"
ABA_TRIAGEM_REGRA = "Triagem_Sem_Alocacao"
ABA_TRIAGEM_ANTIGA = "Triagem_Divida_Antiga"
ABA_CHURN = "Revisao_CS_Churn"
ABA_REANALISE = "Reanalise_Dados"
ABA_EXCECOES = "Nunca_Notificar"
ABA_CAMPANHAS = "Campanhas"
ABA_PAINEL_CAMPANHAS = "Painel_Campanhas"
ABA_EMAIL_VEND = "Email_Vendedores"
ABA_CONTATOS = "Contatos_Emails"        # traz a coluna `vendedor` já resolvida

SQL = Path(__file__).parent

# ---------------------------------------------------------------------- acesso


def conectar():
    """Colab: autentica uma vez e usa a mesma identidade nos dois serviços."""
    from google.colab import auth
    import google.auth
    import gspread

    auth.authenticate_user()
    creds, _ = google.auth.default()
    return gspread.authorize(creds), bigquery.Client(project=PROJECT_ID, credentials=creds)


def _q(bq, arquivo: str, **params):
    """Roda um .sql do repositório com parâmetros nomeados. Nunca interpola
    valor em string de SQL."""
    tipos = []
    for nome, valor in params.items():
        if isinstance(valor, (list, tuple, set)):
            tipos.append(bigquery.ArrayQueryParameter(nome, "STRING", sorted(valor)))
        elif isinstance(valor, date):
            tipos.append(bigquery.ScalarQueryParameter(nome, "DATE", valor))
        else:
            tipos.append(bigquery.ScalarQueryParameter(nome, "STRING", str(valor)))

    sql = (SQL / arquivo).read_text()
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=tipos))
    return job.to_dataframe(create_bqstorage_client=False)


def _cadastros(bq, docs: set) -> pd.DataFrame:
    """Resolve documentos para códigos e diz quem tem título e quem tem
    destinatário — o insumo do validador da lista."""
    sql = f"""
    WITH emp AS (
      SELECT
        codigo,
        nomeCompleto AS nome_cliente,
        cnpjCpf AS cnpj_cpf,
        REGEXP_REPLACE(COALESCE(cnpjCpf,''), r'\\D','') AS doc,
        (SELECT COUNT(DISTINCT LOWER(TRIM(JSON_EXTRACT_SCALAR(x,'$.email'))))
         FROM UNNEST(JSON_EXTRACT_ARRAY(COALESCE(NULLIF(contatos_json,''),'[]'))) x
         WHERE 'S' IN (JSON_EXTRACT_SCALAR(x,'$.recebeEmailCartaCobranca'),
                       JSON_EXTRACT_SCALAR(x,'$.recebeEmailCobranca'),
                       JSON_EXTRACT_SCALAR(x,'$.recebeEmailNfe'),
                       JSON_EXTRACT_SCALAR(x,'$.recebeEmailBoleto'))
           AND REGEXP_CONTAINS(
                 LOWER(TRIM(COALESCE(JSON_EXTRACT_SCALAR(x,'$.email'),''))),
                 r'^[^@\\s,;]+@[^@\\s,;]+\\.[a-z]{{2,}}$')) AS n_destinatarios,
        ARRAY(
          SELECT DISTINCT LOWER(TRIM(JSON_EXTRACT_SCALAR(x,'$.email')))
          FROM UNNEST(JSON_EXTRACT_ARRAY(COALESCE(NULLIF(contatos_json,''),'[]'))) x
          WHERE 'S' IN (JSON_EXTRACT_SCALAR(x,'$.recebeEmailCartaCobranca'),
                        JSON_EXTRACT_SCALAR(x,'$.recebeEmailCobranca'),
                        JSON_EXTRACT_SCALAR(x,'$.recebeEmailNfe'),
                        JSON_EXTRACT_SCALAR(x,'$.recebeEmailBoleto'))
            AND REGEXP_CONTAINS(
                  LOWER(TRIM(COALESCE(JSON_EXTRACT_SCALAR(x,'$.email'),''))),
                  r'^[^@\\s,;]+@[^@\\s,;]+\\.[a-z]{{2,}}$')
        ) AS emails
      FROM `{PROJECT_ID}.bronze.cigam__empresas`
      WHERE divisao.codigoDivisao IN ('10','11','12','90') AND codigo != '000679'
    ),
    t AS (
      -- X90 CONTA. Baixa contábil não é pagamento, e a régua seleciona o
      -- cliente contando com ela: excluir aqui rejeitava como "sem título"
      -- justamente quem a régua tinha acabado de escolher (Aço Verde 002554,
      -- Apia 000268, Agae 003057 — todos com dívida 100% X90).
      SELECT codigoEmpresa, COUNT(*) AS n_titulos
      FROM `{PROJECT_ID}.silver.titulos_cigam`
      WHERE saldo > 0
      GROUP BY 1
    ),
    b AS (
      SELECT cliente_cigam_pagante AS codigo, COUNT(*) AS n_bombas
      FROM `{PROJECT_ID}.gold.bombas_alocadas` GROUP BY 1
    )
    SELECT e.*, COALESCE(t.n_titulos,0) AS n_titulos, COALESCE(b.n_bombas,0) AS n_bombas
    FROM emp e
    LEFT JOIN t ON t.codigoEmpresa = e.codigo
    LEFT JOIN b ON b.codigo = e.codigo
    WHERE e.doc IN UNNEST(@docs) OR e.codigo IN UNNEST(@docs)
    """
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("docs", "STRING", sorted(docs))]))
    return job.to_dataframe(create_bqstorage_client=False)


# ------------------------------------------------------------------- fase A


def ingerir_lista(gc, lista_recebida: list):
    """Recebe a lista no formato da planilha de Frequência de Vencimento,
    classifica e escreve a Campanha_Input colorida. Não consulta o BigQuery.

    Passo separado do preparar() de propósito: entre um e outro alguém lê os
    CONFERIR. Na lista real, 79 de 194 caíram nessa faixa.
    """
    import entrada as ent
    p1 = io.abrir(gc, ID_DESTINO)
    ing = ent.ingerir(lista_recebida)
    print(ing.resumo())
    ent.escrever_campanha_input(p1, ing, io, ABA_INPUT)
    return ing


def _proximo_id(campanhas: list, quando) -> str:
    """2026-09-A, depois -B, -C. Deriva do que já existe no registro.

    Letra e não número porque campanha não é sequencial no mês: se você rodar
    duas em setembro, a segunda é a B, e fica óbvio na leitura do log qual veio
    antes sem precisar comparar datas.
    """
    prefixo = f"{quando:%Y-%m}"
    usadas = {
        str(l.get("id_campanha") or "")[len(prefixo) + 1:]
        for l in campanhas
        if str(l.get("id_campanha") or "").startswith(prefixo + "-")
    }
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    # AVANÇA depois da maior letra usada, não preenche lacuna. Preenchendo,
    # existindo só a -B a campanha nova nasceria -A: letra menor que uma
    # campanha anterior, e nome que pode ter sido usado e depois limpo do log.
    # Uma sequência que só cresce nunca reaproveita nome.
    # len == 1 é necessário: `"" in "ABC"` é True em Python, então um
    # id malformado tipo "2026-09-" (sufixo vazio) contava como letra A e a
    # próxima nascia B.
    validas = [l for l in usadas if len(l) == 1 and l in letras]
    if not validas:
        return f"{prefixo}-A"
    proxima = max(letras.index(l) for l in validas) + 1
    if proxima >= len(letras):
        raise RuntimeError(f"26 campanhas em {prefixo}? Confira o registro.")
    return f"{prefixo}-{letras[proxima]}"


def _dia_util(d, bq=None):
    """Empurra a data para o próximo dia útil.

    Prazo de pagamento caindo no fim de semana é prazo que o cliente não tem —
    o banco não compensa. Feriado é o mesmo problema e não dá para deduzir do
    dia da semana: 07/09 e 12/10 caem em dia útil no calendário e não existem
    para pagamento.

    Com `bq`, usa silver.calendario_dias_uteis — o MESMO calendário que a
    titulos_cigam usa para a carência de inadimplência. Isso importa: se o
    e-mail concede prazo até um dia que o DW já conta como atraso, as duas
    metades do sistema discordam sobre o mesmo dia.

    Sem `bq`, pula só fim de semana e AVISA que feriado não foi checado, em vez
    de dar a impressão de que foi.
    """
    from datetime import timedelta

    if bq is not None:
        linhas = list(bq.query(f"""
            SELECT eh_dia_util, proximo_dia_util
            FROM `{PROJECT_ID}.silver.calendario_dias_uteis`
            WHERE data = DATE '{d:%Y-%m-%d}'
        """).result())
        if not linhas:
            print(f"  {d:%d/%m/%Y} fora do calendário do DW; usando só fim de semana")
        elif not linhas[0].eh_dia_util:
            novo = linhas[0].proximo_dia_util
            print(f"  {d:%d/%m} não é dia útil (fim de semana ou feriado) "
                  f"-> {novo:%d/%m}")
            return novo
        else:
            return d

    original = d
    while d.weekday() >= 5:
        d += timedelta(days=1)
    if d != original:
        print(f"  {original:%d/%m} caía em fim de semana -> {d:%d/%m}")
    print("  (sem `bq`: FERIADO não foi verificado)")
    return d


def nova_campanha(gc, bq=None, prazo_dias: int = 13, data_envio=None,
                  copia_padrao=("contato.financeiro@ctasmart.com.br",),
                  teto_diario: int = 450,
                  forcar_nova: bool = False) -> "campanha_v2.Campanha":
    """Abre a campanha do dia, ou REAPROVEITA a que ainda não foi disparada.

    O que define uma campanha é o E-MAIL QUE SAIU. Antes o id era consumido na
    régua: `candidatos(campanha=c)` gravava a linha `regua` no registro e o
    próximo id avançava. Calibrar a régua três vezes queimava -C, -D e -E sem
    nada ter sido enviado, e a numeração deixava de descrever a realidade.

    Agora só campanha DISPARADA encerra um id. Enquanto não houver envio em
    produção, rodar isto devolve a mesma campanha — a régua e o preparar podem
    ser refeitos à vontade.

        data_envio  = hoje, e é a âncora do cálculo de encargos. Se a campanha
                      reaproveitada já tem prévia CONGELADA, a data original é
                      preservada: recalcular mudaria valores já congelados.
        data_limite = data_envio + prazo_dias, empurrado para dia útil

    forcar_nova=True abre outra mesmo havendo rascunho — para rodar duas
    campanhas em paralelo de propósito.
    """
    from datetime import date, timedelta

    envio_em = data_envio or date.today()
    p1 = io.abrir(gc, ID_DESTINO)

    registro = io.ler_aba(p1, ABA_CAMPANHAS, obrigatoria=False)
    previa = io.ler_aba(p1, ABA_PREVIA, obrigatoria=False)
    log = io.ler_aba(p1, ABA_LOG, obrigatoria=False)

    def _ids(linhas):
        return {str(l.get("id_campanha") or "").strip() for l in linhas} - {""}

    # DISPARADA = e-mail de produção saiu. É o que encerra o id.
    disparadas = {str(l.get("id_campanha") or "").strip() for l in log
                  if str(l.get("status") or "").upper() == "ENVIADO"
                  and str(l.get("modo") or "PRODUCAO").upper() == "PRODUCAO"}
    disparadas |= {str(l.get("id_campanha") or "").strip() for l in registro
                   if str(l.get("fase") or "").startswith("disparado")}
    disparadas -= {""}

    rascunhos = sorted((_ids(registro) | _ids(previa)) - disparadas)

    if rascunhos and not forcar_nova:
        id_campanha = rascunhos[-1]
        congelada = [l for l in previa
                     if str(l.get("id_campanha") or "").strip() == id_campanha]

        if congelada and str(congelada[0].get("data_envio") or "").strip():
            # Prévia congelada manda: os encargos foram calculados naquela data.
            a, m, d = str(congelada[0]["data_envio"]).strip()[:10].split("-")
            envio_em = date(int(a), int(m), int(d))
            lim = str(congelada[0].get("data_limite") or "").strip()[:10]
            limite = (date(*map(int, lim.split("-"))) if lim
                      else _dia_util(envio_em + timedelta(days=prazo_dias), bq))
            print(f"{id_campanha} REAPROVEITADA — já tem prévia congelada "
                  f"({len(congelada)} cliente[s]); datas originais preservadas.")
        else:
            limite = _dia_util(envio_em + timedelta(days=prazo_dias), bq)
            print(f"{id_campanha} REAPROVEITADA — existe no registro e nunca "
                  f"foi disparada; datas atualizadas para hoje.")
        print(f"  (para abrir outra assim mesmo: forcar_nova=True)")
    else:
        usados = _ids(registro) | _ids(previa) | _ids(log)
        id_campanha = _proximo_id([{"id_campanha": i} for i in usados], envio_em)
        limite = _dia_util(envio_em + timedelta(days=prazo_dias), bq)

    c = campanha_v2.Campanha(
        id_campanha=id_campanha,
        data_envio=envio_em,
        data_limite=limite,
        copia_padrao=tuple(copia_padrao),
        teto_diario=teto_diario,
    )
    print(f"{id_campanha} | envio {envio_em:%d/%m/%Y} | "
          f"prazo até {limite:%d/%m/%Y} ({c.dias_de_prazo} dias)")
    if limite != envio_em + timedelta(days=prazo_dias):
        print(f"  (prazo empurrado para dia útil)")

    if disparadas:
        ult = sorted(disparadas)[-1]
        fechada = [l for l in registro
                   if str(l.get("id_campanha") or "").strip() == ult
                   and str(l.get("clientes") or "").strip()]
        quando = fechada[-1].get("data_envio") if fechada else "?"
        quantos = fechada[-1].get("clientes") if fechada else "?"
        print(f"  última disparada: {ult} em {quando} — {quantos} cliente(s)")
    return c


def criar_painel_campanhas(gc):
    """Cria, UMA VEZ, a aba de leitura com fórmulas sobre o registro.

    Por que uma aba separada e não fórmulas na própria `Campanhas`: o
    escrever_aba limpa conteúdo E formatação da aba que escreve — foi o que
    apagou formato e fez R$ 120,38 virar 1900-04-29. Fórmula colocada na
    `Campanhas` seria apagada no próximo append.

    Então a divisão é: `Campanhas` é append-only, escrita só pelo script, e
    esta aba lê de lá. O script nunca toca aqui, e você pode acrescentar
    coluna, gráfico e filtro sem medo de perder no próximo disparo.

    Se a aba já existe, NÃO mexe — suas alterações ficam.
    """
    p1 = io.abrir(gc, ID_DESTINO)
    try:
        p1.worksheet(ABA_PAINEL_CAMPANHAS)
        print(f"'{ABA_PAINEL_CAMPANHAS}' já existe; não foi alterada.")
        return
    except Exception:
        pass

    ws = p1.add_worksheet(title=ABA_PAINEL_CAMPANHAS, rows="300", cols="14")

    # As fórmulas buscam a coluna PELO NOME, com MATCH no cabeçalho. Posição
    # fixa (Col5, Col6) quebraria: o append_log acrescenta coluna nova quando
    # uma fase traz métrica que ainda não existe, e a ordem muda.
    A = ABA_CAMPANHAS

    def v(fase, coluna, id_ref="$B$4"):
        """Valor de uma métrica, da última linha daquela fase da campanha."""
        return (f'=IFERROR(INDEX(FILTER({A}!$A:$Z;'
                f'{A}!$A:$A={id_ref};'
                f'INDEX({A}!$A:$Z;0;MATCH("fase";{A}!$1:$1;0))="{fase}");'
                f'COUNTA(FILTER({A}!$A:$A;{A}!$A:$A={id_ref};'
                f'INDEX({A}!$A:$Z;0;MATCH("fase";{A}!$1:$1;0))="{fase}"));'
                f'MATCH("{coluna}";{A}!$1:$1;0));"—")')

    linhas = [
        ["Controle de campanhas"],
        [f"Lê a aba {A}, escrita pelo script. Esta aba é sua: nada aqui é "
         f"sobrescrito."],
        [],
        ["Campanha:", f'=IFERROR(INDEX(SORT(UNIQUE(FILTER({A}!$A:$A;'
                      f'{A}!$A:$A<>"";{A}!$A:$A<>"id_campanha"));1;0);1);"—")',
         "", "(a mais recente; troque para ver outra)"],
        [],
        ["FUNIL DA RÉGUA"],
        ["Clientes com dívida vencida",   v("regua", "com_divida")],
        ["  fora por acordo vigente",     v("regua", "fora_acordo")],
        ["  fora por Nunca_Notificar",    v("regua", "fora_excecoes")],
        ["Passaram na frequência",        v("regua", "passou_frequencia")],
        ["  fora por frequência própria", v("regua", "fora_freq_propria")],
        ["  SEM ALOCAÇÃO (não avaliáveis)", v("regua", "sem_alocacao"),
         v("regua", "valor_sem_alocacao"), "<- fila do mutirão"],
        ["ENTRARAM NA RÉGUA",             v("regua", "entram"),
         v("regua", "valor_entram")],
        [],
        ["PARÂMETROS USADOS"],
        ["freq_minima",          v("regua", "freq_minima")],
        ["freq_propria_minima",  v("regua", "freq_propria_minima")],
        ["dias_uso",             v("regua", "dias_uso")],
        [],
        ["RESULTADO"],
        ["Clientes congelados",  v("preparado", "clientes")],
        ["Valor cobrado",        v("preparado", "valor_cobrado")],
        ["E-mails enviados",     v("disparado", "enviados")],
        ["  falhas",             v("disparado", "falhas")],
        ["Quitaram",             v("reconciliado", "quitou")],
        ["Pagaram em parte",     v("reconciliado", "pagou_parcial")],
        ["Mantêm bloqueio",      v("reconciliado", "mantem_bloqueio")],
        ["VALOR RECUPERADO",     v("reconciliado", "valor_recuperado")],
        ["% do cobrado",
         '=IFERROR(IF(N(B28)=0;"—";TEXT(N(B28)/N(B22);"0,0%"));"—")'],
        [],
        ["HISTÓRICO — todas as linhas, mais recente primeiro"],
        [f'=IFERROR(QUERY({A}!A:Z;"select * order by Col1 desc";1);'
         f'"(nada registrado ainda)")'],
    ]
    ws.update(linhas, "A1", value_input_option="USER_ENTERED")

    for faixa in ("A1", "A6", "A15", "A20", "A31"):
        ws.format(faixa, {"textFormat": {"bold": True}})
    ws.format("A1", {"textFormat": {"bold": True, "fontSize": 13}})
    ws.format("A13:C13", {"textFormat": {"bold": True}})
    ws.format("A28:C28", {"textFormat": {"bold": True}})
    ws.format("A2", {"textFormat": {"fontSize": 9,
                     "foregroundColor": {"red": .5, "green": .5, "blue": .5}}})
    ws.freeze(rows=4)
    print(f"'{ABA_PAINEL_CAMPANHAS}' criada: funil da régua, parâmetros, "
          f"resultado e histórico. O script não reescreve esta aba.")


ABA_RESUMO = "Campanhas_Resumo"


def auditar_titulos(c, gc, bq, escrever: bool = True, desde=None) -> dict:
    """O que aconteceu com CADA título da campanha, com data e motivo.

    Substitui a comparação com a base inteira, que era um erro de desenho: a
    régua seleciona os piores pagantes, então compará-los com a média mede a
    seleção, não a campanha. O que responde é factual e por título.

    Cada baixa é um lançamento próprio (codigoTipo 'E', ligado pelo
    codigoPartida) com data, valor, portador e histórico. O PORTADOR diz a
    natureza, e é isso que separa recuperação de movimentação contábil:

      C0x, I0x, Y01   pagamento em banco — dinheiro que entrou
      X30             abatimento — motivo escrito no histórico
      X90/X91/X92     baixa contábil — não é pagamento
      X50             outro; sai nomeado para não virar "pago" por omissão

    JUROS entram pela SITUACAO, não pelo portador. Juros recebido é lançamento
    próprio na conta 103001 com situacao 'J', ligado ao título pelo mesmo
    codigoPartida. Filtrar só ('L','U') perdia todos: na 2026-09-B eram
    R$ 697,33 em 11 títulos de 5 clientes, invisíveis no total.

    Isso importa para a discussão com o comercial: o valor cobrado no aviso é
    principal + encargos, então medir só o principal recebido subestima o que
    a campanha trouxe. A Lactopar pagou R$ 281,25 de principal e R$ 31,83 de
    juros — exatamente os R$ 313,08 do card.

    Na 2026-09-B: R$ 14.674,77 pagos em banco, mas R$ 6.134,13 de baixa
    contábil e R$ 1.974,00 de abatimento ("NF de retorno 25704"). Uma conta que
    olhasse só a variação de saldo somaria os três e reportaria R$ 22 mil de
    recuperação, dos quais R$ 8 mil nunca entraram.
    """
    import pandas as pd

    p1 = io.abrir(gc, ID_DESTINO)
    congelados = [l for l in io.ler_aba(p1, ABA_TITULOS, obrigatoria=False)
                  if str(l.get("id_campanha") or "").strip() == c.id_campanha]
    if not congelados:
        raise RuntimeError(f"Nada em '{ABA_TITULOS}' para {c.id_campanha}.")

    # JANELA. Comeca no aviso desta campanha, mas o titulo congelado pode ter
    # sido pago numa rodada anterior: a mesma divida atravessa campanhas.
    # Leonardo Leal pagou R$ 5.035 em 08 e 09/09 e Aguiar R$ 793,56 em 04/09;
    # com a janela abrindo em 21/09 os dois saiam como SEM MOVIMENTO, embora o
    # dinheiro tivesse entrado.
    inicio = desde or c.data_envio
    if desde is None:
        from datetime import date as _date
        datas = [str(l.get("data_envio") or "").strip()[:10]
                 for l in io.ler_aba(p1, ABA_PREVIA, obrigatoria=False)
                 if str(l.get("id_campanha") or "").strip() == c.id_campanha
                 and str(l.get("data_envio") or "").strip()]
        if datas:
            a_, m_, d_ = min(datas).split("-")
            congelada = _date(int(a_), int(m_), int(d_))
            if congelada < inicio:
                print(f"  janela recuada para {congelada:%d/%m/%Y} "
                      f"(data congelada na previa)")
                inicio = congelada

    ids = sorted({str(l.get("codigoLancamento")
                      or l.get("codigolancamento") or "").strip()
                  for l in congelados} - {""})
    if not ids:
        raise RuntimeError(
            f"Sem codigoLancamento em '{ABA_TITULOS}' — não dá para auditar "
            f"título a título.")

    sql = f"""
    SELECT
      l.codigoPartida AS codigoLancamento,
      l.codigoEmpresa AS codigo_cliente,
      SAFE.PARSE_DATE('%d/%m/%Y', l.data) AS data_baixa,
      l.codigoPortador AS portador,
      SAFE_CAST(l.valor AS FLOAT64) AS valor,
      NULLIF(TRIM(l.complementoHistorico), '') AS motivo,
      l.situacao AS situacao,
      CASE
        WHEN l.situacao = 'J'                                THEN 'JUROS'
        WHEN STARTS_WITH(COALESCE(l.codigoPortador,''),'C')
          OR STARTS_WITH(COALESCE(l.codigoPortador,''),'I')
          OR STARTS_WITH(COALESCE(l.codigoPortador,''),'Y')  THEN 'PAGO'
        WHEN l.codigoPortador = 'X30'                        THEN 'ABATIDO'
        WHEN STARTS_WITH(COALESCE(l.codigoPortador,''),'X9') THEN 'BAIXA CONTABIL'
        ELSE CONCAT('OUTRO (', COALESCE(l.codigoPortador,'?'), ')')
      END AS natureza
    FROM `{PROJECT_ID}.silver.lancamentos_receber` l
    WHERE l.codigoTipo = 'E' AND l.situacao IN ('L','U','J')
      AND l.codigoPartida IN UNNEST(@ids)
      AND SAFE.PARSE_DATE('%d/%m/%Y', l.data) >= DATE '{inicio:%Y-%m-%d}'
    ORDER BY 2, 3
    """
    eventos = [dict(r) for r in bq.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids","STRING",ids)])).result()]

    por_titulo = {}
    for e in eventos:
        por_titulo.setdefault(str(e["codigoLancamento"]), []).append(e)

    # Acordo firmado DEPOIS do aviso: quem está na prévia passou pelo filtro,
    # logo não tinha acordo vigente quando foi avisado.
    em_acordo = set()
    try:
        import acordos as ac
        reg = ac.carregar(gc, io)
        em_acordo = {cod for cod in {_codigo(l.get("codigo_cliente"))
                                     for l in congelados}
                     if reg.status(cod) in (ac.PAGANDO, ac.QUEBRADO)}
    except Exception as exc:
        print(f"  (acordos não lidos: {type(exc).__name__})")

    linhas, resumo = [], {}
    for t in congelados:
        tid = str(t.get("codigoLancamento") or t.get("codigolancamento") or "").strip()
        cod = _codigo(t.get("codigo_cliente"))
        evs = por_titulo.get(tid, [])

        if not evs:
            estado = "ACORDADO (sem baixa)" if cod in em_acordo else "SEM MOVIMENTO"
        else:
            naturezas = {e["natureza"] for e in evs}
            # JUROS sozinho não é estado do título — é acessório do pagamento.
            estado = ("PAGO" if "PAGO" in naturezas
                      else sorted(naturezas - {"JUROS"})[0]
                      if naturezas - {"JUROS"} else "JUROS PAGO (principal em aberto)")

        resumo[estado] = resumo.get(estado, 0) + 1
        linhas.append({
            "id_campanha": c.id_campanha,
            "codigo_cliente": cod,
            "nome_cliente": t.get("nome_cliente"),
            "codigoLancamento": tid,
            "doc": t.get("doc"),
            "saldo_no_aviso": _numero_br(t.get("saldo")),
            "estado": estado,
            "baixas": len(evs),
            "valor_pago": round(sum(e["valor"] for e in evs
                                    if e["natureza"] == "PAGO"), 2),
            "valor_juros": round(sum(e["valor"] for e in evs
                                     if e["natureza"] == "JUROS"), 2),
            "valor_abatido": round(sum(e["valor"] for e in evs
                                       if e["natureza"] == "ABATIDO"), 2),
            "valor_baixa_contabil": round(sum(e["valor"] for e in evs
                                              if e["natureza"] == "BAIXA CONTABIL"), 2),
            "valor_outro": round(sum(e["valor"] for e in evs
                                     if e["natureza"].startswith("OUTRO")), 2),
            "data_primeira_baixa": min((e["data_baixa"] for e in evs), default=None),
            "motivos": " | ".join(sorted({str(e["motivo"]) for e in evs
                                          if e["motivo"]}))[:200],
            "cliente_em_acordo": cod in em_acordo,
        })

    tot = lambda k: round(sum(l[k] for l in linhas), 2)
    print(f"\nAUDITORIA — {c.id_campanha}, {len(linhas)} título(s) "
          f"desde {c.data_envio:%d/%m}\n")
    for est, n in sorted(resumo.items(), key=lambda x: -x[1]):
        print(f"   {est:<22} {n:>4}")
    print(f"\n   principal pago     R$ {tot('valor_pago'):>12,.2f}  <- entrou")
    if tot("valor_juros"):
        print(f"   juros recebidos    R$ {tot('valor_juros'):>12,.2f}  <- entrou")
        print(f"   TOTAL RECEBIDO     R$ "
              f"{tot('valor_pago') + tot('valor_juros'):>12,.2f}")
    for k, rot in (("valor_abatido","abatido"),
                   ("valor_baixa_contabil","baixa contábil"),
                   ("valor_outro","outro portador")):
        if tot(k):
            print(f"   {rot:<18} R$ {tot(k):>12,.2f}  <- NÃO entrou")

    if escrever:
        io.escrever_aba(p1, "Campanha_Auditoria", pd.DataFrame(linhas))
    return {"linhas": linhas, "resumo": resumo}


ABA_GERENTES = "Gerentes"
ABA_RESUMO_GERENTE = "Resumo_Gerentes"

# Usado só para CRIAR a aba na primeira vez. Depois disso a aba manda, porque
# vendedor troca de gerente e ninguém vai abrir um .py para registrar isso.
GERENTES_INICIAIS = {
    "MARLON SILVA": "Carlão",
    "PATRICK RIBEIRO": "Meneghini",
    "VIVIAN OLIVEIRA": "Meneghini",
    "GUILHERME PIO PIMENTA": "Meneghini",
}
GERENTE_PADRAO = "João Pedro"


def _norm_nome(v) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(v or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().upper()


def _mapa_gestores(p1):
    """Lê vendedor -> (gerente, e-mail do gerente) da aba Email_Vendedores.

    Ficava numa aba `Gerentes` separada, e isso duplicava cadastro: o vendedor
    já mora na Email_Vendedores. Colunas esperadas:

        Vendedor | Email | Ativo | Gerente | Email_Gerente

    As duas últimas são novas. Sem elas nada quebra — o vendedor cai no
    GERENTE_PADRAO e o gestor sem e-mail sai listado em vez de sumir.
    """
    mapa, emails = {}, {}
    for l in io.ler_aba(p1, ABA_EMAIL_VEND, obrigatoria=False):
        v = _norm_nome(l.get("vendedor") or l.get("Vendedor"))
        g = str(l.get("gerente") or l.get("Gerente") or "").strip()
        e = str(l.get("email_gerente") or l.get("Email_Gerente") or "").strip()
        if v and g:
            mapa[v] = g
            if e and "@" in e:
                emails[g] = e
    if not mapa:
        mapa = {_norm_nome(k): v for k, v in GERENTES_INICIAIS.items()}
        print(f"  '{ABA_EMAIL_VEND}' sem coluna Gerente — usando o mapa "
              f"embutido. Acrescente `Gerente` e `Email_Gerente` lá.")
    return mapa, emails


def _nomes_equip(dados, nome_cliente: str) -> str:
    """'Fazenda Boa Vista - S10 (21130); Sede - Arla (21370)'.

    Tira o nome do cliente repetido no começo do nome da bomba, e cai no
    serial puro quando a view não tem nome.
    """
    if not dados or not dados.get("equip"):
        return ""
    import template as _t
    saida = []
    for serial, bruto in dados["equip"].items():
        nome = _t.limpar_nome_bomba(bruto, nome_cliente) if bruto else ""
        saida.append(f"{nome} ({serial})" if nome else str(serial))
    return "; ".join(sorted(saida))


def resumo_por_gerente(c, gc, bq=None, escrever: bool = True) -> dict:
    """Quadro por gerente: quem entrou, POR QUÊ, e quem você vetou.

    Duas fontes, de propósito:
      Campanha_Previa  -> quem entrou de fato
      Campanha_Input   -> TODOS os candidatos, inclusive os vetados na `acao`

    Mostrar os vetados junto é o que torna o relatório defensável. Sem eles o
    gerente vê só a lista do que foi cobrado e não sabe que a régua também
    poupou clientes da equipe dele — nem por qual motivo. Com eles, a conversa
    deixa de ser "por que cobraram fulano" e passa a ser sobre o critério.

    O vendedor dos vetados vem da Contatos_Emails: eles não passaram pelo
    preparar(), que é quem normalmente resolve isso.

    escrever=False roda como PRÉVIA — imprime e não toca na planilha.
    """
    import pandas as pd

    p1 = io.abrir(gc, ID_DESTINO)

    mapa, _ = _mapa_gestores(p1)

    previa = {_codigo(l.get("codigo_cliente")): l
              for l in io.ler_aba(p1, ABA_PREVIA, obrigatoria=False)
              if str(l.get("id_campanha") or "").strip() == c.id_campanha}
    entrada = {_codigo(l.get("codigo") or l.get("codigo_cliente")): l
               for l in io.ler_aba(p1, ABA_INPUT, obrigatoria=False)}
    if not previa and not entrada:
        raise RuntimeError(f"Nem '{ABA_PREVIA}' nem '{ABA_INPUT}' têm dados.")

    # CARTEIRA da Base_Clientes (planilha 2). A coluna `vendedor` da
    # Contatos_Emails é o `representante` do CIGAM e só serve de reserva.
    #
    # Vem DEPOIS de previa/entrada: a resolução por CNPJ precisa dos
    # documentos dos clientes da campanha, e tentar antes dava
    # UnboundLocalError.
    vendedor_de, cart = {}, None
    try:
        import vendedores as _v
        p2 = io.abrir(gc, ID_VENDEDORES)
        cart = _v.carteira_de_base_clientes(
            io.ler_aba(p2, ABA_BASE_CLIENTES, obrigatoria=False))
        vendedor_de.update(cart.get("por_codigo", {}) if cart else {})
    except Exception as exc:
        print(f"  (Base_Clientes não lida: {type(exc).__name__}; "
              f"vendedor virá da Contatos_Emails)")

    # A Base_Clientes NÃO tem coluna de código, só CNPJ. Sem resolver por
    # CNPJ, `por_codigo` vem vazio e todo mundo cai no fallback da
    # Contatos_Emails, que é o `representante` do CIGAM.
    doc_de = {}
    for l in io.ler_aba(p1, ABA_CONTATOS, obrigatoria=False):
        cod = _codigo(l.get("codigo_cliente"))
        if cod:
            doc_de.setdefault(cod, l.get("cnpj_cpf"))
    for l in previa.values():
        cod = _codigo(l.get("codigo_cliente"))
        if cod and not doc_de.get(cod):
            doc_de[cod] = l.get("cnpj_cpf")
    for l in entrada.values():
        cod = _codigo(l.get("codigo") or l.get("codigo_cliente"))
        if cod and not doc_de.get(cod):
            doc_de[cod] = l.get("cnpj")

    por_cnpj = (cart or {}).get("por_cnpj", {})
    if por_cnpj:
        achados = 0
        for cod, doc in doc_de.items():
            if cod in vendedor_de:
                continue
            for ch in _v._chaves_doc(doc):
                if ch in por_cnpj:
                    vendedor_de[cod] = por_cnpj[ch]
                    achados += 1
                    break
        print(f"  carteira por CNPJ: {achados} cliente(s) resolvido(s)")

    reserva = 0
    for l in io.ler_aba(p1, ABA_CONTATOS, obrigatoria=False):
        cod = _codigo(l.get("codigo_cliente"))
        nome = str(l.get("vendedor") or "").strip()
        if cod and nome and cod not in vendedor_de:
            vendedor_de[cod] = nome
            reserva += 1
    if reserva:
        print(f"  {reserva} cliente(s) SEM carteira na Base_Clientes — usei o "
              f"`representante` do CIGAM, que pode estar desatualizado.")


    # EQUIPAMENTOS. Do snapshot congelado quando existe; senão das colunas da
    # régua. O gestor pediu isso e tem razão: é o parque do cliente dele que
    # para, e ele precisa saber o tamanho antes de o bloqueio acontecer.
    #
    # Duas informações vão junto porque mudam a decisão, não só o volume:
    # bomba em CNPJ de terceiro afeta quem não foi notificado, e sistema com
    # pagante adimplente não pode ser bloqueado inteiro.
    parque = {}
    for b in io.ler_aba(p1, ABA_BOMBAS, obrigatoria=False):
        if str(b.get("id_campanha") or "").strip() != c.id_campanha:
            continue
        d = parque.setdefault(_codigo(b.get("codigo_cliente")),
                              {"seriais": set(), "equip": {}, "terceiros": set(),
                               "adimplentes": 0, "sistemas": set()})
        serial = b.get("serial_equipamento")
        if serial not in (None, ""):
            d["seriais"].add(str(serial))
            # NOME da bomba, não só o serial: quem conhece o cliente reconhece
            # "Fazenda Santa Rita - S10", não "21130". Indexado por serial
            # porque o bloqueio é do equipamento, e dois canais do mesmo
            # equipamento viram uma linha só.
            import template as _t
            nome = _t.limpar_nome_bomba(b.get("bomba_nome"),
                                        b.get("nome_cliente") or "")
            if nome:
                d["equip"].setdefault(str(serial), nome)
        if _bool_valor(b.get("instalada_em_terceiro")):
            d["terceiros"].add(str(b.get("local_nome") or "terceiro").strip())
        try:
            d["adimplentes"] = max(d["adimplentes"],
                                   int(float(b.get("bombas_de_adimplentes") or 0)))
        except ValueError:
            pass
        if b.get("cliente_id"):
            d["sistemas"].add(str(b["cliente_id"]))

    # Quem foi VETADO não passou pelo preparar(), logo não está na
    # Campanha_Bombas e ficava só com o serial — justamente o cliente sobre o
    # qual o gestor mais precisa de contexto, porque é o que ele pode
    # reverter. Com `bq`, busca o nome direto na view.
    if bq is not None:
        faltam = [cod for cod in set(entrada) | set(previa) if cod not in parque]
        if faltam:
            for r_ in bq.query(f"""
                SELECT cliente_cigam_pagante AS codigo,
                       CAST(serial_equipamento AS STRING) AS serial,
                       ANY_VALUE(bomba_nome) AS nome
                FROM `{PROJECT_ID}.gold.bombas_alocadas`
                WHERE cliente_cigam_pagante IN UNNEST(@cods)
                  AND serial_equipamento IS NOT NULL
                GROUP BY 1, 2
            """, job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ArrayQueryParameter("cods", "STRING", faltam)
                 ])).result():
                d = parque.setdefault(r_.codigo,
                                      {"seriais": set(), "equip": {},
                                       "terceiros": set(), "adimplentes": 0,
                                       "sistemas": set()})
                d["seriais"].add(r_.serial)
                d["equip"].setdefault(r_.serial, r_.nome or "")

    desfecho = {}
    for l in io.ler_aba(p1, "Campanha_Auditoria", obrigatoria=False):
        if str(l.get("id_campanha") or "").strip() != c.id_campanha:
            continue
        d = desfecho.setdefault(_codigo(l.get("codigo_cliente")),
                                {"estados": set(), "recebido": 0.0})
        d["estados"].add(str(l.get("estado") or ""))
        d["recebido"] += _numero_br(l.get("valor_pago")) + _numero_br(l.get("valor_juros"))

    def _desde(v):
        t = str(v or "").strip()[:10]
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else t

    def _resultado(cod):
        e = desfecho.get(cod, {}).get("estados", set())
        if "PAGO" in e: return "PAGOU"
        if any("ACORDADO" in x for x in e): return "NEGOCIOU"
        if any("X50" in x for x in e): return "UNIFICOU"
        if any("BAIXA" in x for x in e): return "baixa contábil"
        if any("ABATIDO" in x for x in e): return "abatido"
        return "sem movimento" if e else ""

    linhas = []
    for cod in sorted(set(previa) | set(entrada)):
        r = entrada.get(cod, {})
        p = previa.get(cod, {})
        veto = str(r.get("acao") or "").strip()
        # Carteira atual tem precedência sobre a congelada: vendedor troca de
        # cliente, e o relatório vai para o gestor de HOJE.
        vend = (vendedor_de.get(cod, "")
                or str(p.get("vendedor") or "").strip() or "(sem vendedor)")

        motivo = []
        if r.get("frequencia_propria"):
            motivo.append(f"{r['frequencia_propria']} mês(es) vencido(s)")
            if str(r.get("meses_ano_corrente") or "") != "":
                motivo[-1] += f", {r['meses_ano_corrente']} em {c.data_envio:%Y}"
        if r.get("frequencia") and str(r["frequencia"]) != str(r.get("frequencia_propria")):
            motivo.append(f"grupo com {r['frequencia']}")
        if r.get("vencimento_mais_antigo"):
            motivo.append(f"devendo desde {_desde(r['vencimento_mais_antigo'])}")
        if r.get("ultima_utilizacao"):
            motivo.append(f"abasteceu em {_desde(r['ultima_utilizacao'])}")

        linhas.append({
            "gerente": mapa.get(_norm_nome(vend), GERENTE_PADRAO),
            "vendedor": vend,
            "sem_mapeamento": _norm_nome(vend) not in mapa,
            "codigo_cliente": cod,
            "cliente": p.get("nome_cliente") or r.get("cliente"),
            # Antes do preparar() a prévia não existe: todo mundo cairia em
            # "fora da régua", que é o oposto da verdade. O relatório roda nos
            # DOIS momentos — antes do disparo, para o gerente poder vetar, e
            # depois, para prestar contas — e precisa dizer a coisa certa em
            # cada um.
            # Um estado só para quem vai ser cobrado, congelado ou não.
            # Havia dois — "cobrado" e "A COBRAR" — e o e-mail do gestor só
            # montava bloco para o primeiro: antes do preparar() o quadro saía
            # vazio com total zerado, que foi o que você viu.
            # A distinção entre congelado e não congelado vira uma coluna
            # própria, porque muda o VALOR (com ou sem encargos), não se o
            # cliente entra.
            "situacao": ("VETADO" if veto
                         else "cobrado" if (cod in previa or cod in entrada)
                         else "fora da régua"),
            "congelado": cod in previa,
            "motivo_veto": veto,
            "devendo_desde": _desde(r.get("vencimento_mais_antigo")),
            "titulos": p.get("titulos") or r.get("titulos_abertos") or "",
            "total": _numero_br(p.get("total")) or _numero_br(r.get("em_atraso")),
            "por_que_entrou": "; ".join(motivo) or "(sem evidência)",
            "equipamentos": (len(parque[cod]["seriais"]) if cod in parque
                             else (r.get("equipamentos") or "")),
            "seriais": ("; ".join(sorted(parque[cod]["seriais"])) if cod in parque
                        else str(r.get("seriais") or "")),
            # "NOME (serial)" quando o snapshot tem o nome; só o serial quando
            # veio da régua, que não carrega essa coluna.
            "equipamentos_nomes": _nomes_equip(parque.get(cod),
                                               p.get("nome_cliente")
                                               or r.get("cliente") or ""),
            "em_terceiro": ("; ".join(sorted(parque[cod]["terceiros"]))
                            if cod in parque else ""),
            "sistema_com_adimplente": (parque[cod]["adimplentes"]
                                       if cod in parque else ""),
            "resultado": _resultado(cod),
            "recebido": round(desfecho.get(cod, {}).get("recebido", 0.0), 2),
            "id_campanha": c.id_campanha,
        })

    linhas.sort(key=lambda l: (l["gerente"], l["vendedor"], -l["total"]))

    antes = not previa
    nao_congelados = sum(1 for l in linhas
                         if l["situacao"] == "cobrado" and not l["congelado"])
    print(f"\n{'#'*74}\n#  RELATÓRIO DE BLOQUEIOS — campanha {c.id_campanha}")
    if antes:
        print(f"#  AINDA NÃO ENVIADO — envio previsto {c.data_envio:%d/%m/%Y}, "
              f"prazo até {c.data_limite:%d/%m/%Y}")
        print(f"#  Este é o momento de vetar: escreva o motivo na coluna `acao`")
        print(f"#  da Campanha_Input e rode de novo.")
    else:
        print(f"#  aviso em {c.data_envio:%d/%m/%Y}, "
              f"prazo até {c.data_limite:%d/%m/%Y}")
    if not escrever:
        print(f"#  PRÉVIA — nada foi escrito na planilha")
    if nao_congelados:
        print(f"#  {nao_congelados} cliente(s) ainda NÃO congelado(s): valores "
              f"são o saldo da régua, sem encargos.")
        print(f"#  Rode preparar() antes de enviar aos gestores, para o total "
              f"bater com o que o cliente vai receber.")
    print("#" * 74)

    for ger in sorted({l["gerente"] for l in linhas}):
        dele = [l for l in linhas if l["gerente"] == ger]
        cobrados = [l for l in dele if l["situacao"] in ("cobrado", "A COBRAR")]
        vetados = [l for l in dele if l["situacao"] == "VETADO"]
        tot = sum(l["total"] for l in cobrados)
        rec = sum(l["recebido"] for l in cobrados)

        print(f"\n\n{'='*74}\n{ger.upper()}")
        rotulo = ("A COBRAR" if cobrados and cobrados[0]["situacao"] == "A COBRAR"
                  else "cobrado(s)")
        print(f"{len(cobrados)} cliente(s) {rotulo}, R$ {tot:,.2f}"
              + (f"  ·  R$ {rec:,.2f} recebidos" if rec else "")
              + (f"  ·  {len(vetados)} vetado(s)" if vetados else ""))

        vend_ant = None
        for l in cobrados:
            if l["vendedor"] != vend_ant:
                print(f"\n  {l['vendedor']}")
                vend_ant = l["vendedor"]
            res = f"   [{l['resultado']}]" if l["resultado"] else ""
            print(f"    {str(l['cliente'])[:40]:<40} R$ {l['total']:>10,.2f}"
                  f"  {l['titulos']:>3} tít.{res}")
            print(f"       {l['por_que_entrou']}")

        if vetados:
            print(f"\n  --- NÃO cobrados, retirados por decisão ---")
            for l in vetados:
                print(f"    {str(l['cliente'])[:40]:<40} R$ {l['total']:>10,.2f}"
                      f"   ({l['vendedor']})")
                print(f"       motivo: {l['motivo_veto']}")
                print(f"       entraria por: {l['por_que_entrou']}")

    sem_mapa = sorted({l["vendedor"] for l in linhas if l["sem_mapeamento"]})
    if sem_mapa:
        print(f"\n\n  vendedor(es) sem gerente em '{ABA_GERENTES}', "
              f"caíram em {GERENTE_PADRAO}:\n     {', '.join(sem_mapa)}")

    if escrever:
        io.escrever_aba(p1, ABA_RESUMO_GERENTE, pd.DataFrame(linhas))
    else:
        print(f"\n  (prévia — rode com escrever=True para gravar "
              f"em '{ABA_RESUMO_GERENTE}')")
    return {"linhas": linhas}


def enviar_resumo_gestores(c, gc, bq=None, enviar_fn=None, criterio: str = "",
                           copia_gestao=(), so_para: str = None) -> list:
    """Manda o relatório a cada gestor. Caminho PRÓPRIO, não passa pelo disparar.

    Aviso ao cliente e relatório interno têm riscos diferentes: mandar quadro
    errado ao Carlão é constrangedor, mandar cobrança errada à Ipiranga é outra
    coisa. Compartilhar a mesma trava faria gestor contar no teto diário do
    cliente e o `confirmo_producao` proteger duas coisas incomparáveis.

    enviar_fn=None  -> PRÉVIA: monta tudo e não envia (padrão)
    so_para='x@y'   -> manda todos para esse endereço, para conferir formato
    copia_gestao    -> endereços que recebem CÓPIA de todos os quadros
    """
    from IPython.display import HTML, display
    import template

    dados = resumo_por_gerente(c, gc, bq=bq, escrever=False)["linhas"]
    p1 = io.abrir(gc, ID_DESTINO)
    _, emails = _mapa_gestores(p1)

    criterio = criterio or (
        "frequência de meses vencidos no grupo e no próprio cadastro, "
        "com pelo menos 2 meses vencidos no ano corrente e uso de "
        "equipamento nos últimos 90 dias")

    saida = []
    for ger in sorted({l["gerente"] for l in dados}):
        linhas = [l for l in dados if l["gerente"] == ger]
        msg = template.montar_email_gestor(ger, linhas, c, criterio)
        destino = so_para or emails.get(ger)

        if not destino:
            print(f"  {ger}: SEM E-MAIL na Email_Vendedores — não enviado "
                  f"({msg['cobrados']} cliente[s] no quadro)")
            saida.append({"gerente": ger, "enviado": False, **msg})
            continue

        if enviar_fn is None:
            print(f"\n{'='*70}\nPARA: {destino}"
                  + (f"   CC: {', '.join(copia_gestao)}" if copia_gestao else "")
                  + f"\nASSUNTO: {msg['assunto']}\n{'='*70}")
            display(HTML(msg["html"]))
        else:
            enviar_fn(types.SimpleNamespace(
                codigo_cliente=ger, nome_cliente=ger,
                para=[destino], cc=list(copia_gestao),
                assunto=msg["assunto"], html=msg["html"], texto=msg["texto"]))
            print(f"  {ger} -> {destino}  ({msg['cobrados']} cliente[s]"
                  + (f", {msg['vetados']} retirado[s]" if msg["vetados"] else "")
                  + ")")
        saida.append({"gerente": ger, "destino": destino,
                      "enviado": enviar_fn is not None, **msg})

    if enviar_fn is None:
        print(f"\n  PRÉVIA — nada enviado. Para mandar, passe enviar_fn.")
    return saida


def levantamento_reanalise(c, gc, bq, escrever: bool = True) -> dict:
    """Monta a BASE para o time de dados reanalisar, não uma lista de tarefas.

    A contradição que motiva o pedido: estes clientes são cobrados por
    Licenciamento e Aluguel — cobranças que só existem se houver equipamento —
    e não têm nenhuma bomba alocada no nome deles. Medido hoje: 12 dos maiores
    somam R$ 522 mil nessa situação, e 11 deles sem nenhum cadastro irmão com
    bomba.

    Ou a bomba está atribuída a outro CNPJ, ou a cobrança é indevida. Os dois
    são problema de dado, e nenhum se resolve pela cobrança.

    Devolve, por cliente, as pistas que permitem testar as hipóteses:
      cobrado_por        natureza das faturas recentes
      irmaos_com_bomba   outro cadastro da mesma raiz de CNPJ tem parque?
      como_local         a bomba está no nome dele com outro pagante?
      mesmo_sistema      quem mais paga pelo cliente_id dele
    """
    import pandas as pd

    sql = f"""
    WITH emp AS (
      SELECT codigo, nomeCompleto,
             SUBSTR(REGEXP_REPLACE(COALESCE(cnpjCpf,''), r'\\D',''),1,8) AS raiz
      FROM `{PROJECT_ID}.bronze.cigam__empresas`
      WHERE divisao.codigoDivisao IN ('10','11','12','90') AND codigo != '000679'
    ),
    cb AS (SELECT DISTINCT cliente_cigam_pagante AS codigo
           FROM `{PROJECT_ID}.gold.bombas_alocadas`),
    base AS (
      SELECT e.codigo, e.raiz, e.nomeCompleto AS cliente,
             ROUND(SUM(t.saldo), 2) AS em_aberto,
             COUNT(DISTINCT DATE_TRUNC(t.dataVencimento, MONTH)) AS meses,
             MIN(t.dataVencimento) AS desde,
             STRING_AGG(DISTINCT cc.DESCRICAO ORDER BY cc.DESCRICAO) AS cobrado_por
      FROM `{PROJECT_ID}.silver.titulos_cigam` t
      JOIN emp e ON e.codigo = t.codigoEmpresa
      LEFT JOIN cb ON cb.codigo = e.codigo
      LEFT JOIN `{PROJECT_ID}.bronze.cigam__cadastro_conta_financeira` cc
        ON cc.codigoConta = t.codigoConta
      WHERE t.saldo > 0
        AND t.dataVencimento < CURRENT_DATE('America/Sao_Paulo')
        AND cb.codigo IS NULL
      GROUP BY 1, 2, 3
      HAVING meses >= @meses_min
    )
    SELECT
      b.codigo, SUBSTR(b.cliente, 1, 38) AS cliente, b.em_aberto, b.meses,
      b.desde, b.cobrado_por,
      (SELECT COUNT(DISTINCT e2.codigo) FROM emp e2 JOIN cb ON cb.codigo = e2.codigo
       WHERE e2.raiz = b.raiz) AS irmaos_com_bomba,
      (SELECT STRING_AGG(DISTINCT CONCAT(g.cliente_cigam_pagante, ' paga ',
              CAST(g.serial_equipamento AS STRING)) LIMIT 4)
       FROM `{PROJECT_ID}.gold.bombas_alocadas` g
       WHERE g.cliente_cigam_local = b.codigo) AS como_local,
      (SELECT STRING_AGG(DISTINCT CAST(g.cliente_id AS STRING) LIMIT 3)
       FROM `{PROJECT_ID}.gold.bombas_alocadas` g
       JOIN emp e3 ON e3.codigo = g.cliente_cigam_pagante
       WHERE e3.raiz = b.raiz) AS sistemas_do_grupo
    FROM base b
    ORDER BY b.em_aberto DESC
    """
    linhas = [dict(r) for r in bq.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter(
            "meses_min", "INT64", 3)])).result()]

    recorrente = [l for l in linhas
                  if any(p in (l["cobrado_por"] or "")
                         for p in ("Licenciamento", "Aluguel"))]
    total = sum(l["em_aberto"] for l in linhas)
    print(f"{len(linhas)} cliente(s) com dívida recorrente e SEM bomba alocada, "
          f"R$ {total:,.2f}")
    print(f"  {len(recorrente)} são cobrados por Licenciamento ou Aluguel — "
          f"cobrança que pressupõe equipamento")
    print(f"  {sum(1 for l in linhas if l['como_local'])} aparecem como LOCAL "
          f"de bomba paga por outro CNPJ")
    print(f"  {sum(1 for l in linhas if (l['irmaos_com_bomba'] or 0) > 0)} têm "
          f"cadastro irmão com parque")

    if escrever:
        io.escrever_aba(io.abrir(gc, ID_DESTINO), ABA_REANALISE,
                        pd.DataFrame(linhas))
    return {"linhas": linhas, "total": total}


def enviar_reanalise(c, gc, bq, enviar_fn=None, para=None, copia=()) -> dict:
    """Manda o levantamento ao time de dados. Sem `enviar_fn` é prévia."""
    from IPython.display import HTML, display
    import template

    lev = levantamento_reanalise(c, gc, bq, escrever=enviar_fn is not None)
    linhas = lev["linhas"]
    if not linhas:
        print("  nada a reanalisar.")
        return {}

    link = f"https://docs.google.com/spreadsheets/d/{ID_DESTINO}/edit"
    msg = template.montar_email_reanalise(linhas, c, link=link, aba=ABA_REANALISE)

    if enviar_fn is None or not para:
        print(f"\n{'='*70}\nPARA: {para or '(defina para=)'}"
              + (f"   CC: {', '.join(copia)}" if copia else "")
              + f"\nASSUNTO: {msg['assunto']}\n{'='*70}")
        display(HTML(msg["html"]))
        print("\n  PRÉVIA — nada enviado.")
        return msg

    enviar_fn(types.SimpleNamespace(
        codigo_cliente="reanalise", nome_cliente="Inteligência de Dados",
        para=[para], cc=list(copia),
        assunto=msg["assunto"], html=msg["html"], texto=msg["texto"]))
    print(f"  enviado para {para} — {msg['clientes']} cliente(s), "
          f"R$ {msg['total']:,.2f}")
    return msg


def enviar_revisao_interna(c, gc, bq=None, enviar_fn=None, para_eo=None,
                           para_cs=None, prazo: str = "", copia=()) -> list:
    """Manda as duas listas de revisão paralela: alocação e churn.

    São clientes que passaram em TODOS os critérios de dívida e ficaram de
    fora da campanha só pelo estado do equipamento. A cobrança não os alcança,
    mas cada um aponta um problema que outro time resolve:

      sem alocação   pode ser erro de cadastro — a dívida existe e o
                     equipamento também, só não estão ligados
      parque parado  pode ser churn em formação — parou de usar e parou de
                     pagar ao mesmo tempo

    Caminho próprio, como o dos gestores: nada disso passa pela trava do
    disparo ao cliente. Sem `enviar_fn` é prévia.
    """
    from IPython.display import HTML, display
    import template

    p1 = io.abrir(gc, ID_DESTINO)
    link = f"https://docs.google.com/spreadsheets/d/{ID_DESTINO}/edit"

    # PARQUE de cada cliente, inclusive quando ele aparece só como LOCAL.
    #
    # O grupo da EO é "sem bomba como pagante", e a bomba pode existir com
    # outro CNPJ pagando — é justamente o erro de alocação que eles procuram.
    # Sem mostrar o parque, a lista vira um pedido de investigação do zero; com
    # ele, a EO vê na hora se o equipamento existe e sob qual vínculo.
    #
    # Isso já pegou um caso: Trans ABC 001709 e 002944 estavam na lista de
    # "sem alocação" com bomba alocada e abastecendo no dia anterior.
    parque = {}
    if bq is not None:
        cods = [_codigo(l.get("codigo") or l.get("codigo_cliente"))
                for aba in (ABA_TRIAGEM_REGRA, ABA_CHURN)
                for l in io.ler_aba(p1, aba, obrigatoria=False)]
        cods = sorted({x for x in cods if x})
        if cods:
            for r in bq.query(f"""
                SELECT
                  b.cliente_cigam_pagante AS pagante,
                  b.cliente_cigam_local   AS local,
                  CAST(b.serial_equipamento AS STRING) AS serial,
                  ANY_VALUE(b.bomba_nome) AS nome,
                  ANY_VALUE(CAST(b.cliente_id AS STRING)) AS cliente_id,
                  MAX(u.ultimo) AS ultimo_uso
                FROM `{PROJECT_ID}.gold.bombas_alocadas` b
                LEFT JOIN (
                  SELECT CAST(bomba_id AS STRING) AS bomba_id, MAX(data) AS ultimo
                  FROM `{PROJECT_ID}.silver.abastecimentos_validos`
                  WHERE data >= DATE_SUB(CURRENT_DATE('America/Sao_Paulo'),
                                         INTERVAL 400 DAY)
                  GROUP BY 1) u
                  ON u.bomba_id = CAST(b.bomba_id AS STRING)
                WHERE b.cliente_cigam_pagante IN UNNEST(@c)
                   OR b.cliente_cigam_local IN UNNEST(@c)
                GROUP BY 1, 2, 3
            """, job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ArrayQueryParameter("c", "STRING", cods)])).result():
                for cod, papel in ((r.pagante, "paga"), (r.local, "local")):
                    if cod in cods:
                        parque.setdefault(cod, []).append(
                            {"serial": r.serial, "nome": r.nome,
                             "cliente_id": r.cliente_id, "papel": papel,
                             "ultimo_uso": r.ultimo_uso})

    blocos = [("alocacao", ABA_TRIAGEM_REGRA, para_eo,
               "Excelência Operacional"),
              ("churn", ABA_CHURN, para_cs, "CS")]

    saida = []
    for tipo, aba, destino, time in blocos:
        linhas = [l for l in io.ler_aba(p1, aba, obrigatoria=False)
                  if str(l.get("codigo") or l.get("codigo_cliente") or "").strip()]
        if not linhas:
            print(f"  {time}: '{aba}' vazia — nada a revisar.")
            continue

        # Já conferido não volta para a fila: o time preenche a coluna e some
        # da próxima leva, senão o e-mail repete sempre os mesmos nomes e
        # deixa de ser lido.
        col = "cliente_id_validado" if tipo == "alocacao" else "avaliacao_cs"
        pendentes = [l for l in linhas if not str(l.get(col) or "").strip()]
        ja = len(linhas) - len(pendentes)
        if not pendentes:
            print(f"  {time}: todos os {len(linhas)} já conferidos.")
            continue

        for l in pendentes:
            bombas = parque.get(_codigo(l.get("codigo")
                                        or l.get("codigo_cliente")), [])
            l["parque"] = "; ".join(
                f"{b['nome'] or b['serial']} ({b['serial']}"
                + (f", {b['papel']}" if b["papel"] == "local" else "")
                + (f", usou {b['ultimo_uso']:%d/%m}" if b["ultimo_uso"] else
                   ", sem uso")
                + ")"
                for b in sorted(bombas, key=lambda x: str(x["nome"] or "")))
            l["tem_bomba"] = "sim" if bombas else "não"

        msg = template.montar_email_interno(tipo, pendentes, c,
                                            link=f"{link}", prazo=prazo)
        if not destino:
            print(f"  {time}: SEM destinatário — passe para_eo= / para_cs= "
                  f"({len(pendentes)} cliente[s] pendente[s])")
            saida.append({"time": time, "enviado": False, **msg})
            continue

        if enviar_fn is None:
            print(f"\n{'='*70}\nPARA: {destino}"
                  + (f"   CC: {', '.join(copia)}" if copia else "")
                  + f"\nASSUNTO: {msg['assunto']}"
                  + (f"\n({ja} já conferido[s], fora desta lista)" if ja else "")
                  + f"\n{'='*70}")
            display(HTML(msg["html"]))
        else:
            enviar_fn(types.SimpleNamespace(
                codigo_cliente=tipo, nome_cliente=time,
                para=[destino], cc=list(copia),
                assunto=msg["assunto"], html=msg["html"], texto=msg["texto"]))
            print(f"  {time} -> {destino}  ({len(pendentes)} pendente(s), "
                  f"R$ {msg['total']:,.2f})")
        saida.append({"time": time, "destino": destino,
                      "enviado": enviar_fn is not None, **msg})

    if enviar_fn is None and saida:
        print(f"\n  PRÉVIA — nada enviado. Para mandar, passe enviar_fn.")
    return saida


def resumo_campanhas(gc):
    """Uma linha por campanha, com as fases pivotadas em colunas.

    A aba Campanhas é append-only e tem uma linha por FASE — boa para histórico,
    ruim para comparar campanhas: `valor_cobrado` está na linha do preparado e
    `valor_recuperado` na do reconciliado, e cruzar isso em fórmula do Sheets
    fica ilegível.

    Esta aba é achatada de propósito: cada linha é uma campanha inteira, com os
    índices já calculados. É a fonte para gráfico e para o painel.

    Recalculada do zero a cada chamada — não é histórico, é uma projeção do
    histórico. O que não pode ser perdido está na aba Campanhas.
    """
    import pandas as pd

    p1 = io.abrir(gc, ID_DESTINO)
    registro = io.ler_aba(p1, ABA_CAMPANHAS, obrigatoria=False)
    if not registro:
        print(f"'{ABA_CAMPANHAS}' vazia; nada a resumir."); return []

    def _n(v):
        t = str(v or "").strip().replace("R$", "").strip()
        t = t.replace(".", "").replace(",", ".") if "," in t else t
        try:
            return float(t or 0)
        except ValueError:
            return 0.0

    # Última linha de cada (campanha, fase) vence: reexecução de uma fase
    # substitui a anterior em vez de duplicar.
    por_campanha = {}
    for l in registro:
        cid = str(l.get("id_campanha") or "").strip()
        fase = str(l.get("fase") or "").strip().split(" ")[0]
        if not cid or not fase:
            continue
        por_campanha.setdefault(cid, {})[fase] = l

    linhas = []
    for cid in sorted(por_campanha, reverse=True):
        f = por_campanha[cid]
        regua = f.get("regua", {})
        prep  = f.get("preparado", {})
        disp  = f.get("disparado", {})
        rec   = f.get("reconciliado", {})

        cobrado    = _n(prep.get("valor_cobrado"))
        recuperado = _n(rec.get("valor_recuperado"))
        clientes   = _n(prep.get("clientes"))
        quitou     = _n(rec.get("quitou"))
        parcial    = _n(rec.get("pagou_parcial"))
        tit_aviso  = _n(rec.get("titulos_no_aviso")) or _n(prep.get("titulos"))
        tit_recup  = (_n(rec.get("titulos_quitados"))
                      + _n(rec.get("titulos_reduzidos")))
        reneg      = _n(rec.get("renegociaram"))

        def pct(a, b):
            return round(a / b * 100, 1) if b else ""

        linhas.append({
            "id_campanha": cid,
            "data_envio": prep.get("data_envio") or disp.get("data_envio") or "",
            "data_limite": prep.get("data_limite") or disp.get("data_limite") or "",
            "reconciliada": "sim" if rec else "não",
            # funil
            "com_divida": _n(regua.get("com_divida")) or "",
            "sem_alocacao": _n(regua.get("sem_alocacao")) or "",
            "entraram": _n(regua.get("entram")) or "",
            # execução
            "clientes": clientes,
            "titulos": tit_aviso,
            "valor_cobrado": cobrado,
            "enviados": _n(disp.get("enviados")),
            "falhas": _n(disp.get("falhas")),
            # resultado
            "quitaram": quitou,
            "pagaram_parcial": parcial,
            "renegociaram": reneg,
            "titulos_recuperados": tit_recup,
            "valor_recuperado": recuperado,
            "valor_pago_titulos": _n(rec.get("valor_pago_titulos")),
            "virou_x90": _n(rec.get("valor_virou_x90")),
            # índices
            "pct_valor_recuperado": pct(recuperado, cobrado),
            "pct_clientes_quitaram": pct(quitou, clientes),
            "pct_titulos_recuperados": pct(tit_recup, tit_aviso),
            "pct_renegociacao": pct(reneg, clientes),
            "pct_alguma_reacao": pct(quitou + parcial + reneg, clientes),
        })

    df = pd.DataFrame(linhas)
    io.escrever_aba(p1, ABA_RESUMO, df)

    fechadas = [l for l in linhas if l["reconciliada"] == "sim"]
    print(f"'{ABA_RESUMO}': {len(linhas)} campanha(s), {len(fechadas)} reconciliada(s).")
    if fechadas:
        cob = sum(l["valor_cobrado"] for l in fechadas)
        rec_ = sum(l["valor_recuperado"] for l in fechadas)
        print(f"  acumulado: R$ {rec_:,.2f} recuperados de R$ {cob:,.2f} "
              f"cobrados ({rec_ / cob * 100:.1f}%)" if cob else "")
    pendentes = [l["id_campanha"] for l in linhas if l["reconciliada"] == "não"]
    if pendentes:
        print(f"  sem reconciliação: {', '.join(pendentes)} — "
              f"os índices ficam vazios até rodar reconciliar()")
    return linhas


def migrar_datas_previa(gc, dry_run: bool = True):
    """Acrescenta data_envio/data_limite às campanhas JÁ congeladas.

    Rodar preparar() de novo não serve: ele recalcularia os encargos com a data
    de hoje, mudando valores que o cliente já recebeu por e-mail. O snapshot
    deixaria de descrever o que foi cobrado.

    Esta função não toca em nenhum valor — só acrescenta duas colunas, lendo
    as datas do registro. Idempotente: campanha que já tem as colunas é pulada.

    dry_run=True (padrão) mostra o que faria sem escrever. É snapshot de
    campanha enviada; vale olhar antes.
    """
    import pandas as pd

    p1 = io.abrir(gc, ID_DESTINO)
    linhas = io.ler_aba(p1, ABA_PREVIA, obrigatoria=False)
    if not linhas:
        print(f"'{ABA_PREVIA}' vazia; nada a migrar."); return

    datas = {}
    for l in io.ler_aba(p1, ABA_CAMPANHAS, obrigatoria=False):
        cid = str(l.get("id_campanha") or "").strip()
        env, lim = str(l.get("data_envio") or "").strip(), str(l.get("data_limite") or "").strip()
        if cid and env and lim:
            datas[cid] = (env[:10], lim[:10])

    # Trabalha numa CÓPIA. A primeira versão mutava as linhas lidas antes de
    # decidir não escrever, então rodar em dry_run e rodar de novo dava
    # resultados diferentes — um dry_run que muda estado não é dry_run.
    novas = [dict(l) for l in linhas]

    faltam, preenchidas, sem_data = 0, 0, set()
    for l in novas:
        cid = str(l.get("id_campanha") or "").strip()
        if str(l.get("data_envio") or "").strip():
            preenchidas += 1
            continue
        if cid in datas:
            l["data_envio"], l["data_limite"] = datas[cid]
            faltam += 1
        else:
            # String vazia, não None: None vira "None" no Sheets.
            l.setdefault("data_envio", "")
            l.setdefault("data_limite", "")
            sem_data.add(cid)

    print(f"{len(linhas)} linha(s) em '{ABA_PREVIA}': "
          f"{preenchidas} já tinham data, {faltam} preenchida(s).")
    if sem_data:
        print(f"  SEM data no registro, ficam como estão: "
              f"{', '.join(sorted(sem_data))}")
        print(f"  (para essas, registre a campanha antes com backfill_registro)")

    if not faltam:
        print("Nada a escrever."); return

    if dry_run:
        print("\ndry_run=True: NÃO escrevi. Amostra do que ficaria:")
        for l in novas[:3]:
            print(f"   {l.get('id_campanha')} {l.get('codigo_cliente')} "
                  f"envio={l.get('data_envio')} limite={l.get('data_limite')}")
        print("\nPara aplicar: migrar_datas_previa(gc, dry_run=False)")
        return

    # Reescreve a aba INTEIRA, todas as campanhas. escrever_aba limpa antes,
    # então o DataFrame precisa conter tudo que estava lá — por isso partimos
    # das linhas lidas e só acrescentamos colunas.
    df = pd.DataFrame(novas)
    io.escrever_aba(p1, ABA_PREVIA, df)
    print(f"'{ABA_PREVIA}' regravada: {len(df)} linha(s), "
          f"{len(df.columns)} coluna(s).")


def abrir_campanha(gc, id_campanha: str = None,
                   copia_padrao=("contato.financeiro@ctasmart.com.br",),
                   teto_diario: int = 450) -> "campanha_v2.Campanha":
    """REABRE uma campanha existente em vez de criar outra.

    nova_campanha() sempre gera id novo, então uma sessão nova do Colab perdia
    o vínculo com o que já foi congelado e enviado. Se o runtime cai no meio do
    disparo, a campanha continua na planilha e o notebook não a alcança.

    Sem id, reabre a mais recente com prévia congelada.

    As datas NUNCA vêm de hoje: data_envio é a âncora dos encargos já
    calculados, e recalcular com a data atual mudaria valores que o cliente já
    recebeu por e-mail.
    """
    from datetime import date as _date

    p1 = io.abrir(gc, ID_DESTINO)
    previa = io.ler_aba(p1, ABA_PREVIA, obrigatoria=False)

    congeladas = {str(l.get("id_campanha") or "").strip() for l in previa}
    congeladas.discard("")
    if not congeladas:
        raise RuntimeError(f"Nenhuma campanha congelada em '{ABA_PREVIA}'. "
                           f"Use nova_campanha() + preparar().")

    if id_campanha is None:
        id_campanha = sorted(congeladas)[-1]
        if len(congeladas) > 1:
            print(f"  congeladas: {', '.join(sorted(congeladas))}")
    elif id_campanha not in congeladas:
        raise RuntimeError(f"'{id_campanha}' não tem prévia congelada. "
                           f"Disponíveis: {', '.join(sorted(congeladas))}")

    # DUAS FONTES para as datas: a própria prévia primeiro (autossuficiente
    # desde que o preparar grava as colunas), o registro depois. Antes só o
    # registro servia, e apagá-lo trancava a campanha.
    ultimo = next((l for l in reversed(previa)
                   if str(l.get("id_campanha") or "").strip() == id_campanha
                   and str(l.get("data_envio") or "").strip()), None)
    origem = ABA_PREVIA
    if ultimo is None:
        reg = [l for l in io.ler_aba(p1, ABA_CAMPANHAS, obrigatoria=False)
               if str(l.get("id_campanha") or "").strip() == id_campanha
               and str(l.get("data_envio") or "").strip()]
        ultimo, origem = (reg[-1], ABA_CAMPANHAS) if reg else (None, None)
    if ultimo is None:
        raise RuntimeError(
            f"'{id_campanha}' está congelada mas não tem datas nem em "
            f"'{ABA_PREVIA}' nem em '{ABA_CAMPANHAS}'. Passe à mão:\n"
            f"  c = campanha_v2.Campanha(id_campanha='{id_campanha}', "
            f"data_envio=date(A,M,D), data_limite=date(A,M,D))\n"
            f"data_envio tem de ser a do disparo original — é a âncora dos "
            f"encargos que o cliente já recebeu.")

    def _d(txt):
        a, m, d = str(txt).strip()[:10].split("-")
        return _date(int(a), int(m), int(d))

    c = campanha_v2.Campanha(
        id_campanha=id_campanha,
        data_envio=_d(ultimo["data_envio"]),
        data_limite=_d(ultimo["data_limite"]),
        copia_padrao=tuple(copia_padrao), teto_diario=teto_diario,
    )
    n = sum(1 for l in previa
            if str(l.get("id_campanha") or "").strip() == id_campanha)
    print(f"{c.id_campanha} reaberta | envio {c.data_envio:%d/%m/%Y} | "
          f"limite {c.data_limite:%d/%m/%Y} | {n} cliente(s) | "
          f"datas de '{origem}'")
    return c


def backfill_registro(gc, c):
    """Registra uma campanha já congelada, sem recongelar.

    Rodar preparar() de novo recalcularia os encargos com a data de hoje,
    mudando valores que o cliente já recebeu por e-mail. O backfill lê o que
    está congelado e só registra.
    """
    p1 = io.abrir(gc, ID_DESTINO)

    def _linhas(aba):
        return [l for l in io.ler_aba(p1, aba, obrigatoria=False)
                if str(l.get("id_campanha") or "").strip() == c.id_campanha]

    previa = _linhas(ABA_PREVIA)
    if not previa:
        raise RuntimeError(f"'{c.id_campanha}' não tem prévia congelada.")

    ja = [l for l in io.ler_aba(p1, ABA_CAMPANHAS, obrigatoria=False)
          if str(l.get("id_campanha") or "").strip() == c.id_campanha]
    if ja:
        print(f"'{c.id_campanha}' já tem {len(ja)} linha(s) no registro.")
        return

    def _n(v):
        t = str(v or 0).strip().replace("R$", "").strip()
        t = t.replace(".", "").replace(",", ".") if "," in t else t
        try:
            return float(t or 0)
        except ValueError:
            return 0.0

    # Conta SÓ produção: filtrar apenas por status contava os e-mails de teste,
    # que foram para a caixa do operador.
    log = _linhas(ABA_LOG)
    enviados = [l for l in log
                if str(l.get("status") or "").upper() == "ENVIADO"
                and str(l.get("modo") or "PRODUCAO").upper() == "PRODUCAO"]

    registrar(gc, c, "preparado",
              clientes=len(previa), titulos=len(_linhas(ABA_TITULOS)),
              bombas=len(_linhas(ABA_BOMBAS)),
              valor_cobrado=round(sum(_n(l.get("total")) for l in previa), 2))
    if enviados:
        registrar(gc, c, "disparado", enviados=len(enviados), falhas=0)
    print(f"'{c.id_campanha}': {len(previa)} cliente(s), "
          f"{len(enviados)} enviado(s) em produção.")


def registrar(gc, c, fase: str, **metricas):
    """Uma linha por campanha no registro. É o controle que faltava: o
    Campanha_Log é por mensagem e não responde 'quantas campanhas já rodaram,
    com que prazo, quanto valor'."""
    from datetime import datetime
    p1 = io.abrir(gc, ID_DESTINO)
    # `modo` só descreve ENVIO. Na fase "preparado" nada é enviado, e gravar
    # DRY_RUN ali sugeria que a campanha inteira foi um ensaio — foi o que
    # apareceu no registro da 2026-09-B, que teve disparo real de produção.
    modo = c.modo if fase.startswith("disparado") else ""
    io.append_log(p1, ABA_CAMPANHAS, {
        "id_campanha": c.id_campanha,
        "fase": fase,
        "modo": modo,
        "data_envio": f"{c.data_envio:%Y-%m-%d}",
        "data_limite": f"{c.data_limite:%Y-%m-%d}",
        "dias_prazo": c.dias_de_prazo,
        "registrado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        **metricas,
    })






def candidatos(gc, bq, freq_minima: int = 7, freq_propria_minima: int = 3,
               meses_ano_corrente_min: int = 2, dias_uso: int = 90,
               escrever: bool = True, campanha=None) -> list:
    """Aplica a REGRA DE BLOQUEIO e escreve a Campanha_Input.

        deve a X frequência  E  tem uso recente  ->  entra na régua

    O script propõe, você veta. A alternativa — você monta a lista e o script
    obedece — coloca em você o trabalho que o warehouse faz melhor (frequência,
    saldo, último uso, bombas, destinatários) e deixa de fora só o que ele não
    sabe: renegociação em curso, jurídico, conta que o comercial pediu para
    segurar. É esse resto que vai na coluna `acao`.

    EM BRANCO ENTRA. Se fosse o contrário, uma distração deixaria cliente de
    fora em silêncio; assim, a distração faz entrar alguém que você teria
    tirado — e o teste redirecionado pega isso antes de sair.

    freq_minima: meses distintos com título vencido no GRUPO (raiz de CNPJ),
                 incluindo X90 e contando SÓ cadastros com bomba. Conferido
                 contra os números do Paul em 6 de 6 clientes.
    freq_propria_minima: o grupo dá o sinal, mas o cadastro precisa merecer.
                 Sem isto, quem deve um mês entra porque um irmão deve nove.
    dias_uso:    janela do último abastecimento. Cliente sem alocação sai com
                 uso desconhecido e NÃO entra automaticamente.
    """
    p1 = io.abrir(gc, ID_DESTINO)

    # Política permanente, não decisão de campanha. Conta grande, contrato
    # especial ou cliente sob acordo guarda-chuva sai TODA rodada — vetar os
    # mesmos na coluna `acao` a cada campanha é trabalho repetido que uma hora
    # alguém esquece, e o esquecimento manda aviso de bloqueio para a Ipiranga.
    #
    # Aba Nunca_Notificar: chave | motivo
    #   000337           código CIGAM   -> um cadastro
    #   17185786         raiz de CNPJ   -> o grupo todo
    #   17185786000161   CNPJ completo  -> idem, usa a raiz
    #   ID(974321)       cliente_id     -> todos os pagantes do sistema
    #
    # O PREFIXO ID() É OBRIGATÓRIO e não é firula. Deduzir o tipo pelo número
    # de dígitos não funciona: existe cliente_id 974321 (6 dígitos, igual a
    # código CIGAM) e cliente_id 69288523 (8, igual a raiz de CNPJ). Sem o
    # marcador, os dois seriam lidos como outra coisa e a exclusão falharia
    # em silêncio — que é o pior modo de falhar numa lista de proteção.
    #
    # O cliente_id existe porque um sistema pode ter bombas pagas por CNPJs
    # sem relação entre si: o 53412440 tem 35 pagantes. Excluir por CNPJ não
    # protege o sistema, e proteger o sistema não se expressa por CNPJ.
    excecoes = io.ler_aba(p1, ABA_EXCECOES, obrigatoria=False)
    bloq_cadastro, bloq_raiz, bloq_sistema, motivo_de = set(), set(), set(), {}
    ignoradas = []
    for l in excecoes:
        bruto = str(l.get("chave") or l.get("codigo") or "").strip()
        if not bruto:
            continue
        motivo = str(l.get("motivo") or "").strip() or "sem motivo informado"

        m = re.fullmatch(r"(?i)\s*id\s*\(?\s*(\d+)\s*\)?\s*", bruto)
        if m:
            k = m.group(1)
            bloq_sistema.add(k)
        else:
            digitos = re.sub(r"\D", "", bruto)
            if not digitos:
                ignoradas.append((bruto, "sem dígitos"))
                continue
            if len(digitos) <= 6:
                k = digitos.zfill(6); bloq_cadastro.add(k)
            elif len(digitos) in (7, 8, 10, 11, 13, 14):
                # 7, 10 e 13 dígitos = o Sheets comeu o zero à esquerda. Raiz
                # 07636657 (Aço Verde) entra como 7636657 e a exclusão falhava
                # em silêncio: dois cadastros dela receberam e-mail de teste.
                # Zero-padding para o comprimento canônico mais próximo.
                canonico = {7: 8, 10: 11, 13: 14}.get(len(digitos), len(digitos))
                if canonico != len(digitos):
                    print(f"     {bruto!r}: {len(digitos)} dígitos — zero à "
                          f"esquerda comido pelo Sheets, lendo como "
                          f"{digitos.zfill(canonico)[:8]}")
                k = digitos.zfill(canonico)[:8]; bloq_raiz.add(k)
            else:
                # Não inventa interpretação: 7, 9 ou 12 dígitos sem prefixo não
                # é código, nem raiz, nem sistema declarado. Avisa e ignora.
                ignoradas.append(
                    (bruto, f"{len(digitos)} dígitos — use até 6 (código), "
                            f"8/11/14 (CNPJ) ou ID(...) para cliente_id"))
                continue
        motivo_de[k] = motivo

    if ignoradas:
        print(f"  {len(ignoradas)} chave(s) IGNORADA(S) em '{ABA_EXCECOES}' "
              f"— NÃO excluíram ninguém:")
        for bruto, por_que in ignoradas:
            print(f"     {bruto!r}: {por_que}")

    # cliente_id -> cadastros que pagam por bomba daquele sistema
    cadastros_do_sistema = {}
    if bloq_sistema:
        for r in bq.query(f"""
            SELECT DISTINCT CAST(cliente_id AS STRING) AS cliente_id,
                   cliente_cigam_pagante AS codigo
            FROM `{PROJECT_ID}.gold.bombas_alocadas`
            WHERE CAST(cliente_id AS STRING) IN UNNEST(@ids)
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("ids", "STRING", sorted(bloq_sistema))
             ])).result():
            cadastros_do_sistema.setdefault(r.cliente_id, set()).add(r.codigo)
        for sid, cods in cadastros_do_sistema.items():
            print(f"  sistema {sid}: {len(cods)} cadastro(s) pagante(s) excluído(s)")
        faltando = bloq_sistema - set(cadastros_do_sistema)
        if faltando:
            print(f"  AVISO: cliente_id sem bomba alocada, não excluiu nada: "
                  f"{', '.join(sorted(faltando))}")

    por_sistema = {c for cods in cadastros_do_sistema.values() for c in cods}
    bloqueados = bloq_cadastro | bloq_raiz

    sql = (SQL / "base_inadimplencia.sql").read_text()
    sql = sql.replace("DECLARE dias_uso_recente INT64 DEFAULT 90;",
                      f"DECLARE dias_uso_recente INT64 DEFAULT {int(dias_uso)};")
    df = bq.query(sql).to_dataframe(create_bqstorage_client=False)
    n_com_divida = len(df)
    n_fora_juridico = 0      # a query já exclui X91/X92; medido abaixo se houver
    n_fora_excecoes = 0
    n_fora_acordo = 0

    if bloqueados or por_sistema:
        na_lista = (df.codigo.isin(bloq_cadastro)
                    | df.cnpj_raiz.isin(bloq_raiz)
                    | df.codigo.isin(por_sistema))
        vetados = df[na_lista]
        df = df[~na_lista]
        # Chave que não excluiu NINGUÉM quase sempre é erro de digitação, e
        # falha em silêncio: você acha que protegeu a conta e não protegeu.
        # Foi assim que a Aço Verde recebeu e-mail de teste — a raiz 07636657
        # entrou como 7636657 e não casou com nada.
        sem_efeito = []
        for k, motivo in sorted(motivo_de.items()):
            se_pegou = (
                (k in bloq_cadastro and (df.codigo == k).any())
                or (k in bloq_raiz and (df.cnpj_raiz == k).any())
                or (k in bloq_sistema and bool(cadastros_do_sistema.get(k)))
                or (k in bloq_cadastro and (vetados.codigo == k).any())
                or (k in bloq_raiz and (vetados.cnpj_raiz == k).any())
            )
            if not se_pegou:
                sem_efeito.append((k, motivo))
        if sem_efeito:
            print(f"  {len(sem_efeito)} chave(s) de '{ABA_EXCECOES}' NÃO "
                  f"excluíram ninguém — confira se estão certas:")
            for k, motivo in sem_efeito:
                print(f"     {k}  ({motivo})")

        if len(vetados):
            print(f"{len(vetados)} cadastro(s) fora por '{ABA_EXCECOES}' "
                  f"(R$ {vetados.em_atraso.astype(float).sum():,.2f}):")
            for r in vetados.itertuples():
                if r.codigo in motivo_de:
                    chave, via = r.codigo, "código"
                elif r.cnpj_raiz in motivo_de:
                    chave, via = r.cnpj_raiz, "raiz"
                else:
                    chave = next((s for s, cods in cadastros_do_sistema.items()
                                  if r.codigo in cods), None)
                    via = f"sistema {chave}"
                print(f"   {r.codigo} {str(r.cliente)[:30]:<30} "
                      f"[{via}] {motivo_de.get(chave, '')}")

    # Pelo menos N meses vencidos DESTE ANO. Quem não atinge sai da régua e vai
    # para a Triagem_Divida_Antiga: a dívida existe, mas bloquear equipamento
    # por resíduo de anos anteriores é outra conversa, e é a que o comercial
    # contesta com mais razão.
    do_ano = df.meses_ano_corrente.fillna(0).astype(int) >= meses_ano_corrente_min

    # O critério de GRUPO só se aplica a quem TEM grupo.
    #
    # `freq_minima` mede o grupo; `freq_propria_minima` mede o cadastro. Para
    # cadastro único os dois olham o mesmo número, e o de grupo, sendo maior,
    # engolia o outro: na prática quem não tinha grupo precisava de 4 meses e
    # quem tinha precisava de 3. Leonardo Leal Lopes, R$ 40.415 vencidos, 3
    # meses todos de 2026, abastecendo hoje, ficava de fora por isso.
    #
    # Medido: 7 clientes e R$ 62.088,50 barrados só por essa assimetria.
    tem_grupo = df.cadastros_com_bomba.fillna(1).astype(int) > 1
    passou_grupo = (~tem_grupo) | (df.frequencia >= freq_minima)

    qualificado = (passou_grupo
                   & (df.frequencia_propria >= freq_propria_minima)
                   & do_ano)
    so_por_cadastro_unico = df[(~tem_grupo) & (df.frequencia < freq_minima)
                               & (df.frequencia_propria >= freq_propria_minima)
                               & do_ano]
    so_divida_antiga = df[passou_grupo
                          & (df.frequencia_propria >= freq_propria_minima)
                          & ~do_ano]
    # Acordos: quem está pagando sai; quem QUEBROU fica e é marcado.
    # Sem este cruzamento, o disparo manda aviso de bloqueio para cliente que
    # está honrando um acordo — o pior erro possível numa régua de cobrança.
    import acordos as ac
    try:
        reg = ac.carregar(gc, io)      # só a LEITURA entra no try
    except Exception as exc:
        raise RuntimeError(
            f"Não consegui ler a aba ACORDOS ({type(exc).__name__}: {exc}).\n"
            f"Isto NÃO é opcional: sem o cruzamento, o disparo notifica cliente "
            f"que está pagando acordo. Compartilhe a planilha com a conta que "
            f"você usa no Colab."
        ) from exc

    print(reg.resumo())
    if not reg.por_cliente:
        raise RuntimeError(
            "A aba ACORDOS foi lida mas devolveu ZERO clientes. Isso quase "
            "sempre é cabeçalho ou nome de coluna diferente do esperado — não "
            "é 'ninguém tem acordo'. Confira antes de seguir."
        )

    docs = {r.codigo: str(getattr(r, "faturas_vencidas", "") or "").split(";")
            for r in df.itertuples()}
    df, fora_acordo = ac.aplicar(df, reg, docs_por_cliente=docs)
    if len(fora_acordo):
        elegivel = fora_acordo[(fora_acordo.frequencia >= freq_minima)
                               & (fora_acordo.frequencia_propria >= freq_propria_minima)]
        print(f"  {len(elegivel)} candidato(s) fora por acordo ou estado indefinido "
              f"(R$ {elegivel.em_atraso.astype(float).sum():,.2f}):")
        for r in elegivel.itertuples():
            print(f"     {r.codigo} {str(r.cliente)[:30]:<30} {r.status_acordo}")

    # TRÊS destinos, não dois. Todos passaram nos critérios de dívida; o que
    # muda é o estado do equipamento:
    #
    #   usa_equipamento True   bomba alocada e abastecendo  -> CAMPANHA
    #   usa_equipamento NaN    sem alocação                 -> Excelência Op.
    #   usa_equipamento False  alocada, sem uso na janela   -> CS (churn)
    #
    # O terceiro grupo caía fora sem ir para aba nenhuma. São clientes com
    # dívida recente cujo parque está parado — exatamente o perfil de churn, e
    # o sinal mais barato que existe para o CS agir antes do cancelamento.
    apto = qualificado.reindex(df.index, fill_value=False)
    dentro = df[apto & (df.usa_equipamento == True)]
    sem_alocacao = df[apto & (df.usa_equipamento.isna())]
    sem_uso_recente = df[apto & (df.usa_equipamento == False)]
    sem_uso = sem_alocacao                      # nome antigo, ainda usado abaixo
    so_pelo_grupo = df[passou_grupo
                       & (df.frequencia_propria < freq_propria_minima)]

    print(f"{len(df)} cliente(s) com dívida | grupo >= {freq_minima}: "
          f"{(df.frequencia >= freq_minima).sum()} | e própria >= "
          f"{freq_propria_minima}: {qualificado.sum()}")
    if len(so_pelo_grupo):
        print(f"  {len(so_pelo_grupo)} fora por frequência própria baixa "
              f"(entrariam só pelo grupo)")
    if len(so_por_cadastro_unico):
        print(f"  {len(so_por_cadastro_unico)} entram por CADASTRO ÚNICO "
              f"(sem grupo, o critério de grupo não se aplica): "
              f"R$ {so_por_cadastro_unico.em_atraso.astype(float).sum():,.2f}")
    if len(so_divida_antiga):
        print(f"  {len(so_divida_antiga)} fora por ter menos de "
              f"{meses_ano_corrente_min} mês(es) vencido(s) em "
              f"{__import__('datetime').date.today():%Y} "
              f"(R$ {so_divida_antiga.em_atraso.astype(float).sum():,.2f}) "
              f"-> '{ABA_TRIAGEM_ANTIGA}'")
    print(f"  entram na régua (uso em {dias_uso}d): {len(dentro)} · "
          f"R$ {dentro.em_atraso.sum():,.2f}")
    if len(sem_uso):
        print(f"  {len(sem_uso)} com frequência mas SEM ALOCAÇÃO — uso "
              f"desconhecido, ficam fora e vão para Triagem "
              f"(R$ {sem_uso.em_atraso.sum():,.2f})")

    quebrados = dentro[dentro.get("status_acordo", "") == "QUEBRADO"] \
                if "status_acordo" in dentro else dentro.iloc[0:0]
    if len(quebrados):
        print(f"  {len(quebrados)} com ACORDO QUEBRADO — já tiveram uma chance:")
        for r in quebrados.itertuples():
            print(f"     {r.codigo} {str(r.cliente)[:30]}")

    # Grava o FUNIL. Sem isto os números da régua morrem com a sessão do
    # Colab: quantos foram avaliados, quantos passaram em cada condição,
    # quantos saíram por acordo ou por exclusão permanente. É o que permite
    # comparar campanhas e responder "por que a de outubro pegou menos gente".
    if campanha is not None and escrever:
        registrar(
            gc, campanha, "regua",
            freq_minima=freq_minima, freq_propria_minima=freq_propria_minima,
            dias_uso=dias_uso,
            com_divida=n_com_divida,
            fora_juridico=n_fora_juridico,
            fora_excecoes=n_fora_excecoes,
            fora_acordo=n_fora_acordo,
            meses_ano_corrente_min=meses_ano_corrente_min,
            passou_frequencia=int(qualificado.sum()),
            fora_divida_antiga=len(so_divida_antiga),
            fora_freq_propria=len(so_pelo_grupo),
            sem_alocacao=len(sem_alocacao),
            sem_uso_recente=len(sem_uso_recente),
            entram=len(dentro),
            valor_entram=round(float(dentro.em_atraso.astype(float).sum()), 2),
            valor_sem_alocacao=round(float(sem_uso.em_atraso.astype(float).sum()), 2)
                               if len(sem_uso) else 0.0,
        )

    if escrever and len(dentro):
        import pandas as pd
        saida = dentro.copy()
        saida.insert(0, "acao", "")          # em branco = entra
        io.escrever_aba(p1, ABA_INPUT, saida)
        # Abas de REVISÃO PARALELA. Cada uma tem uma coluna para o time
        # preencher — é por ela que a informação volta para cá.
        import pandas as _pd
        if len(sem_alocacao):
            a = sem_alocacao.copy()
            a.insert(0, "cliente_id_validado", "")   # Excelência Operacional
            a.insert(1, "conferido_por", "")
            io.escrever_aba(p1, ABA_TRIAGEM_REGRA, a)
        else:
            io.escrever_aba(p1, ABA_TRIAGEM_REGRA,
                            _pd.DataFrame(columns=["cliente_id_validado"]))

        if len(sem_uso_recente):
            b = sem_uso_recente.copy()
            b.insert(0, "avaliacao_cs", "")          # ativo / churn / em contato
            b.insert(1, "conferido_por", "")
            io.escrever_aba(p1, ABA_CHURN, b)
            print(f"  {len(sem_uso_recente)} com bomba alocada e SEM USO em "
                  f"{dias_uso}d (R$ "
                  f"{sem_uso_recente.em_atraso.astype(float).sum():,.2f}) "
                  f"-> '{ABA_CHURN}', avaliação do CS")
        io.escrever_aba(p1, ABA_TRIAGEM_ANTIGA, so_divida_antiga)
        print(f"\n'{ABA_INPUT}': {len(saida)} candidato(s). Preencha `acao` "
              f"só para EXCLUIR alguém, com o motivo.")

    return dentro.to_dict("records")


def preparar(c, gc, bq) -> dict:
    """Lê a lista, resolve, calcula e CONGELA. Não envia nada."""
    p1 = io.abrir(gc, ID_DESTINO)

    todas = io.ler_aba(p1, ABA_INPUT)
    if not todas:
        raise RuntimeError(f"'{ABA_INPUT}' vazia. Rode candidatos() para a régua "
                           f"calcular, ou ingerir_lista() para uma lista recebida.")

    # Duas origens possíveis para a aba, e cada uma marca de um jeito:
    #   candidatos()    -> coluna `acao`, EM BRANCO entra
    #   ingerir_lista() -> coluna `status`, só PRONTO entra
    # Sem nenhuma das duas, a aba foi montada à mão e tudo entra.
    cols = set(todas[0])
    if "acao" in cols:
        elegiveis = [l for l in todas if not str(l.get("acao", "")).strip()]
        vetados = [l for l in todas if str(l.get("acao", "")).strip()]
        print(f"{len(todas)} candidato(s); {len(elegiveis)} entram, "
              f"{len(vetados)} vetado(s) por você.")
        for l in vetados[:10]:
            print(f"   {l.get('codigo')}: {l.get('acao')}")
    elif "status" in cols:
        elegiveis = [l for l in todas if str(l.get("status", "")).strip().upper() == "PRONTO"]
        barradas = len(todas) - len(elegiveis)
        print(f"{len(todas)} linha(s) em {ABA_INPUT}; {len(elegiveis)} PRONTO, "
              f"{barradas} retida(s) (renegociado, acordo, jurídico, sem equipamento).")
        if barradas and not elegiveis:
            raise RuntimeError("Nenhuma linha PRONTO. Revise a Campanha_Input: "
                               "renegociado e acordo não recebem aviso de bloqueio.")
    else:
        elegiveis = todas
        print(f"{len(todas)} linha(s) em {ABA_INPUT} (sem coluna 'status': "
              f"nenhum filtro de renegociado/acordo aplicado).")

    entrada = [l.get("codigo") or l.get("cliente") or l.get("cnpj") for l in elegiveis]
    entrada = [e for e in entrada if str(e or "").strip()]

    docs = set()
    for bruto in entrada:
        tipo, valor = cfg.classificar_entrada(bruto)
        if tipo:
            docs.add(valor)

    cad = _cadastros(bq, docs)
    por_codigo = {r.codigo: r for r in cad.itertuples()}
    cadastros = {r.codigo: r.doc for r in cad.itertuples()}

    resolucao = cfg.resolver_lista(
        entrada=entrada,
        cadastros=cadastros,
        codigos_com_titulo={r.codigo for r in cad.itertuples() if r.n_titulos > 0},
        codigos_com_destinatario={r.codigo for r in cad.itertuples() if r.n_destinatarios > 0},
    )
    print(resolucao.resumo())

    codigos = resolucao.incluidos
    if not codigos:
        raise RuntimeError("Nenhum cliente sobrou. Corrija a lista antes de seguir.")

    # --- congelamento
    df_tit = _q(bq, "titulos.sql", data_envio=c.data_envio, lista_clientes=codigos)
    df_bmb = _q(bq, "bombas.sql", lista_clientes=codigos)

    enc = [encargos.calcular_encargos(r.saldo, int(r.dias_atraso)) for r in df_tit.itertuples()]
    df_tit["encargos"] = [float(e["encargos"]) for e in enc]
    df_tit["total"] = [float(e["total"]) for e in enc]
    df_tit.insert(0, "id_campanha", c.id_campanha)
    df_bmb.insert(0, "id_campanha", c.id_campanha)

    # --- vendedores: tudo na planilha 1. A Contatos_Emails já traz o
    # CARTEIRA vem da Base_Clientes (planilha 2), não da coluna `vendedor` da
    # Contatos_Emails. Aquela coluna é o `representante` do CIGAM — cadastro
    # antigo, não carteira — e foi de lá que saiu "MÁRIO LUCAS", colaborador
    # de divisão 40 que nunca foi vendedor.
    #
    # Se a planilha 2 não abrir, segue com a fonte antiga e AVISA, em vez de
    # derrubar a campanha: vendedor em cópia é acessório, não pode travar o
    # aviso ao cliente.
    base_clientes = []
    try:
        p2 = io.abrir(gc, ID_VENDEDORES)
        base_clientes = io.ler_aba(p2, ABA_BASE_CLIENTES, obrigatoria=False)
    except Exception as exc:
        print(f"  AVISO: não abri a planilha 2 ({type(exc).__name__}). "
              f"Vendedor virá da Contatos_Emails, que é fonte errada.")

    resv = vend.resolver_vendedores(
        base_clientes=base_clientes,
        contatos_emails=io.ler_aba(p1, ABA_CONTATOS),
        email_vendedores=io.ler_aba(p1, ABA_EMAIL_VEND),
        codigos=codigos,
    )
    print(resv.resumo())

    # --- prévia
    previa, triagem = [], []
    for cod in codigos:
        r = por_codigo[cod]
        tit = df_tit[df_tit.codigo_cliente == cod]
        bmb = df_bmb[df_bmb.codigo_cliente == cod]
        copia = resv.em_copia.get(cod)

        previa.append({
            "id_campanha": c.id_campanha, "codigo_cliente": cod,
            "nome_cliente": r.nome_cliente, "cnpj_cpf": r.cnpj_cpf,
            "titulos": len(tit),
            "saldo": round(tit.saldo.astype(float).sum(), 2),
            "encargos": round(tit.encargos.sum(), 2),
            "total": round(tit.total.sum(), 2),
            "bombas": len(bmb),
            "destinatarios": "; ".join(r.emails),
            "vendedor": (copia or {}).get("vendedor", ""),
            "email_vendedor": "; ".join((copia or {}).get("emails", [])),
        })

        if len(bmb) == 0:
            triagem.append({"id_campanha": c.id_campanha, "codigo_cliente": cod,
                            "nome_cliente": r.nome_cliente, "caso": "sem bomba alocada",
                            "detalhe": "pode ser bloqueio anterior, sem hardware, ou "
                                       "alocação não validada no app — não automatizar"})
    for cod, motivo in resv.bloqueados.items():
        triagem.append({"id_campanha": c.id_campanha, "codigo_cliente": cod,
                        "nome_cliente": por_codigo[cod].nome_cliente,
                        "caso": "sem vendedor em cópia", "detalhe": motivo})
    for bruto, motivo in resolucao.rejeitados:
        triagem.append({"id_campanha": c.id_campanha, "codigo_cliente": str(bruto),
                        "nome_cliente": "", "caso": "rejeitado na lista", "detalhe": motivo})

    # DATAS NA PRÓPRIA PRÉVIA. Elas viviam só na aba Campanhas, e apagar
    # aquela aba deixava a campanha inalcançável: o abrir_campanha não tinha
    # de onde tirar data_envio e data_limite, e sem data_envio não dá para
    # reabrir — ela é a âncora dos encargos que o cliente já recebeu.
    # Duas colunas fazem o snapshot bastar por si.
    df_previa = pd.DataFrame(previa)
    df_previa["data_envio"] = f"{c.data_envio:%Y-%m-%d}"
    df_previa["data_limite"] = f"{c.data_limite:%Y-%m-%d}"
    io.escrever_aba(p1, ABA_PREVIA, df_previa)
    io.escrever_aba(p1, ABA_TITULOS, df_tit)
    io.escrever_aba(p1, ABA_BOMBAS, df_bmb)
    io.escrever_aba(p1, ABA_TRIAGEM, pd.DataFrame(
        triagem, columns=["id_campanha", "codigo_cliente", "nome_cliente",
                          "caso", "detalhe"]))

    total = sum(p["total"] for p in previa)
    print(f"\nCONGELADO: {len(previa)} cliente(s), {len(df_tit)} título(s), "
          f"{len(df_bmb)} bomba(s), R$ {total:,.2f}.")
    print(f"{len(triagem)} caso(s) em {ABA_TRIAGEM}. Confira antes de disparar.")

    registrar(gc, c, "preparado",
              clientes=len(previa), titulos=len(df_tit), bombas=len(df_bmb),
              valor_cobrado=round(total, 2), casos_triagem=len(triagem))
    return {"previa": previa, "triagem": triagem}


# ------------------------------------------------------------------- fase B


def montar_fila(c, gc) -> list:
    """Constrói as mensagens a partir do CONGELADO, sem enviar nada.

    Extraída do disparar() para a prévia usar exatamente o mesmo caminho: se a
    prévia montasse o e-mail por conta própria, você estaria conferindo uma
    coisa e disparando outra.
    """
    p1 = io.abrir(gc, ID_DESTINO)

    previa = [l for l in io.ler_aba(p1, ABA_PREVIA) if l["id_campanha"] == c.id_campanha]
    titulos = [l for l in io.ler_aba(p1, ABA_TITULOS) if l["id_campanha"] == c.id_campanha]
    bombas = [l for l in io.ler_aba(p1, ABA_BOMBAS) if l["id_campanha"] == c.id_campanha]
    if not previa:
        raise RuntimeError(f"Nada congelado para '{c.id_campanha}'. Rode preparar() antes.")

    import nfse as nfse_ponte
    mapa_nfse = nfse_ponte.carregar(p1, io)

    fila = []
    for cli in previa:
        # Normaliza os três lados. Aqui as fontes vêm todas da mesma planilha e
        # casavam por acidente, não por desenho: bastava uma delas passar a ter
        # zero à esquerda para o e-mail sair sem títulos ou sem bombas.
        cod = _codigo(cli["codigo_cliente"])
        tit = [_num(t) for t in titulos if _codigo(t["codigo_cliente"]) == cod]
        bmb = [_bool(b) for b in bombas if _codigo(b["codigo_cliente"]) == cod]
        if not tit:
            continue

        tit = nfse_ponte.aplicar(tit, mapa_nfse)
        corpo = template.montar_email(cli, tit, c)
        html = corpo["html"]
        if bmb:
            html = html.replace(
                "<p>Informamos que",
                template.quadro_equipamentos(bmb, cli["nome_cliente"]) + "<p>Informamos que",
                1,
            )

        cc = [e for e in (cli.get("email_vendedor") or "").split(";") if e.strip()]
        cc = [e.strip() for e in cc] + list(c.copia_padrao)

        fila.append(envio.Job(
            codigo_cliente=cod, nome_cliente=cli["nome_cliente"],
            para=[e.strip() for e in (cli.get("destinatarios") or "").split(";") if e.strip()],
            cc=list(dict.fromkeys(cc)),
            assunto=corpo["assunto"], html=html, texto=corpo["texto"],
        ))

    return fila


def previa_email(c, gc, codigo: str = None, quantos: int = 1):
    """Mostra o e-mail renderizado no notebook. NÃO envia, não toca no log,
    não precisa de SMTP."""
    from IPython.display import HTML, display

    fila = montar_fila(c, gc)
    if codigo:
        alvo = str(codigo).zfill(6)
        fila = [j for j in fila if j.codigo_cliente == alvo]
        if not fila:
            print(f"{alvo} não está na fila. Códigos: "
                  f"{', '.join(j.codigo_cliente for j in montar_fila(c, gc))[:200]}")
            return []

    for job in fila[:quantos]:
        print("=" * 78)
        print(f"PARA:     {'; '.join(job.para)}")
        print(f"CC:       {'; '.join(job.cc)}")
        print(f"ASSUNTO:  {job.assunto}")
        print("=" * 78)
        display(HTML(job.html))
    return fila[:quantos]


def disparar(c, gc, enviar_fn=None) -> envio.Resultado:
    """Lê o congelado e envia. NUNCA consulta o BigQuery."""
    p1 = io.abrir(gc, ID_DESTINO)
    fila = montar_fila(c, gc)

    r = envio.disparar(
        fila, c,
        enviar_fn=enviar_fn or (lambda job: None),
        log_fn=lambda linha: io.append_log(p1, ABA_LOG, linha),
        # ENSAIO consulta o log de PRODUÇÃO, não o dele próprio. O ensaio
        # simula produção: se ele usasse o próprio modo, não mostraria quem
        # seria pulado e deixaria de ser portão — o número que interessa é
        # quantos SAIRIAM de verdade agora.
        chaves_ja_enviadas=io.chaves_ja_enviadas(
            p1, ABA_LOG, c.id_campanha,
            modo="PRODUCAO" if c.modo == "DRY_RUN" else c.modo,
            reenviar=c.reenviar),
    )
    print(r.resumo())
    registrar(gc, c, "disparado",
              enviados=len(r.enviados), pulados=len(r.pulados),
              falhas=len(r.falhas), pendentes=len(r.pendentes),
              destinatarios=r.destinatarios_usados)
    return r


# ------------------------------------------------------------------- fase C


def atualizar_painel(c, gc, bq=None, saldo_hoje=None, seriais_hoje=None):
    """Monta e escreve o Painel_Bloqueio. Pode rodar em qualquer momento.

    Virou função porque a versão em célula usava l["id_campanha"] com colchete
    e lia a Campanha_Previa como obrigatória: sem o preparar() a aba não
    existe, e a linha "(nenhum registro nesta campanha)" — que eu escrevo
    quando um DataFrame sai vazio — não tem essa chave. Os dois casos
    estouravam em vez de dizer o que faltava.
    """
    import painel

    p1 = io.abrir(gc, ID_DESTINO)

    def _da_campanha(aba):
        return [l for l in io.ler_aba(p1, aba, obrigatoria=False)
                if str(l.get("id_campanha") or "") == c.id_campanha]

    previa = _da_campanha(ABA_PREVIA)
    if not previa:
        print(f"Nada em '{ABA_PREVIA}' para {c.id_campanha}. "
              f"Rode preparar() antes — o painel descreve o que foi congelado.")
        return []

    # Consulta os seriais de HOJE quando `bq` está disponível. Sem isso o
    # painel não tem como distinguir "bomba bloqueada" de "não fui olhar", e
    # None é o único valor honesto — a etapa BLOQUEADO simplesmente não sai.
    if seriais_hoje is None and bq is not None:
        seriais_hoje = {
            str(r.serial) for r in bq.query(f"""
                SELECT DISTINCT CAST(serial_equipamento AS STRING) AS serial
                FROM `{PROJECT_ID}.gold.bombas_alocadas`
                WHERE serial_equipamento IS NOT NULL
            """).result()
        }
        print(f"  {len(seriais_hoje)} serial(is) hoje em gold.bombas_alocadas")
    elif seriais_hoje is None:
        print("  sem `bq`: não consultei os seriais, então a etapa BLOQUEADO "
              "não será derivada (ausência de dado não é bloqueio)")

    linhas = painel.montar(
        etapa_anterior=painel.etapas_da_aba(p1, io),
        previa=previa,
        log=io.ler_aba(p1, ABA_LOG, obrigatoria=False),
        campanha=c,
        bombas_snapshot=_da_campanha(ABA_BOMBAS),
        saldo_hoje=saldo_hoje,
        seriais_hoje=seriais_hoje,
    )
    painel.escrever(p1, linhas, io)
    return linhas


def _bool_valor(v) -> bool:
    """'TRUE'/'1'/'sim' -> True. O Sheets devolve booleano como texto."""
    return str(v or "").strip().upper() in ("TRUE", "1", "SIM", "VERDADEIRO")


def _codigo(valor) -> str:
    """Código CIGAM com 6 dígitos. O Sheets come o zero à esquerda quando o
    valor entra como número, então a Campanha_Previa guarda `2554` e o DW usa
    `002554` — o IN UNNEST não casa e o saldo de hoje vem vazio.

    Consequência real na 2026-09-B: os 38 clientes apareceram como quitados e
    a campanha registrou R$ 190.513,82 de recuperação inexistente. O nível
    título escapou porque casa por codigoLancamento, não por código.
    """
    import re as _re
    d = _re.sub(r"\D", "", str(valor or ""))
    return d.zfill(6) if d else ""


def _numero_br(valor) -> float:
    """Aceita "4.148,76" e "4148.76". A prévia vem do Sheets em pt-BR."""
    import re as _re
    t = _re.sub(r"[^\d,.\-]", "", str(valor if valor not in (None, "") else 0))
    if not t:
        return 0.0
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


ABA_CONFERENCIA = "Campanha_Conferencia"


def conferir_titulos(c, gc, bq, escrever: bool = True) -> dict:
    """APOSENTADA — use auditar_titulos().

    Comparava saldo antes e depois, e por isso não distingue pagamento de
    baixa contábil, abatimento ou unificação: as quatro reduzem saldo e
    apareciam como quitação. Foi o que produziu "268 de 268 quitados".

    Mantida só para leitura de campanhas antigas. Não é mais chamada pelo
    reconciliar, e a aba Campanha_Conferencia pode ser apagada.
    """
    """Confere TÍTULO A TÍTULO o que mudou desde o aviso.

    A reconciliação compara saldo somado por cliente, e isso confunde três
    coisas que não são a mesma:

      - o cliente pagou aquele título
      - o título foi BAIXADO PARA X90, o que reduz o saldo normal sem ninguém
        ter pago nada (e a régua vai continuar cobrando, porque X90 é dívida)
      - o título sumiu da view por outro motivo (cancelamento, unificação)

    No agregado as três parecem pagamento. Aqui não: o join é por
    codigoLancamento, que é único por lançamento, e comparamos saldo E
    portador.

    Estados por título:
      QUITADO     saiu da view com saldo zerado  -> pagou
      REDUZIDO    saldo menor, mesmo portador    -> pagou em parte
      X90         virou baixa contábil           -> NÃO pagou
      INALTERADO  mesmo saldo
      AUMENTOU    saldo maior (juros capitalizados ou correção no ERP)
    """
    p1 = io.abrir(gc, ID_DESTINO)
    snap = [l for l in io.ler_aba(p1, ABA_TITULOS, obrigatoria=False)
            if str(l.get("id_campanha") or "").strip() == c.id_campanha]
    if not snap:
        print(f"Sem títulos congelados para {c.id_campanha}."); return {}

    lancamentos = [str(l.get("codigoLancamento") or "").strip() for l in snap]
    lancamentos = [x for x in lancamentos if x]

    sql = f"""
    SELECT CAST(codigoLancamento AS STRING) AS codigoLancamento,
           ROUND(saldo, 2)                  AS saldo_hoje,
           COALESCE(codigoPortador, '')     AS portador_hoje,
           situacao                         AS situacao_hoje
    FROM `{PROJECT_ID}.silver.titulos_cigam`
    WHERE CAST(codigoLancamento AS STRING) IN UNNEST(@lanc)
    """
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("lanc", "STRING", lancamentos)]))
    agora = {r.codigoLancamento: r for r in job.result()}

    linhas, cont = [], {}
    for t in snap:
        lanc = str(t.get("codigoLancamento") or "").strip()
        antes = _numero_br(t.get("saldo"))
        port_antes = str(t.get("codigoPortador") or "").strip()
        r = agora.get(lanc)

        if r is None:
            estado, saldo_hoje, port_hoje = "QUITADO", 0.0, ""
        else:
            saldo_hoje = float(r.saldo_hoje or 0)
            port_hoje = r.portador_hoje
            if port_hoje.startswith("X9") and not port_antes.startswith("X9"):
                estado = "X90"          # baixa contábil, não pagamento
            elif saldo_hoje <= 0:
                estado = "QUITADO"
            elif saldo_hoje < antes - 0.005:
                estado = "REDUZIDO"
            elif saldo_hoje > antes + 0.005:
                estado = "AUMENTOU"
            else:
                estado = "INALTERADO"

        cont[estado] = cont.get(estado, 0) + 1
        linhas.append({
            "id_campanha": c.id_campanha,
            "codigo_cliente": t.get("codigo_cliente"),
            "nome_cliente": t.get("nome_cliente"),
            "codigoLancamento": lanc,
            "doc": t.get("doc"),
            "dataVencimento": t.get("dataVencimento"),
            "estado": estado,
            "saldo_no_aviso": antes,
            "saldo_hoje": saldo_hoje,
            "variacao": round(saldo_hoje - antes, 2),
            "portador_no_aviso": port_antes or "—",
            "portador_hoje": port_hoje or "—",
        })

    pago = round(sum(l["saldo_no_aviso"] - l["saldo_hoje"] for l in linhas
                     if l["estado"] in ("QUITADO", "REDUZIDO")), 2)
    virou_x90 = round(sum(l["saldo_no_aviso"] for l in linhas
                          if l["estado"] == "X90"), 2)

    print(f"{len(linhas)} título(s) conferido(s):")
    for e in ("QUITADO", "REDUZIDO", "INALTERADO", "AUMENTOU", "X90"):
        if cont.get(e):
            print(f"   {e:<11} {cont[e]:>4}")
    print(f"  pago de verdade:  R$ {pago:,.2f}")
    if virou_x90:
        print(f"  virou X90:        R$ {virou_x90:,.2f}  <- NÃO é pagamento; "
              f"no agregado por cliente pareceria quitação")

    if escrever:
        import pandas as pd
        io.escrever_aba(p1, ABA_CONFERENCIA, pd.DataFrame(linhas))

    return {"linhas": linhas, "contagem": cont,
            "valor_pago": pago, "valor_x90": virou_x90}


ABA_TITULOS_RESULTADO = "Campanha_Titulos_Resultado"




def reconciliar(c, gc, bq) -> dict:
    """Única releitura do BigQuery no ciclo. Quem zerou sai; quem pagou em
    parte é decisão humana — abrir card de bloqueio para quem pagou no dia 12
    é o erro que queima a operação inteira."""
    p1 = io.abrir(gc, ID_DESTINO)
    previa = [l for l in io.ler_aba(p1, ABA_PREVIA) if l["id_campanha"] == c.id_campanha]
    codigos = [_codigo(l["codigo_cliente"]) for l in previa]

    # X90 CONTA, como em todo o resto do pipeline. Excluir aqui era pior que
    # inconsistente: o saldo_no_aviso INCLUI X90 e o de hoje não incluía, então
    # todo cliente com dívida X90 parecia ter pago exatamente esse valor. A
    # Construtora Luiz Costa, com R$ 6.758 100% X90, apareceria como quitada.
    sql = f"""
    SELECT codigoEmpresa AS codigo_cliente,
           ROUND(SUM(saldo), 2) AS saldo_hoje,
           COUNT(*)             AS titulos_hoje
    FROM `{PROJECT_ID}.silver.titulos_cigam`
    WHERE codigoEmpresa IN UNNEST(@codigos)
      AND saldo > 0
      AND dataVencimento < CURRENT_DATE('America/Sao_Paulo')
    GROUP BY 1
    """
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("codigos", "STRING", codigos)]))
    linhas_hoje = {r.codigo_cliente: (float(r.saldo_hoje), int(r.titulos_hoje))
                   for r in job.result()}
    hoje = {k: v[0] for k, v in linhas_hoje.items()}

    # TRAVA. Zero clientes com saldo hoje, numa campanha inteira, é quase
    # sempre falha de casamento de chave — e o resultado dela é "todos
    # quitaram", que é exatamente o erro mais caro que este script pode
    # cometer. Aconteceu na 2026-09-B por causa do zero à esquerda.
    if not linhas_hoje and len(codigos) > 3:
        raise RuntimeError(
            f"Nenhum dos {len(codigos)} clientes tem saldo vencido hoje no DW. "
            f"Isso classificaria TODOS como quitados. Confira o formato do "
            f"código: enviei {codigos[:3]}, o DW usa 6 dígitos com zero à "
            f"esquerda (ex.: '002554').")

    quitou, parcial, mantem = [], [], []
    tit_antes = tit_hoje = 0
    for l in previa:
        antes = _numero_br(l.get("saldo"))
        agora = hoje.get(_codigo(l["codigo_cliente"]), 0.0)
        n_antes = int(float(l.get("titulos") or 0))
        n_hoje = linhas_hoje.get(_codigo(l["codigo_cliente"]), (0.0, 0))[1]
        tit_antes += n_antes
        tit_hoje += n_hoje
        registro = {**l, "saldo_no_aviso": antes, "saldo_hoje": agora,
                    "pagou": round(antes - agora, 2),
                    "titulos_no_aviso": n_antes, "titulos_hoje": n_hoje}
        (quitou if agora <= 0 else parcial if agora < antes else mantem).append(registro)

    # RENEGOCIAÇÃO: quem está na prévia PASSOU pelo filtro de acordo, ou seja,
    # não tinha acordo vigente quando foi avisado. Se aparece na aba ACORDOS
    # agora, negociou depois do aviso. É a medida limpa do efeito da campanha
    # sobre negociação — não dá para confundir com acordo antigo.
    renegociaram = []
    try:
        import acordos as ac
        reg_ac = ac.carregar(gc, io)
        renegociaram = [l["codigo_cliente"] for l in previa
                        if reg_ac.status(_codigo(l["codigo_cliente"]))
                        in (ac.PAGANDO, ac.QUEBRADO)]
    except Exception as exc:
        print(f"  (não consegui medir renegociação: {type(exc).__name__})")

    print(f"quitou: {len(quitou)} | pagou em parte: {len(parcial)} | "
          f"sem movimento: {len(mantem)}")
    if parcial:
        print("PAGAMENTO PARCIAL — decidir um a um antes de abrir card:")
        for r in parcial:
            print(f"   {r['codigo_cliente']} {r['nome_cliente'][:30]}: "
                  f"{r['saldo_no_aviso']:.2f} -> {r['saldo_hoje']:.2f}")
    recuperado = round(sum(r["saldo_no_aviso"] - r["saldo_hoje"]
                           for r in quitou + parcial), 2)
    if renegociaram:
        print(f"renegociaram depois do aviso: {len(renegociaram)} "
              f"({', '.join(renegociaram[:8])}{'...' if len(renegociaram) > 8 else ''})")

    # AUDITORIA, não conferência. As duas respondiam a mesma pergunta com
    # números diferentes: a conferência compara saldo antes/depois e não
    # distingue pagamento de baixa contábil — foi ela que reportou 268 de 268
    # quitados. A auditoria segue os lançamentos de baixa e separa pago,
    # juros, abatido, unificado e write-off.
    #
    # Manter as duas era ter duas versões do mesmo fato na planilha, que é o
    # erro que este projeto passou semanas corrigindo em outros lugares.
    aud = auditar_titulos(c, gc, bq)
    cont = {}
    for l in aud.get("linhas", []):
        cont[l["estado"]] = cont.get(l["estado"], 0) + 1

    registrar(gc, c, "reconciliado",
              quitou=len(quitou), pagou_parcial=len(parcial),
              mantem_bloqueio=len(mantem),
              valor_recuperado=recuperado,
              titulos_no_aviso=tit_antes,
              titulos_pagos=cont.get("PAGO", 0),
              titulos_acordados=sum(v for k, v in cont.items() if "ACORDADO" in k),
              titulos_sem_movimento=cont.get("SEM MOVIMENTO", 0),
              titulos_baixa_contabil=cont.get("BAIXA CONTABIL", 0),
              titulos_abatidos=cont.get("ABATIDO", 0),
              titulos_unificados=sum(v for k, v in cont.items() if "X50" in k),
              # "pago de verdade" exclui o que só virou baixa contábil. No
              # agregado por cliente, X90 parece quitação e infla a recuperação.
              valor_pago_titulos=round(sum(l["valor_pago"] + l["valor_juros"]
                                           for l in aud.get("linhas", [])), 2),
              valor_nao_entrou=round(sum(l["valor_abatido"]
                                         + l["valor_baixa_contabil"]
                                         + l["valor_outro"]
                                         for l in aud.get("linhas", [])), 2),
              renegociaram=len(renegociaram))
    return {"quitou": quitou, "parcial": parcial, "mantem": mantem,
            "renegociaram": renegociaram}


ABA_CARDS = "Campanha_Cards"


def gerar_cards(c, gc, codigos: list, escrever: bool = True) -> list:
    """Texto dos cards, a partir do CONGELADO. `codigos` sai da reconciliação —
    esta função não decide quem bloqueia."""
    codigos = [_codigo(x) for x in codigos]
    p1 = io.abrir(gc, ID_DESTINO)
    # Normaliza os DOIS lados. A lista vem da reconciliação (já normalizada) e
    # a prévia do Sheets (sem zero à esquerda): sem isto nenhum card sairia, e
    # "zero cards" pareceria "ninguém a bloquear".
    previa = {_codigo(l["codigo_cliente"]): l for l in io.ler_aba(p1, ABA_PREVIA)
              if l["id_campanha"] == c.id_campanha}
    bombas = [l for l in io.ler_aba(p1, ABA_BOMBAS) if l["id_campanha"] == c.id_campanha]

    cards = []
    for cod in codigos:
        bmb = [_bool(b) for b in bombas
               if _codigo(b["codigo_cliente"]) == cod]
        if cod not in previa:
            print(f"  {cod}: não está na prévia desta campanha — card não gerado")
            continue
        if not bmb:
            print(f"  {cod}: sem bomba no snapshot — card não gerado")
            continue
        # O tipo de solicitação depende do GRUPO, e o snapshot já tem as
        # colunas: sem passar isto, `inseguro` é sempre falso e todo card sai
        # como "Bloqueio do sistema e da bomba" — inclusive os que têm bomba
        # de pagante adimplente no mesmo sistema, cujo bloqueio de sistema
        # derrubaria quem pagou em dia. Foi o que aconteceu na 2026-09-B: 36
        # de 36 cards com o tipo mais agressivo.
        seguro = all(_bool_valor(b.get("bloqueio_de_sistema_seguro"))
                     for b in bmb)
        adimplentes = max((int(float(b.get("bombas_de_adimplentes") or 0))
                           for b in bmb), default=0)
        nomes = sorted({str(b.get("adimplentes_no_sistema") or "").strip()
                        for b in bmb} - {""})
        # O pipefy consome quatro atributos do grupo. Montar só parte deles
        # estoura na hora de escrever as observações, então os quatro saem do
        # snapshot — que já tem todas essas colunas.
        ids_sistema = sorted({str(b.get("cliente_id") or "").strip()
                              for b in bmb} - {""})
        # `adimplentes_no_sistema` vem com os nomes separados por " | ".
        terceiros = sorted({n.strip()
                            for b in bmb
                            for n in str(b.get("adimplentes_no_sistema") or "").split("|")
                            if n.strip()})
        grupo = types.SimpleNamespace(
            bloqueio_de_sistema_seguro=seguro,
            bombas_de_adimplentes=adimplentes,
            cliente_ids=ids_sistema,
            terceiros_adimplentes=terceiros,
        )
        cards.append(pipefy.montar_card(
            previa[cod], bmb, campanha=c,
            total_devido=_numero_br(previa[cod].get("total")),
            grupo=grupo))

    # TRAVA. Zero cards com lista não vazia é falha de casamento, não decisão
    # de negócio — foi o que aconteceu quando eu normalizei a prévia e a lista
    # mas esqueci a Campanha_Bombas: 36 clientes "sem bomba" que têm bomba.
    if codigos and not cards:
        raise RuntimeError(
            f"{len(codigos)} cliente(s) na lista e NENHUM card gerado. "
            f"Isso é falha de chave, não ausência de bloqueio. "
            f"Códigos enviados: {codigos[:3]}; "
            f"na prévia: {list(previa)[:3]}; "
            f"em '{ABA_BOMBAS}': "
            f"{[_codigo(b['codigo_cliente']) for b in bombas[:3]]}")

    por_tipo = {}
    for card in cards:
        t = card["tipo_solicitacao"]
        por_tipo[t] = por_tipo.get(t, 0) + 1
    print(f"{len(cards)} card(s) prontos:")
    for t, n in sorted(por_tipo.items()):
        print(f"   {n:>3}  {t}")

    if escrever and cards:
        _escrever_cards(gc, c, cards)
    return cards


def _escrever_cards(gc, c, cards: list):
    """Grava os cards na planilha, preservando o que você já preencheu.

    Sem isto o texto só existia no output do Colab: fechando a sessão, os 36
    cards somem, e se você abrir 20 e o runtime cair não há registro de quais
    faltam. As colunas `aberto_em` e `card_id` são SUAS — o script nunca as
    sobrescreve, então dá para marcar o que já foi para o Pipefy.
    """
    import pandas as pd

    p1 = io.abrir(gc, ID_DESTINO)
    antigas = io.ler_aba(p1, ABA_CARDS, obrigatoria=False)

    # Preserva o preenchimento humano por (campanha, cliente).
    humano = {(str(l.get("id_campanha") or "").strip(),
               _codigo(l.get("codigo_cliente"))): l for l in antigas}

    linhas = []
    for card in cards:
        # As chaves internas do card têm sublinhado: `_codigo_cliente` e
        # `_qtd_equipamentos`. Lendo sem ele, a coluna saía vazia nas 36 linhas.
        cod = _codigo(card.get("_codigo_cliente"))
        ja = humano.get((c.id_campanha, cod), {})
        linhas.append({
            "id_campanha": c.id_campanha,
            "codigo_cliente": cod,
            "cliente": card.get("titulo"),
            "cnpj": card.get("cnpj"),
            "tipo_solicitacao": card.get("tipo_solicitacao"),
            "equipamentos": card.get("_qtd_equipamentos", ""),
            "sistema_seguro": card.get("_bloqueio_de_sistema_seguro"),
            "observacoes": card.get("observacoes"),
            # suas, nunca sobrescritas
            "aberto_em": ja.get("aberto_em", ""),
            "card_id": ja.get("card_id", ""),
        })

    # Mantém as campanhas anteriores na aba.
    outras = [l for l in antigas
              if str(l.get("id_campanha") or "").strip() != c.id_campanha]
    df = pd.DataFrame(outras + linhas)
    io.escrever_aba(p1, ABA_CARDS, df)

    ja_abertos = sum(1 for l in linhas if str(l["card_id"]).strip())
    print(f"  '{ABA_CARDS}': {len(linhas)} card(s) desta campanha"
          + (f", {ja_abertos} já com card_id preenchido." if ja_abertos
             else ". Preencha `card_id` conforme abrir no Pipefy."))


# ------------------------------------------------------------------ utilitários


def _num(linha: dict) -> dict:
    """Sheets devolve tudo string. Converte o que o template precisa como número."""
    d = dict(linha)
    for k in ("saldo", "encargos", "total"):
        # Dois formatos convivem: pt-BR do Sheets ("1.152,00") e americano de
        # quem calculou em Python ("64.80"). Remover o ponto cegamente estraga
        # o segundo: 64.80 virava 6480. Decide pela vírgula — se ela existe, o
        # ponto é separador de milhar; se não, o ponto é o decimal.
        bruto = str(d.get(k) if d.get(k) not in (None, "") else 0).strip()
        bruto = (bruto.replace(".", "").replace(",", ".")
                 if "," in bruto else bruto)
        try:
            d[k] = float(bruto or 0)
        except ValueError:
            raise ValueError(
                f"Coluna '{k}' da Campanha_Titulos veio como {d.get(k)!r}, que "
                f"não é número. Quase sempre é FORMATO de célula: o Sheets "
                f"exibe 120,38 como 1900-04-29 quando a coluna herdou formato "
                f"de data. Rode preparar() de novo com o io_sheets atualizado, "
                f"que limpa a formatação antes de escrever."
            ) from None
    d["dias_atraso"] = int(float(d.get("dias_atraso") or 0))
    # ler_aba normaliza o cabeçalho para minúsculas, então `codigoContrato`
    # chega como `codigocontrato` e o template não acha. Restaura os nomes que
    # o template espera. Foi por isso que a coluna Contrato saiu "—" em 37 de
    # 37 linhas: o dado estava lá, com outro nome.
    for camel in ("codigoContrato", "dataVencimento", "codigoPortador",
                  "codigoLancamento", "tipo_pendencia"):
        if camel not in d and camel.lower() in d:
            d[camel] = d[camel.lower()]

    if isinstance(d.get("dataVencimento"), str):
        v = d.get("dataVencimento")
        try:
            d["dataVencimento"] = date.fromisoformat(v[:10])
        except ValueError:
            d["dataVencimento"] = v
    return d


def _bool(linha: dict) -> dict:
    d = dict(linha)
    d["instalada_em_terceiro"] = str(
        d.get("instalada_em_terceiro", "")).strip().upper() in ("TRUE", "VERDADEIRO", "1")
    return d


if __name__ == "__main__":
    print(__doc__)
