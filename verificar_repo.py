"""
Confere se o repositório clonado bate com o que deveria estar lá.

Existe porque a minha cópia e o GitHub já divergiram duas vezes sem ninguém
perceber: o `backfill_registro` e o `abrir_campanha` sumiram de um dos lados e
a mensagem de erro apontava para outra coisa. `py_compile` não pega isso, e
"module has no attribute" para uma função que está no disco é confuso o
bastante para custar meia hora.

Rode no Colab depois do clone:

    import verificar_repo; verificar_repo.rodar()
"""

import ast
import importlib
import pathlib
import sys

# Funções que o notebook chama. Se alguma sumir do repositório, o notebook
# quebra numa célula do meio, geralmente depois de já ter feito trabalho.
ESPERADAS = {
    "main": ["conectar", "nova_campanha", "abrir_campanha", "backfill_registro",
             "migrar_datas_previa", "criar_painel_campanhas", "candidatos",
             "preparar", "previa_email", "disparar", "atualizar_painel",
             "reconciliar", "auditar_titulos", "gerar_cards",
             "resumo_por_gerente", "enviar_resumo_gestores", "resumo_campanhas",
             "registrar"],
    "preflight": ["rodar"],
    "envio": ["criar_enviador_smtp", "disparar"],
    "template": ["montar_email", "montar_email_gestor", "montar_quadro",
                 "quadro_equipamentos", "limpar_nome_bomba"],
    "painel": ["montar", "escrever", "calcular_etapa"],
    "acordos": ["carregar", "aplicar"],
    "vendedores": ["resolver_vendedores", "carteira_de_base_clientes"],
    "pipefy": ["montar_card", "formatar_documento"],
    "nfse": ["carregar", "aplicar"],
    "io_sheets": ["abrir", "ler_aba", "escrever_aba", "append_log",
                  "chaves_ja_enviadas", "ler_aba_procurando_cabecalho"],
}

# Tabelas que NÃO devem mais aparecer. Duas razões distintas:
#
#   silver.lancamentos_enriquecidos   parou de atualizar em 30/09/2026
#   bronze.cigam__lancamentos         idem
#   silver_pier.*                     existe e atualiza, mas é o schema
#                                     restrito, com contas a pagar e despesa.
#                                     Para cobrança a fonte é
#                                     silver.lancamentos_receber, que tem o
#                                     mesmo dado no recorte que usamos e não
#                                     depende de um acesso criado para ser
#                                     revogável.
SCHEMA_MORTO = ("silver.lancamentos_enriquecidos", "bronze.cigam__lancamentos",
                "silver_pier.", "bronze_pier.")

ARQUIVOS_SQL = ["base_inadimplencia.sql", "titulos.sql", "bombas.sql",
                "query_contatos.sql"]


def _sombras_de_laco(arvore) -> list:
    """Atribuição que destrói o ITERÁVEL de um for em andamento.

    O caso que motivou: dentro de `for l in itens:` havia `itens = [...]` para
    montar a lista de equipamentos. O laço terminava na primeira volta e o
    `len(itens)` do título passava a contar equipamentos — o relatório dizia
    "2 cliente(s)" e mostrava um. Não é erro de sintaxe nem de import; só
    aparece no resultado, e só se alguém conferir.

    Ignora compreensões, que têm escopo próprio, e a reatribuição da própria
    variável do laço, que é idioma comum e inofensivo.
    """
    achados = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.For):
            continue
        iteravel = {n.id for n in ast.walk(no.iter) if isinstance(n, ast.Name)}
        for filho in no.body:
            for n in ast.walk(filho):
                if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp,
                                  ast.GeneratorExp)):
                    continue
                if (isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id in iteravel
                                for t in n.targets)):
                    alvo = next(t.id for t in n.targets
                                if isinstance(t, ast.Name) and t.id in iteravel)
                    achados.append(
                        f"linha {n.lineno}: '{alvo}' é o iterável do for da "
                        f"linha {no.lineno} e é reatribuído dentro dele")
    return achados


def rodar(dir_repo: str = None) -> bool:
    raiz = pathlib.Path(dir_repo or pathlib.Path(__file__).parent)
    falhas = []

    print(f"── arquivos em {raiz}")
    py = sorted(p.stem for p in raiz.glob("*.py") if p.stem != "verificar_repo")
    sql = sorted(p.name for p in raiz.glob("*.sql"))
    print(f"   {len(py)} módulo(s): {', '.join(py)}")
    print(f"   {len(sql)} query(ies): {', '.join(sql)}")
    for nome in ARQUIVOS_SQL:
        if nome not in sql:
            falhas.append(f"falta a query {nome}")

    print("\n── sintaxe")
    for p in sorted(raiz.glob("*.py")):
        try:
            ast.parse(p.read_text())
        except SyntaxError as e:
            falhas.append(f"{p.name}: sintaxe linha {e.lineno} — {e.msg}")
    print(f"   {len(py)} arquivo(s) verificado(s)")

    print("\n── laços")
    n_sombra = 0
    for p in sorted(raiz.glob("*.py")):
        if p.name == "verificar_repo.py":
            continue
        for a in _sombras_de_laco(ast.parse(p.read_text())):
            falhas.append(f"{p.name} {a}")
            n_sombra += 1
    print(f"   {'nenhum iterável sobrescrito' if not n_sombra else 'VER FALHAS'}")

    print("\n── funções esperadas")
    if str(raiz) not in sys.path:
        sys.path.insert(0, str(raiz))
    for mod, fns in ESPERADAS.items():
        try:
            m = importlib.import_module(mod)
            importlib.reload(m)
        except ModuleNotFoundError as e:
            # gspread/bigquery não instalados é ambiente, não repositório.
            print(f"   —     {mod}: dependência ausente ({e.name}); "
                  f"conferi só a sintaxe")
            continue
        except Exception as e:
            falhas.append(f"{mod}: não importa — {type(e).__name__}: {e}")
            continue
        faltam = [f for f in fns if not hasattr(m, f)]
        if faltam:
            falhas.append(f"{mod}: faltam {', '.join(faltam)}")
            print(f"   FALTA {mod}: {', '.join(faltam)}")
        else:
            print(f"   ok    {mod} ({len(fns)})")

    print("\n── schema do DW")
    for p in list(raiz.glob("*.py")) + list(raiz.glob("*.sql")):
        if p.name == "verificar_repo.py":      # a lista mora aqui
            continue
        txt = p.read_text()
        for morto in SCHEMA_MORTO:
            if morto in txt:
                falhas.append(
                    f"{p.name} usa {morto} — use silver.lancamentos_receber")
    print("   nenhuma referência a schema morto" if not any(
        "parou de atualizar" in f for f in falhas) else "   VER FALHAS")

    print("\n" + "=" * 62)
    if falhas:
        print(f"{len(falhas)} PROBLEMA(S):")
        for f in falhas:
            print(f"  · {f}")
        return False
    print("repositório consistente")
    return True


if __name__ == "__main__":
    sys.exit(0 if rodar() else 1)
