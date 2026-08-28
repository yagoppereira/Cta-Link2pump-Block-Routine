"""
Resolve o vendedor de cada cliente da campanha e o e-mail dele.

DUAS PLANILHAS, PAPÉIS DIFERENTES — não confundir.

  PLANILHA 1 "Lista de Vendedores e Contatos CIGAM"  (ID_DESTINO, NOSSA)
    Contatos_Emails    saída da extração do BigQuery
    Email_Vendedores   Vendedor | Email | Ativo   <- mantida por nós
    Campanha_* e log   escritos pelo disparo

  PLANILHA 2 "Planilha Central Inadimplência por Carteira de Vendedor"
             (ID_VENDEDORES, DELES — somente leitura)
    Base_Clientes      saída do Apps Script consolidarBaseInteligente()
    Base_CIGAM, Base_Comercial, Insercoes_Manuais, Base_Cidades,
    Lista_Vendedores   insumos INTERNOS do Apps Script. Não são fonte nossa
                       de e-mail e não devem ser lidos por este módulo.

FONTES QUE ESTE MÓDULO USA

  Base_Clientes  (planilha 2)
    empresa_cnpj_cpf | vendedor | empresa | UF | Município | regra_atribuicao
    Uma linha por CNPJ (o script já deduplica por cnpjPuro).
    `vendedor` vem em CAIXA ALTA. Pode ser a string literal "NÃO MAPEADO".
    `empresa_cnpj_cpf` vem FORMATADO, como saiu do CIGAM -> limpar no join.

  Email_Vendedores  (planilha 1)
    Vendedor | Email | Ativo
    Uma célula de Email pode conter VÁRIOS endereços separados por ; ou ,
    (conta grande cujo supervisor entra sempre em cópia).

    A coluna Ativo é opcional e célula vazia significa ATIVO. Ela existe por
    um motivo específico: no Apps Script, as regras 2, 3, 3.1 e 4 checam
    eAtivo() antes de atribuir, mas a REGRA 1 (Inserção Manual) NÃO checa.
    Um nome antigo esquecido na aba Insercoes_Manuais sai classificado como
    regra 1 — a de maior confiança. Esta coluna é o único ponto onde esse
    caso pode ser barrado.
    A atribuição cliente->vendedor é do processo externo e NÃO é
    reimplementada aqui: cinco regras em cascata, normalização de cidade,
    raiz de CNPJ, substring com trava, retenção de inativo. Duas cópias da
    mesma regra divergem no dia em que alguém mexe só numa. Lemos a saída.
    O e-mail, sim, é nosso — mantido nesta aba, para não dependermos da
    estrutura interna da planilha deles.

A FRONTEIRA ENTRE AS DUAS PLANILHAS É O NOME DO VENDEDOR, escrito à mão nas
duas pontas. Se renomearem alguém na planilha 2, nossa aba fica velha e aqueles
clientes perdem a cópia sem erro nenhum. Por isso a aba nasce de
bootstrap_emails_vendedores(), que extrai os nomes como eles REALMENTE existem
na Base_Clientes, e é reconciliada a cada rodada.

CONFIANÇA DA ATRIBUIÇÃO

O Apps Script resolve o vendedor por uma cascata de 5 regras que NÃO têm o
mesmo valor probatório. Num relatório interno, um chute razoável ajuda. Num
e-mail onde o vendedor é copiado no extrato de dívida do cliente, um chute
errado manda dado financeiro do cliente A para o vendedor do cliente B.

  IDENTIFICADO  regras 1, 2, 3   inserção manual, CNPJ raiz, razão exata
  INFERIDO      regras 3.1, 4    substring de razão social, praça (cidade/UF)
  INATIVO       regra 5          vendedor retido que está desligado
  AUSENTE       "Nenhuma"        NÃO MAPEADO

Por padrão só IDENTIFICADO entra em cópia. INFERIDO sai em relatório para
confirmação manual; INATIVO e AUSENTE nunca entram. Para liberar os inferidos,
passe incluir_inferidos=True — decisão de negócio, não default técnico.
"""

import re
import unicodedata
from dataclasses import dataclass, field

NAO_MAPEADO = "NAO MAPEADO"

IDENTIFICADO = "identificado"
INFERIDO = "inferido"
INATIVO = "inativo"
AUSENTE = "ausente"


def chave(texto) -> str:
    """Normaliza para comparação: sem acento, sem NBSP, sem caixa, espaço colapsado."""
    s = str(texto or "").replace("\u00a0", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


def so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def normalizar_emails(valor) -> list:
    """Uma célula pode ter vários endereços separados por ; ou ,.

    Devolve a lista de endereços válidos, deduplicada, na ordem original.
    Célula vazia ou só com lixo devolve lista vazia.
    """
    bruto = str(valor or "").replace("\u00a0", " ")
    saida, vistos = [], set()
    for parte in re.split(r"[;,]", bruto):
        e = parte.strip().lower()
        if re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}", e) and e not in vistos:
            vistos.add(e)
            saida.append(e)
    return saida


def classificar_regra(regra) -> str:
    """Traduz a regra_atribuicao do Apps Script em nível de confiança."""
    r = chave(regra)
    if r.startswith("5."):
        return INATIVO
    if "PARCIAL" in r or r.startswith("4."):
        return INFERIDO
    if r.startswith(("1.", "2.", "3.")):
        return IDENTIFICADO
    return AUSENTE


@dataclass
class ResolucaoVendedores:
    em_copia: dict = field(default_factory=dict)        # codigo -> {vendedor, email, regra}
    para_confirmar: dict = field(default_factory=dict)  # codigo -> {vendedor, email, regra}
    bloqueados: dict = field(default_factory=dict)      # codigo -> motivo
    vendedor_sem_email: dict = field(default_factory=dict)   # nome -> [codigos]
    vendedor_desconhecido: dict = field(default_factory=dict)  # nome -> [codigos]

    def resumo(self) -> str:
        L = [f"{len(self.em_copia)} cliente(s) com vendedor em cópia (atribuição identificada)."]
        if self.para_confirmar:
            L.append(f"{len(self.para_confirmar)} com atribuição INFERIDA — confirmar antes de copiar:")
            for cod, i in sorted(self.para_confirmar.items())[:10]:
                L.append(f"   {cod}: {i['vendedor']} ({i['regra']})")
        if self.bloqueados:
            por_motivo = {}
            for cod, m in self.bloqueados.items():
                por_motivo.setdefault(m, []).append(cod)
            L.append(f"{len(self.bloqueados)} sem vendedor em cópia:")
            for m, cods in sorted(por_motivo.items()):
                L.append(f"   {m}: {len(cods)} ({', '.join(sorted(cods)[:5])}"
                         + (" ..." if len(cods) > 5 else "") + ")")
        if self.vendedor_desconhecido:
            L.append("Nome na Base_Clientes (planilha 2) sem linha na Email_Vendedores:")
            for n, c in sorted(self.vendedor_desconhecido.items()):
                L.append(f"   {n!r}: {len(c)} cliente(s)")
        if self.vendedor_sem_email:
            L.append("Vendedor na Email_Vendedores sem e-mail válido:")
            for n, c in sorted(self.vendedor_sem_email.items()):
                L.append(f"   {n!r}: {len(c)} cliente(s)")
        return "\n".join(L)


def resolver_vendedores(base_clientes: list,
                        email_vendedores: list,
                        cnpj_por_codigo: dict,
                        incluir_inferidos: bool = False) -> ResolucaoVendedores:
    """
    base_clientes:     [{'empresa_cnpj_cpf','vendedor','regra_atribuicao'}, ...]
    email_vendedores:  [{'Vendedor','Email','Ativo'}, ...] da planilha 1
    cnpj_por_codigo:   {codigo_cliente: cnpj_limpo} dos clientes DA CAMPANHA
    """
    r = ResolucaoVendedores()

    catalogo = {}
    for l in email_vendedores:
        nome = str(l.get("vendedor") or "").replace("\u00a0", " ").strip()
        if not nome:
            continue
        # Ausência ou célula vazia = ATIVO. Só desliga com um "não" explícito:
        # se fosse o contrário, uma coluna esquecida zeraria a cópia da
        # campanha inteira sem levantar erro.
        bruto_ativo = l.get("ativo", l.get("Ativo", ""))
        if str(bruto_ativo).strip() != "":
            ativo = chave(bruto_ativo) not in ("FALSE", "FALSO", "N", "NAO", "0")
        else:
            ativo = True
        catalogo[chave(nome)] = {
            "nome": nome,
            "emails": normalizar_emails(l.get("email") or l.get("Email")),
            "ativo": ativo,
        }

    atrib = {}
    for l in base_clientes:
        cnpj = so_digitos(l.get("empresa_cnpj_cpf"))
        nome = str(l.get("vendedor") or "").replace("\u00a0", " ").strip()
        if cnpj and nome:
            atrib[cnpj] = (nome, l.get("regra_atribuicao"))

    for codigo, cnpj in cnpj_por_codigo.items():
        item = atrib.get(cnpj)
        if item is None:
            r.bloqueados[codigo] = "CNPJ ausente da Base_Clientes"
            continue

        nome, regra = item
        nivel = classificar_regra(regra)

        if chave(nome) == NAO_MAPEADO or nivel == AUSENTE:
            r.bloqueados[codigo] = "sem vendedor atribuído"
            continue
        if nivel == INATIVO:
            r.bloqueados[codigo] = f"vendedor inativo ({nome})"
            continue

        v = catalogo.get(chave(nome))
        if v is None:
            r.vendedor_desconhecido.setdefault(nome, []).append(codigo)
            r.bloqueados[codigo] = "vendedor fora da Email_Vendedores"
            continue
        if not v["ativo"]:
            r.bloqueados[codigo] = f"marcado inativo na Email_Vendedores ({nome})"
            continue
        if not v["emails"]:
            r.vendedor_sem_email.setdefault(v["nome"], []).append(codigo)
            r.bloqueados[codigo] = "vendedor sem e-mail válido"
            continue

        destino = {"vendedor": v["nome"], "emails": v["emails"], "regra": str(regra)}
        if nivel == IDENTIFICADO or incluir_inferidos:
            r.em_copia[codigo] = destino
        else:
            r.para_confirmar[codigo] = destino

    return r


def aba_campanha_vendedores(res: ResolucaoVendedores, id_campanha: str) -> list:
    """Linhas da aba paralela: uma por vendedor, com os clientes dele."""
    por_vendedor = {}
    for codigo, i in res.em_copia.items():
        d = por_vendedor.setdefault(
            chave(i["vendedor"]),
            {"vendedor": i["vendedor"], "emails": i["emails"], "clientes": []},
        )
        d["clientes"].append(codigo)

    linhas = []
    for d in por_vendedor.values():
        cl = sorted(d["clientes"])
        linhas.append({
            "id_campanha": id_campanha,
            "vendedor": d["vendedor"],
            "email": "; ".join(d["emails"]),
            "qtd_destinatarios": len(d["emails"]),
            "qtd_clientes": len(cl),
            "codigos_clientes": ", ".join(cl),
        })
    return sorted(linhas, key=lambda x: (-x["qtd_clientes"], x["vendedor"]))


def nomes_em_uso(base_clientes: list) -> dict:
    """Nomes de vendedor que realmente aparecem na Base_Clientes externa.

    Chave normalizada -> {nome exibido, qtd de clientes, regras vistas}.
    Ignora "NÃO MAPEADO" e linhas sem regra utilizável.
    """
    uso = {}
    for l in base_clientes:
        nome = str(l.get("vendedor") or "").replace("\u00a0", " ").strip()
        k = chave(nome)
        if not k or k == NAO_MAPEADO:
            continue
        d = uso.setdefault(k, {"nome": nome, "qtd": 0, "regras": set()})
        d["qtd"] += 1
        d["regras"].add(classificar_regra(l.get("regra_atribuicao")))
    return uso


def bootstrap_emails_vendedores(base_clientes: list, aba_atual: list) -> tuple:
    """Monta as linhas da aba Emails_Vendedores sem perder o que já foi digitado.

    Devolve (linhas, novos, orfaos):
      linhas -> o conteúdo completo da aba, ordenado por carteira
      novos  -> nomes que apareceram na Base_Clientes e ainda não tinham linha
      orfaos -> nomes na nossa aba que não existem mais na Base_Clientes
                (vendedor renomeado ou desligado — CONFERIR, não apagar)
    """
    existente = {}
    for l in aba_atual:
        nome = str(l.get("nome") or l.get("vendedor") or "").strip()
        if nome:
            existente[chave(nome)] = l

    uso = nomes_em_uso(base_clientes)

    linhas, novos = [], []
    for k, d in uso.items():
        anterior = existente.get(k)
        if anterior is None:
            novos.append(d["nome"])
        linhas.append({
            "nome": d["nome"],
            "email": (anterior or {}).get("email", ""),
            "ativo": (anterior or {}).get("ativo", "S"),
            "qtd_clientes": d["qtd"],
            "confianca": ", ".join(sorted(d["regras"])),
        })

    orfaos = [existente[k].get("nome") or existente[k].get("vendedor")
              for k in existente.keys() - uso.keys()]

    linhas.sort(key=lambda x: (-x["qtd_clientes"], chave(x["nome"])))
    return linhas, sorted(novos), sorted(o for o in orfaos if o)


if __name__ == "__main__":
    res = resolver_vendedores(
        base_clientes=[
            {"empresa_cnpj_cpf": "08.077.872/0003-21", "vendedor": "VIVIAN NORBERTO",
             "regra_atribuicao": "2. Comercial (CNPJ Raiz)"},
            {"empresa_cnpj_cpf": "11.222.333/0001-44", "vendedor": "GUILHERME PIO PIMENTA JUNIOR",
             "regra_atribuicao": "4. Atribuição por Praça (Cidade/UF)"},
            # Regra 1 NÃO passa por eAtivo() no Apps Script: nome antigo
            # esquecido na Insercoes_Manuais chega aqui como alta confiança.
            {"empresa_cnpj_cpf": "55.666.777/0001-88", "vendedor": "CARLOS ANTIGO",
             "regra_atribuicao": "1. Inserção Manual (Exata)"},
            {"empresa_cnpj_cpf": "99.888.777/0001-66", "vendedor": "NÃO MAPEADO",
             "regra_atribuicao": "Nenhuma"},
            {"empresa_cnpj_cpf": "12.345.678/0001-99", "vendedor": "DAVI BONATTO DIAZ",
             "regra_atribuicao": "3. Comercial (Razão Social Parcial)"},
            {"empresa_cnpj_cpf": "33.444.555/0001-77", "vendedor": "MARLON SILVA",
             "regra_atribuicao": "1. Inserção Manual (Exata)"},
        ],
        email_vendedores=[
            # Coluna Ativo vazia = ativo, como na aba real hoje
            {"vendedor": "Vivian  Norberto", "email": "vivian@empresa.com"},
            {"vendedor": "GUILHERME PIO PIMENTA JUNIOR", "email": "guilherme@empresa.com"},
            {"vendedor": "CARLOS ANTIGO", "email": "carlos@empresa.com", "ativo": "N"},
            {"vendedor": "DAVI BONATTO DIAZ", "email": ""},
            {"vendedor": "MARLON SILVA",
             "email": "marlon.silva@ctasmart.com.br; carlos.souza@ctasmart.com.br"},
        ],
        cnpj_por_codigo={
            "000479": "08077872000321",
            "000123": "11222333000144",
            "000456": "55666777000188",
            "000789": "99888777000166",
            "000999": "12345678000199",
            "001111": "33444555000177",
        },
    )
    print(res.resumo())
    print()
    for l in aba_campanha_vendedores(res, "2026-09-A"):
        print(f"  {l['vendedor']:<26} {l['email']:<56} "
              f"{l['qtd_clientes']} cliente(s) -> {l['codigos_clientes']}")
