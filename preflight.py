"""
Pré-voo: confere se a estrutura está de pé, sem enviar nada e sem escrever
em aba de campanha.

Ordem deliberada, do mais barato para o mais caro. A primeira falha para tudo:
não adianta validar SQL se a autenticação não passou.

    1. importa os módulos
    2. autentica
    3. abre as duas planilhas e confere as abas obrigatórias
    4. lê Campanha_Input, Email_Vendedores e Base_Clientes
    5. valida as três queries com dry_run do BigQuery (sintaxe e parâmetros,
       sem custo e sem ler linha)
    6. testa escrita e append numa aba de rascunho, que é apagada no fim

O passo 6 existe porque o caminho de escrita é o único que nunca foi exercitado
em nenhum teste — e é onde mora o resize=True, que já causou perda silenciosa.

    import preflight; preflight.rodar()
"""

from datetime import date

ABAS_P1 = ["Campanha_Input", "Email_Vendedores", "Contatos_Emails"]
ABA_RASCUNHO = "_preflight_apagar"

_ok, _falhas = [], []


def _passo(nome):
    print(f"\n── {nome}")


def _ok_(msg):
    _ok.append(msg)
    print(f"   ok    {msg}")


def _falha(msg):
    _falhas.append(msg)
    print(f"   FALHA {msg}")


def rodar(codigos_teste=("000337", "003106", "002786")) -> bool:
    global _ok, _falhas
    _ok, _falhas = [], []

    _passo("1. módulos")
    try:
        import agrupamento, campanha_v2, encargos, envio, io_sheets  # noqa
        import main, pipefy, template, vendedores                    # noqa
        _ok_("os 9 módulos importam")
    except Exception as e:
        _falha(f"import: {type(e).__name__}: {e}")
        return _resumo()

    import main, io_sheets as io, campanha_v2, encargos

    _passo("2. cálculo (não depende de rede)")
    e = encargos.calcular_encargos("317.43", 210)
    if str(e["encargos"]) == "29.45":
        _ok_("paridade com a planilha: 317,43 / 210 dias -> 29,45")
    else:
        _falha(f"encargos deu {e['encargos']}, esperado 29.45 — NÃO use este build")

    _passo("3. autenticação")
    try:
        gc, bq = main.conectar()
        _ok_("Google autenticado")
    except Exception as ex:
        _falha(f"conectar(): {type(ex).__name__}: {ex}")
        return _resumo()

    _passo("4. planilhas e abas")
    planilhas = {}
    for rotulo, sid, obrigatorias in (
        ("planilha 1 (destino)", main.ID_DESTINO, ABAS_P1),
    ):
        try:
            p = io.abrir(gc, sid)
            planilhas[rotulo] = p
            existentes = [w.title for w in p.worksheets()]
            _ok_(f"{rotulo}: '{p.title}'")
            for aba in obrigatorias:
                if aba in existentes:
                    _ok_(f"   aba '{aba}'")
                else:
                    _falha(f"   aba '{aba}' NÃO existe. Abas presentes: "
                           f"{', '.join(existentes[:8])}")
        except Exception as ex:
            _falha(f"{rotulo}: {type(ex).__name__}: {ex}")

    _passo("5. conteúdo das abas")
    p1 = planilhas.get("planilha 1 (destino)")

    if p1:
        entrada = io.ler_aba(p1, "Campanha_Input", obrigatoria=False)
        if not entrada:
            # Vazia é o estado NORMAL antes da régua rodar: quem preenche esta
            # aba é main.candidatos(). Não é falha, é o próximo passo.
            print("   —     Campanha_Input vazia. Rode main.candidatos() para a "
                  "régua calcular quem entra.")
        else:
            _ok_(f"Campanha_Input: {len(entrada)} linha(s)")
            cols = set(entrada[0])
            if not (cols & {"cliente", "cnpj", "codigo"}):
                _falha(f"   nenhuma coluna 'codigo'/'cliente'/'cnpj'. Tem: "
                       f"{', '.join(sorted(cols))}")

            # Duas origens, duas convenções:
            #   candidatos()    -> coluna `acao`,   EM BRANCO entra
            #   ingerir_lista() -> coluna `status`, só PRONTO entra
            from collections import Counter
            if "acao" in cols:
                vetados = sum(1 for l in entrada if str(l.get("acao","")).strip())
                _ok_(f"   régua: {len(entrada) - vetados} entram, {vetados} vetado(s)")
            elif "status" in cols:
                cont = Counter(str(l.get("status","")).strip().upper() for l in entrada)
                _ok_("   status: " + ", ".join(f"{v} {k}" for k, v in cont.most_common()))
            else:
                _falha("   sem coluna 'acao' nem 'status': a aba foi montada à mão "
                       "e TODAS as linhas entrariam, sem filtro de veto.")

        vend = io.ler_aba(p1, "Email_Vendedores", obrigatoria=False)
        import vendedores as V
        com_email = [l for l in vend if V.normalizar_emails(l.get("email"))]
        (_ok_ if com_email else _falha)(
            f"Email_Vendedores: {len(vend)} linha(s), {len(com_email)} com e-mail válido")

        contatos = io.ler_aba(p1, "Contatos_Emails", obrigatoria=False)
        if not contatos:
            _falha("Contatos_Emails vazia — é dela que sai o vendedor de cada cliente")
        else:
            cols = set(contatos[0])
            faltando = {"codigo_cliente", "vendedor"} - cols
            (_falha if faltando else _ok_)(
                f"Contatos_Emails: {len(contatos)} linha(s)"
                + (f" — FALTA coluna {faltando}" if faltando else ""))
            if not faltando:
                print("\n" + V.conferir_catalogo(contatos, vend))

    _passo("6. queries (dry_run: valida sem custo e sem ler linha)")
    from google.cloud import bigquery
    for arquivo, params in (
        ("titulos.sql", {"data_envio": date.today(), "lista_clientes": list(codigos_teste)}),
        ("bombas.sql", {"lista_clientes": list(codigos_teste)}),
    ):
        try:
            tipos = []
            for nome, valor in params.items():
                if isinstance(valor, list):
                    tipos.append(bigquery.ArrayQueryParameter(nome, "STRING", valor))
                else:
                    tipos.append(bigquery.ScalarQueryParameter(nome, "DATE", valor))
            sql = (main.SQL / arquivo).read_text()
            job = bq.query(sql, job_config=bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False, query_parameters=tipos))
            mb = (job.total_bytes_processed or 0) / 1e6
            _ok_(f"{arquivo}: válida, leria {mb:.1f} MB")
        except Exception as ex:
            _falha(f"{arquivo}: {type(ex).__name__}: {str(ex)[:160]}")

    try:
        n = list(bq.query(
            f"SELECT COUNT(*) AS n FROM `{main.PROJECT_ID}.bronze.cigam__empresas`"
        ).result())[0].n
        _ok_(f"BigQuery responde: {n} empresas no cadastro")
    except Exception as ex:
        _falha(f"BigQuery: {type(ex).__name__}: {ex}")

    _passo("7. escrita no Sheets (aba de rascunho, apagada no fim)")
    if p1:
        try:
            import pandas as pd
            grande = pd.DataFrame({"col_a": range(1500), "col_b": ["x"] * 1500})
            io.escrever_aba(p1, ABA_RASCUNHO, grande)
            ws = p1.worksheet(ABA_RASCUNHO)
            escritas = len(ws.col_values(1)) - 1
            (_ok_ if escritas == 1500 else _falha)(
                f"escreveu {escritas}/1500 linhas"
                + ("" if escritas == 1500 else " — resize=True não está funcionando"))

            io.append_log(p1, ABA_RASCUNHO + "_log", {"id_campanha": "PREFLIGHT",
                                                      "codigo_cliente": "000000",
                                                      "status": "TESTE"})
            _ok_("append de log funciona")
        except Exception as ex:
            _falha(f"escrita: {type(ex).__name__}: {ex}")
        finally:
            for nome in (ABA_RASCUNHO, ABA_RASCUNHO + "_log"):
                try:
                    p1.del_worksheet(p1.worksheet(nome))
                except Exception:
                    pass
            print("   (rascunhos apagados)")

    return _resumo()


def _resumo() -> bool:
    print("\n" + "═" * 62)
    if _falhas:
        print(f"{len(_falhas)} FALHA(S) — corrija antes de rodar preparar():")
        for f in _falhas:
            print(f"  · {f}")
        return False
    print(f"Estrutura ok ({len(_ok)} checagens). Pode rodar preparar().")
    return True
