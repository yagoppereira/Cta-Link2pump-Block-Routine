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
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

import campanha_v2 as cfg
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
ID_VENDEDORES = "1KSd7cdHUiotmE6WAePq0WnXzhLZz8vggwPkcn1ZGBv4"   # não usado

ABA_INPUT = "Campanha_Input"
ABA_PREVIA = "Campanha_Previa"
ABA_TITULOS = "Campanha_Titulos"
ABA_BOMBAS = "Campanha_Bombas"
ABA_LOG = "Campanha_Log"
ABA_TRIAGEM = "Triagem"
ABA_TRIAGEM_REGRA = "Triagem_Sem_Alocacao"
ABA_EXCECOES = "Nunca_Notificar"
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


def candidatos(gc, bq, freq_minima: int = 7, freq_propria_minima: int = 3,
               dias_uso: int = 90, escrever: bool = True) -> list:
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
    #   chave = código CIGAM (006 dígitos) OU raiz de CNPJ (8 dígitos)
    #   a raiz pega o grupo inteiro de uma vez: Aço Verde tem 4 cadastros,
    #   Rio Itá 4, Translovato 5 — listar um a um envelhece mal.
    excecoes = io.ler_aba(p1, ABA_EXCECOES, obrigatoria=False)
    bloqueados, motivo_de = set(), {}
    for l in excecoes:
        chave = re.sub(r"\D", "", str(l.get("chave") or l.get("codigo") or ""))
        if not chave:
            continue
        chave = chave.zfill(6) if len(chave) <= 6 else chave[:8]
        bloqueados.add(chave)
        motivo_de[chave] = str(l.get("motivo") or "").strip() or "sem motivo informado"

    sql = (SQL / "base_inadimplencia.sql").read_text()
    sql = sql.replace("DECLARE dias_uso_recente INT64 DEFAULT 90;",
                      f"DECLARE dias_uso_recente INT64 DEFAULT {int(dias_uso)};")
    df = bq.query(sql).to_dataframe(create_bqstorage_client=False)

    if bloqueados:
        na_lista = df.codigo.isin(bloqueados) | df.cnpj_raiz.isin(bloqueados)
        vetados = df[na_lista]
        df = df[~na_lista]
        if len(vetados):
            print(f"{len(vetados)} cadastro(s) fora por '{ABA_EXCECOES}' "
                  f"(R$ {vetados.em_atraso.astype(float).sum():,.2f}):")
            for r in vetados.itertuples():
                chave = r.codigo if r.codigo in motivo_de else r.cnpj_raiz
                print(f"   {r.codigo} {str(r.cliente)[:32]:<32} {motivo_de.get(chave,'')}")

    qualificado = ((df.frequencia >= freq_minima)
                   & (df.frequencia_propria >= freq_propria_minima))
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

    dentro = df[qualificado.reindex(df.index, fill_value=False)
                & (df.usa_equipamento == True)]
    sem_uso = df[qualificado.reindex(df.index, fill_value=False)
                 & (df.usa_equipamento.isna())]
    so_pelo_grupo = df[(df.frequencia >= freq_minima)
                       & (df.frequencia_propria < freq_propria_minima)]

    print(f"{len(df)} cliente(s) com dívida | grupo >= {freq_minima}: "
          f"{(df.frequencia >= freq_minima).sum()} | e própria >= "
          f"{freq_propria_minima}: {qualificado.sum()}")
    if len(so_pelo_grupo):
        print(f"  {len(so_pelo_grupo)} fora por frequência própria baixa "
              f"(entrariam só pelo grupo)")
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

    if escrever and len(dentro):
        import pandas as pd
        saida = dentro.copy()
        saida.insert(0, "acao", "")          # em branco = entra
        io.escrever_aba(p1, ABA_INPUT, saida)
        io.escrever_aba(p1, ABA_TRIAGEM_REGRA, sem_uso)
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
    # cruzamento cliente<->vendedor pronto; join por codigo_cliente, nunca por
    # cnpj_cpf (essa coluna virou número e perdeu zeros à esquerda).
    resv = vend.resolver_vendedores(
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

    io.escrever_aba(p1, ABA_PREVIA, pd.DataFrame(previa))
    io.escrever_aba(p1, ABA_TITULOS, df_tit)
    io.escrever_aba(p1, ABA_BOMBAS, df_bmb)
    io.escrever_aba(p1, ABA_TRIAGEM, pd.DataFrame(
        triagem, columns=["id_campanha", "codigo_cliente", "nome_cliente",
                          "caso", "detalhe"]))

    total = sum(p["total"] for p in previa)
    print(f"\nCONGELADO: {len(previa)} cliente(s), {len(df_tit)} título(s), "
          f"{len(df_bmb)} bomba(s), R$ {total:,.2f}.")
    print(f"{len(triagem)} caso(s) em {ABA_TRIAGEM}. Confira antes de disparar.")
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

    fila = []
    for cli in previa:
        cod = cli["codigo_cliente"]
        tit = [_num(t) for t in titulos if t["codigo_cliente"] == cod]
        bmb = [_bool(b) for b in bombas if b["codigo_cliente"] == cod]
        if not tit:
            continue

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
        chaves_ja_enviadas=io.chaves_ja_enviadas(p1, ABA_LOG, c.id_campanha),
    )
    print(r.resumo())
    return r


# ------------------------------------------------------------------- fase C


def reconciliar(c, gc, bq) -> dict:
    """Única releitura do BigQuery no ciclo. Quem zerou sai; quem pagou em
    parte é decisão humana — abrir card de bloqueio para quem pagou no dia 12
    é o erro que queima a operação inteira."""
    p1 = io.abrir(gc, ID_DESTINO)
    previa = [l for l in io.ler_aba(p1, ABA_PREVIA) if l["id_campanha"] == c.id_campanha]
    codigos = [l["codigo_cliente"] for l in previa]

    sql = f"""
    SELECT codigoEmpresa AS codigo_cliente, ROUND(SUM(saldo),2) AS saldo_hoje
    FROM `{PROJECT_ID}.silver.titulos_cigam`
    WHERE codigoEmpresa IN UNNEST(@codigos)
      AND saldo > 0 AND COALESCE(codigoPortador,'') != 'X90'
    GROUP BY 1
    """
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("codigos", "STRING", codigos)]))
    hoje = {r.codigo_cliente: float(r.saldo_hoje) for r in job.result()}

    quitou, parcial, mantem = [], [], []
    for l in previa:
        antes = float(l["saldo"] or 0)
        agora = hoje.get(l["codigo_cliente"], 0.0)
        registro = {**l, "saldo_no_aviso": antes, "saldo_hoje": agora,
                    "pagou": round(antes - agora, 2)}
        (quitou if agora <= 0 else parcial if agora < antes else mantem).append(registro)

    print(f"quitou: {len(quitou)} | pagou em parte: {len(parcial)} | "
          f"sem movimento: {len(mantem)}")
    if parcial:
        print("PAGAMENTO PARCIAL — decidir um a um antes de abrir card:")
        for r in parcial:
            print(f"   {r['codigo_cliente']} {r['nome_cliente'][:30]}: "
                  f"{r['saldo_no_aviso']:.2f} -> {r['saldo_hoje']:.2f}")
    return {"quitou": quitou, "parcial": parcial, "mantem": mantem}


def gerar_cards(c, gc, codigos: list) -> list:
    """Texto dos cards, a partir do CONGELADO. `codigos` sai da reconciliação —
    esta função não decide quem bloqueia."""
    p1 = io.abrir(gc, ID_DESTINO)
    previa = {l["codigo_cliente"]: l for l in io.ler_aba(p1, ABA_PREVIA)
              if l["id_campanha"] == c.id_campanha}
    bombas = [l for l in io.ler_aba(p1, ABA_BOMBAS) if l["id_campanha"] == c.id_campanha]

    cards = []
    for cod in codigos:
        bmb = [_bool(b) for b in bombas if b["codigo_cliente"] == cod]
        if not bmb:
            print(f"  {cod}: sem bomba no snapshot — card não gerado")
            continue
        cards.append(pipefy.montar_card(
            previa[cod], bmb, campanha=c,
            total_devido=float(previa[cod]["total"] or 0)))
    print(f"{len(cards)} card(s) prontos para abrir no Pipefy.")
    return cards


# ------------------------------------------------------------------ utilitários


def _num(linha: dict) -> dict:
    """Sheets devolve tudo string. Converte o que o template precisa como número."""
    d = dict(linha)
    for k in ("saldo", "encargos", "total"):
        bruto = str(d.get(k) or 0).strip().replace(".", "").replace(",", ".")
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
    if isinstance(d.get("datavencimento") or d.get("dataVencimento"), str):
        v = d.get("datavencimento") or d.get("dataVencimento")
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
