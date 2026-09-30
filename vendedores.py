"""
Vendedor em cópia no aviso de bloqueio.

FONTE DA CARTEIRA: aba Base_Clientes, na PLANILHA 2.

Durante um tempo usei a coluna `vendedor` da Contatos_Emails, achando que o
cruzamento já estava resolvido lá. Estava errado: aquela coluna carrega o
campo `representante` do CIGAM, que é cadastro antigo e não carteira. Foi de
lá que saiu "MÁRIO LUCAS" — um colaborador de divisão 40 que não existe como
vendedor, preenchido no cadastro do Peter Ferter e propagado sem ninguém pedir.

A Base_Clientes é mantida pelo comercial e descreve a carteira vigente.
Reler dela não é refazer trabalho: é ler a fonte certa.

  Base_Clientes      (planilha 2) chave do cliente | vendedor
  Contatos_Emails    (planilha 1) codigo_cliente | nome_cliente | cnpj_cpf
  Email_Vendedores   (planilha 1) Vendedor | Email | Ativo | Gerente | Email_Gerente

JOIN SEMPRE POR codigo_cliente, NUNCA POR cnpj_cpf.
A coluna cnpj_cpf da Contatos_Emails virou número em algum ponto do caminho e
perdeu zeros à esquerda: os comprimentos vão de 1 a 14 dígitos. A ARMAC
(00.242.184/0001-04) está lá como 242184000104. E um valor de 11 dígitos pode
ser CPF completo ou CNPJ sem três zeros, sem como distinguir.
O codigo_cliente sobrevive: faixa 1..6505, zfill(6) reconstrói todos.

CASAMENTO DOS NOMES — medido na base real:
19 nomes distintos na Contatos_Emails, 17 casam com a Email_Vendedores. Os dois
que sobram são "NÃO MAPEADO" (14 clientes, não é pessoa) e "MÁRIO LUCAS"
(1 cliente, sem e-mail cadastrado).

O QUE SE PERDE COM ESSA SIMPLIFICAÇÃO
A Contatos_Emails não carrega a regra_atribuicao. Sem ela não dá para separar
vendedor identificado (inserção manual, CNPJ raiz, razão exata) de inferido
(substring de razão social, praça por cidade/UF). Todos entram em cópia igual.
Se um dia isso incomodar, é acrescentar a coluna na geração da Contatos_Emails.
"""

import re
import unicodedata
from dataclasses import dataclass, field

NAO_MAPEADO = "NAO MAPEADO"


def chave(texto) -> str:
    """Normaliza para comparação: sem acento, sem NBSP, sem caixa, espaço colapsado."""
    s = str(texto or "").replace("\u00a0", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


def normalizar_codigo(valor) -> str | None:
    """1868 -> '001868'. O Sheets devolve o código como número; sem o zfill
    nenhum join encontra nada."""
    d = re.sub(r"\D", "", str(valor or "").split(".")[0])
    return d.zfill(6) if d else None


def normalizar_emails(valor) -> list:
    """Uma célula pode ter vários endereços separados por ; ou , (conta grande
    cujo supervisor entra sempre em cópia). Também apara espaço sobrando, que
    a Email_Vendedores tem em várias linhas."""
    saida, vistos = [], set()
    for parte in re.split(r"[;,]", str(valor or "").replace("\u00a0", " ")):
        e = parte.strip().lower()
        if re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}", e) and e not in vistos:
            vistos.add(e)
            saida.append(e)
    return saida


_VAZIOS = {"", "nan", "none", "null", "<na>"}


def _campo(linha: dict, *nomes):
    """Lê o primeiro nome de coluna presente. Trata 'nan' como vazio: pandas
    devolve float NaN em célula em branco, e str(nan) == 'nan', uma string
    truthy que passaria como se fosse nome de vendedor."""
    for n in nomes:
        val = linha.get(n)
        if val is not None and str(val).strip().lower() not in _VAZIOS:
            return val
    return ""


@dataclass
class ResolucaoVendedores:
    em_copia: dict = field(default_factory=dict)          # codigo -> {vendedor, emails}
    bloqueados: dict = field(default_factory=dict)        # codigo -> motivo
    sem_email: dict = field(default_factory=dict)         # nome -> [codigos]
    fora_do_catalogo: dict = field(default_factory=dict)  # nome -> [codigos]

    def resumo(self) -> str:
        L = [f"{len(self.em_copia)} cliente(s) com vendedor em cópia."]
        if self.bloqueados:
            por_motivo = {}
            for cod, m in self.bloqueados.items():
                por_motivo.setdefault(m, []).append(cod)
            L.append(f"{len(self.bloqueados)} sem vendedor em cópia:")
            for m, cods in sorted(por_motivo.items()):
                L.append(f"   {m}: {len(cods)} ({', '.join(sorted(cods)[:5])}"
                         + (" ..." if len(cods) > 5 else "") + ")")
        for rotulo, d in (("sem linha na Email_Vendedores", self.fora_do_catalogo),
                          ("sem e-mail válido", self.sem_email)):
            if d:
                L.append(f"Vendedor {rotulo}:")
                for nome, cods in sorted(d.items()):
                    L.append(f"   {nome!r}: {len(cods)} cliente(s)")
        return "\n".join(L)


def _chaves_doc(valor) -> set:
    """Todas as grafias plausíveis de um CNPJ/CPF, para o join não depender
    de zero à esquerda.

    O Sheets come zeros dos dois lados: a Base_Clientes pode ter 7636657001241
    e a Contatos_Emails 07636657001241 para o mesmo cliente. Indexar por várias
    formas é mais barato que adivinhar qual sobreviveu.
    """
    d = re.sub(r"\D", "", str(valor or ""))
    if not d:
        return set()
    formas = {d}
    # CPF e CNPJ não se convertem um no outro. Um documento de 11 dígitos é
    # CPF completo: preenchê-lo até 14 gera "00022032365820", que não existe e
    # pode casar por acidente com outro registro.
    if len(d) <= 11:
        formas.add(d.zfill(11))          # CPF com zero comido
    if 11 < len(d) <= 14:
        formas.add(d.zfill(14))          # CNPJ com zero comido
    return formas


def carteira_de_base_clientes(base_clientes: list) -> dict:
    """{codigo_cliente: vendedor} a partir da Base_Clientes.

    Detecta as colunas em vez de exigir nomes fixos: a planilha é de outro
    processo e pode mudar sem aviso. Procura, nesta ordem, uma coluna de
    código de cliente e uma de CNPJ; o vendedor é qualquer coluna cujo nome
    contenha "vendedor", "representante" ou "carteira".

    Imprime o que encontrou. Se detectar errado, é melhor você ver na hora do
    que descobrir por um nome estranho no rodapé de um e-mail.
    """
    if not base_clientes:
        return {}

    cols = list(base_clientes[0])
    def achar(*termos):
        for c in cols:
            k = chave(c)
            if any(t in k for t in termos):
                return c
        return None

    col_cod = achar("CODIGOCLIENTE", "CODIGO", "COD")
    col_cnpj = achar("CNPJ", "CPF", "DOCUMENTO")
    col_vend = achar("VENDEDOR", "REPRESENTANTE", "CARTEIRA")

    if not col_vend:
        print(f"  Base_Clientes SEM coluna de vendedor. Colunas: "
              f"{', '.join(cols[:10])}")
        return {}

    carteira, por_cnpj = {}, {}
    for l in base_clientes:
        nome = str(l.get(col_vend) or "").replace("\u00a0", " ").strip()
        if not nome:
            continue
        cod = normalizar_codigo(l.get(col_cod)) if col_cod else None
        if cod:
            carteira[cod] = nome
        if col_cnpj:
            for k in _chaves_doc(l.get(col_cnpj)):
                por_cnpj[k] = nome

    print(f"  Base_Clientes: {len(base_clientes)} linha(s); vendedor em "
          f"'{col_vend}'"
          + (f", código em '{col_cod}'" if col_cod else "")
          + (f", CNPJ em '{col_cnpj}'" if col_cnpj else "")
          + f" -> {len(carteira)} por código, {len(por_cnpj)} por CNPJ")
    return {"por_codigo": carteira, "por_cnpj": por_cnpj,
            "vendedores": sorted({*carteira.values(), *por_cnpj.values()})}


def resolver_vendedores(contatos_emails: list,
                        email_vendedores: list,
                        codigos: list,
                        base_clientes: list = None) -> ResolucaoVendedores:
    """
    base_clientes:    aba Base_Clientes (planilha 2) — FONTE DA CARTEIRA
    contatos_emails:  aba Contatos_Emails (planilha 1) — só para o CNPJ
    email_vendedores: aba Email_Vendedores (planilha 1) — e-mail e gerente
    codigos:          códigos da campanha, já em zfill(6)

    Sem `base_clientes` cai na coluna `vendedor` da Contatos_Emails e AVISA.
    Aquela coluna é o `representante` do CIGAM, não a carteira.
    """
    r = ResolucaoVendedores()

    catalogo = {}
    for l in email_vendedores:
        nome = str(_campo(l, "vendedor", "Vendedor")).replace("\u00a0", " ").strip()
        if not nome:
            continue
        # Coluna Ativo é opcional; ausente ou vazia = ATIVO. Só desliga com um
        # "não" explícito — o contrário faria uma coluna esquecida zerar a
        # cópia da campanha inteira, sem erro.
        bruto = _campo(l, "ativo", "Ativo")
        ativo = (chave(bruto) not in ("FALSE", "FALSO", "N", "NAO", "0")
                 if str(bruto).strip() else True)
        catalogo[chave(nome)] = {"nome": nome,
                                 "emails": normalizar_emails(_campo(l, "email", "Email")),
                                 "ativo": ativo}

    vendedor_por_codigo = {}

    carteira = carteira_de_base_clientes(base_clientes or [])
    if carteira:
        vendedor_por_codigo.update(carteira["por_codigo"])
        # Completa pelo CNPJ quem a Base_Clientes não trouxe por código.
        if carteira["por_cnpj"]:
            for l in contatos_emails:
                cod = normalizar_codigo(_campo(l, "codigo_cliente"))
                if not cod or cod in vendedor_por_codigo:
                    continue
                # Mesmo normalizador dos dois lados. A versão anterior gerava
                # zfill(14) para CPF, o que produz documento inexistente e
                # pode casar por acidente.
                for k in _chaves_doc(_campo(l, "cnpj_cpf")):
                    if k in carteira["por_cnpj"]:
                        vendedor_por_codigo[cod] = carteira["por_cnpj"][k]
                        break
    else:
        print("  AVISO: sem Base_Clientes, usando a coluna `vendedor` da "
              "Contatos_Emails — ela é o `representante` do CIGAM, não a "
              "carteira do comercial.")

    for l in contatos_emails:
        cod = normalizar_codigo(_campo(l, "codigo_cliente"))
        nome = str(_campo(l, "vendedor")).replace("\u00a0", " ").strip()
        if cod and cod in vendedor_por_codigo:
            continue
        if cod:
            vendedor_por_codigo[cod] = nome

    for cod in codigos:
        nome = vendedor_por_codigo.get(cod)

        if nome is None:
            r.bloqueados[cod] = "código ausente da Contatos_Emails"
            continue
        if not nome or chave(nome) == NAO_MAPEADO:
            r.bloqueados[cod] = "sem vendedor atribuído"
            continue

        v = catalogo.get(chave(nome))
        if v is None:
            r.fora_do_catalogo.setdefault(nome, []).append(cod)
            r.bloqueados[cod] = "vendedor fora da Email_Vendedores"
            continue
        if not v["ativo"]:
            r.bloqueados[cod] = f"vendedor inativo ({v['nome']})"
            continue
        if not v["emails"]:
            r.sem_email.setdefault(v["nome"], []).append(cod)
            r.bloqueados[cod] = "vendedor sem e-mail válido"
            continue

        r.em_copia[cod] = {"vendedor": v["nome"], "emails": v["emails"]}

    return r


def aba_campanha_vendedores(res: ResolucaoVendedores, id_campanha: str) -> list:
    """Uma linha por vendedor: quem foi copiado em quê."""
    por_vendedor = {}
    for cod, i in res.em_copia.items():
        d = por_vendedor.setdefault(chave(i["vendedor"]),
                                    {"vendedor": i["vendedor"], "emails": i["emails"],
                                     "clientes": []})
        d["clientes"].append(cod)

    linhas = [{
        "id_campanha": id_campanha,
        "vendedor": d["vendedor"],
        "email": "; ".join(d["emails"]),
        "qtd_clientes": len(d["clientes"]),
        "codigos_clientes": ", ".join(sorted(d["clientes"])),
    } for d in por_vendedor.values()]
    return sorted(linhas, key=lambda x: (-x["qtd_clientes"], x["vendedor"]))


def conferir_catalogo(contatos_emails: list, email_vendedores: list) -> str:
    """Diagnóstico independente da campanha: quais nomes em uso não têm e-mail."""
    catalogo = {chave(_campo(l, "vendedor", "Vendedor")) for l in email_vendedores}
    uso = {}
    for l in contatos_emails:
        nome = str(_campo(l, "vendedor")).strip()
        if nome and chave(nome) != NAO_MAPEADO:
            uso.setdefault(chave(nome), [nome, 0])[1] += 1

    faltando = {k: v for k, v in uso.items() if k not in catalogo}
    L = [f"{len(uso)} vendedor(es) em uso, {len(uso) - len(faltando)} com e-mail."]
    if faltando:
        L.append("Sem e-mail na Email_Vendedores:")
        for nome, n in sorted(faltando.values(), key=lambda x: -x[1]):
            L.append(f"   {nome:<32} {n:>5} cliente(s)")
    sobrando = catalogo - set(uso) - {""}
    if sobrando:
        L.append(f"Na aba e sem cliente algum: {', '.join(sorted(sobrando))}")
    return "\n".join(L)
