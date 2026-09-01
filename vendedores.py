"""
Vendedor em cópia no aviso de bloqueio.

FONTE ÚNICA: PLANILHA 1. A planilha 2 saiu do circuito.
O cruzamento cliente <-> vendedor já foi resolvido quando a Contatos_Emails
foi gerada; reler a Base_Clientes seria refazer um trabalho já feito, por um
caminho mais frágil.

  Contatos_Emails    codigo_cliente | nome_cliente | cnpj_cpf | ... | vendedor
  Email_Vendedores   Vendedor | Email | Ativo (opcional)

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


def resolver_vendedores(contatos_emails: list,
                        email_vendedores: list,
                        codigos: list) -> ResolucaoVendedores:
    """
    contatos_emails:  aba Contatos_Emails (planilha 1)
    email_vendedores: aba Email_Vendedores (planilha 1)
    codigos:          códigos da campanha, já em zfill(6)
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
    for l in contatos_emails:
        cod = normalizar_codigo(_campo(l, "codigo_cliente"))
        nome = str(_campo(l, "vendedor")).replace("\u00a0", " ").strip()
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
