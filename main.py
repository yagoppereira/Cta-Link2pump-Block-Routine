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
ID_VENDEDORES = "1KSd7cdHUiotmE6WAePq0WnXzhLZz8vggwPkcn1ZGBv4"   # planilha 2 (leitura)

ABA_INPUT = "Campanha_Input"
ABA_PREVIA = "Campanha_Previa"
ABA_TITULOS = "Campanha_Titulos"
ABA_BOMBAS = "Campanha_Bombas"
ABA_LOG = "Campanha_Log"
ABA_TRIAGEM = "Triagem"
ABA_EMAIL_VEND = "Email_Vendedores"
ABA_BASE_CLIENTES = "Base_Clientes"     # planilha 2

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
         WHERE JSON_EXTRACT_SCALAR(x,'$.recebeEmailCartaCobranca') = 'S'
           AND REGEXP_CONTAINS(
                 LOWER(TRIM(COALESCE(JSON_EXTRACT_SCALAR(x,'$.email'),''))),
                 r'^[^@\\s,;]+@[^@\\s,;]+\\.[a-z]{{2,}}$')) AS n_destinatarios,
        ARRAY(
          SELECT DISTINCT LOWER(TRIM(JSON_EXTRACT_SCALAR(x,'$.email')))
          FROM UNNEST(JSON_EXTRACT_ARRAY(COALESCE(NULLIF(contatos_json,''),'[]'))) x
          WHERE JSON_EXTRACT_SCALAR(x,'$.recebeEmailCartaCobranca') = 'S'
            AND REGEXP_CONTAINS(
                  LOWER(TRIM(COALESCE(JSON_EXTRACT_SCALAR(x,'$.email'),''))),
                  r'^[^@\\s,;]+@[^@\\s,;]+\\.[a-z]{{2,}}$')
        ) AS emails
      FROM `{PROJECT_ID}.bronze.cigam__empresas`
      WHERE divisao.codigoDivisao IN ('10','11','12','90') AND codigo != '000679'
    ),
    t AS (
      SELECT codigoEmpresa, COUNT(*) AS n_titulos
      FROM `{PROJECT_ID}.silver.titulos_cigam`
      WHERE saldo > 0 AND COALESCE(codigoPortador,'') != 'X90'
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


def preparar(c, gc, bq) -> dict:
    """Lê a lista, resolve, calcula e CONGELA. Não envia nada."""
    p1 = io.abrir(gc, ID_DESTINO)
    p2 = io.abrir(gc, ID_VENDEDORES)

    entrada = [l.get("cliente") or l.get("cnpj") or l.get("codigo")
               for l in io.ler_aba(p1, ABA_INPUT)]
    entrada = [e for e in entrada if str(e or "").strip()]
    print(f"{len(entrada)} linha(s) em {ABA_INPUT}.")

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

    # --- vendedores (planilha 2, somente leitura)
    resv = vend.resolver_vendedores(
        base_clientes=io.ler_aba(p2, ABA_BASE_CLIENTES),
        email_vendedores=io.ler_aba(p1, ABA_EMAIL_VEND),
        cnpj_por_codigo={cod: cadastros[cod] for cod in codigos},
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
        if cod in resv.para_confirmar:
            triagem.append({"id_campanha": c.id_campanha, "codigo_cliente": cod,
                            "nome_cliente": r.nome_cliente, "caso": "vendedor inferido",
                            "detalhe": resv.para_confirmar[cod]["regra"]})
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
    io.escrever_aba(p1, ABA_TRIAGEM, pd.DataFrame(triagem))

    total = sum(p["total"] for p in previa)
    print(f"\nCONGELADO: {len(previa)} cliente(s), {len(df_tit)} título(s), "
          f"{len(df_bmb)} bomba(s), R$ {total:,.2f}.")
    print(f"{len(triagem)} caso(s) em {ABA_TRIAGEM}. Confira antes de disparar.")
    return {"previa": previa, "triagem": triagem}


# ------------------------------------------------------------------- fase B


def disparar(c, gc, enviar_fn=None) -> envio.Resultado:
    """Lê o congelado e envia. NUNCA consulta o BigQuery."""
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
        d[k] = float(str(d.get(k) or 0).replace(",", ".") or 0)
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
