"""
Quem recebe UM e-mail, e se o bloqueio de sistema é seguro.

REGRA DE AGRUPAMENTO
Dois cadastros CIGAM notificados que caem no mesmo `cliente_id` do sistema CTA
pertencem ao mesmo usuário: recebem um e-mail só, com a dívida somada.
Cadastros em sistemas diferentes recebem e-mails separados.

POR QUE COMPONENTE CONEXA E NÃO AGRUPAMENTO SIMPLES
Um pagante pode ter bombas em vários `cliente_id` — na base há quem esteja em
16. Se A divide o sistema X com B, e divide o sistema Y com C, então A, B e C
são o mesmo usuário na prática. Agrupando só por sistema, A cairia em dois
grupos e receberia dois avisos, que é o problema que o agrupamento existe para
resolver. Union-find sobre o grafo bipartido cadastro <-> sistema.

CADASTRO SEM BOMBA
Sem bomba não há `cliente_id`, e sem `cliente_id` não há evidência forte de
"mesmo usuário". Esses caem em uma regra secundária: raiz de CNPJ. É evidência
mais fraca — raiz igual prova mesma matriz, não mesmo acesso ao sistema — mas
sem ela um grupo como a Alvoar, que tem três cadastros e só dois com bomba,
continuaria mandando dois avisos para a mesma pessoa. Desligue com
fallback_raiz_cnpj=False se preferir só a evidência forte.

SÓ ENTRAM OS CADASTROS DA LISTA
Um sistema pode conter pagantes que não estão sendo notificados. O e-mail do
Aço Verde não pode falar das bombas da CBF Indústria de Gusa.

TRAVA DA RAIZ DE CNPJ
Compartilhar `cliente_id` NÃO prova mesmo dono: o sistema 69288523 tem 18
pagantes, incluindo empresa sem relação nenhuma com as outras. Se dois
cadastros de empresas DIFERENTES estivessem na lista e dividissem um sistema,
fundir os dois mandaria a dívida de uma para a outra. Por isso a fusão exige
sistema em comum E raiz de CNPJ igual. A trava é implementada no nó do
union-find: o cadastro se liga a (sistema, raiz), não a (sistema).

CONFLITO DE BLOQUEIO
Quando o sistema tem bomba de pagante adimplente, bloquear o `cliente_id`
derruba quem está em dia. Medido: 241 dos 1.867 sistemas têm mais de um
pagante (um deles tem 35), somando 1.992 bombas — um terço da base. Nesses
casos o pedido ao suporte é do SERIAL, nunca do sistema.
"""

from dataclasses import dataclass, field


class _UnionFind:
    def __init__(self):
        self.pai = {}

    def achar(self, x):
        self.pai.setdefault(x, x)
        raiz = x
        while self.pai[raiz] != raiz:
            raiz = self.pai[raiz]
        while self.pai[x] != raiz:      # compressão de caminho
            self.pai[x], x = raiz, self.pai[x]
        return raiz

    def unir(self, a, b):
        ra, rb = self.achar(a), self.achar(b)
        if ra != rb:
            self.pai[rb] = ra


@dataclass
class Grupo:
    codigos: list = field(default_factory=list)        # cadastros CIGAM
    cliente_ids: list = field(default_factory=list)    # sistemas CTA
    seriais: list = field(default_factory=list)
    bombas_de_terceiros: int = 0        # no(s) sistema(s), de outro pagante
    bombas_de_adimplentes: int = 0      # subconjunto: pagante que está em dia
    terceiros_adimplentes: list = field(default_factory=list)

    @property
    def bloqueio_de_sistema_seguro(self) -> bool:
        """Falso quando cortar o cliente_id derrubaria quem está em dia."""
        return self.bombas_de_adimplentes == 0

    @property
    def chave(self) -> str:
        return "+".join(self.codigos)


def agrupar(codigos: list, bombas: list,
            raiz_por_codigo: dict,
            conflito_por_sistema: dict | None = None,
            fallback_raiz_cnpj: bool = True) -> list:
    """
    codigos: cadastros da campanha (os que vão receber aviso)
    bombas:  snapshot, com codigo_cliente, cliente_id, serial_equipamento
    raiz_por_codigo: {codigo: 8 primeiros dígitos do CNPJ}. OBRIGATÓRIO — é a
             trava que impede fundir empresas diferentes que dividem sistema.
    conflito_por_sistema: {cliente_id: {'bombas_de_terceiros': n,
                                        'bombas_de_adimplentes': n,
                                        'adimplentes': [nomes]}}
             vem da bombas.sql; sem ele nenhum conflito é detectado e todo
             bloqueio parece seguro — pior que não agrupar.
    fallback_raiz_cnpj: liga a regra secundária para cadastro SEM bomba.
    """
    conflito_por_sistema = conflito_por_sistema or {}
    na_lista = set(codigos)
    uf = _UnionFind()

    def _raiz(cod) -> str:
        return str(raiz_por_codigo.get(cod) or "")[:8]

    for cod in codigos:
        uf.achar(("c", cod))

    com_bomba = set()
    for b in bombas:
        cod = b.get("codigo_cliente")
        sistema = b.get("cliente_id")
        if cod in na_lista and sistema:
            com_bomba.add(cod)
            # O nó é (sistema, raiz): dois cadastros só se encontram quando
            # dividem o sistema E são da mesma empresa.
            raiz = _raiz(cod) or f"__sem_raiz_{cod}"
            uf.unir(("c", cod), ("s", str(sistema), raiz))

    # Regra secundária: cadastro SEM bomba não tem cliente_id, logo não tem
    # evidência forte de "mesmo usuário". Entra pela raiz de CNPJ, e SÓ se a
    # raiz já estiver ancorada em alguém que tem bomba. Nunca cria grupo novo
    # por raiz — isso juntaria cadastros que só compartilham matriz, sem prova
    # de que dividem acesso.
    if fallback_raiz_cnpj:
        ancora: dict = {}
        for cod in sorted(com_bomba):
            if _raiz(cod):
                ancora.setdefault(_raiz(cod), cod)
        for cod in codigos:
            if cod not in com_bomba and _raiz(cod) in ancora:
                uf.unir(("c", ancora[_raiz(cod)]), ("c", cod))

    grupos: dict = {}
    for cod in codigos:
        grupos.setdefault(uf.achar(("c", cod)), Grupo()).codigos.append(cod)

    for b in bombas:
        cod = b.get("codigo_cliente")
        if cod not in na_lista:
            continue
        g = grupos[uf.achar(("c", cod))]
        sistema = str(b.get("cliente_id") or "")
        if sistema and sistema not in g.cliente_ids:
            g.cliente_ids.append(sistema)
        serial = b.get("serial_equipamento")
        if serial is not None and serial not in g.seriais:
            g.seriais.append(serial)

    for g in grupos.values():
        g.codigos.sort()
        g.cliente_ids.sort()
        g.seriais.sort()
        nomes = []
        for sistema in g.cliente_ids:
            c = conflito_por_sistema.get(sistema) or {}
            g.bombas_de_terceiros += int(c.get("bombas_de_terceiros") or 0)
            g.bombas_de_adimplentes += int(c.get("bombas_de_adimplentes") or 0)
            nomes += list(c.get("adimplentes") or [])
        g.terceiros_adimplentes = sorted(set(n for n in nomes if n))

    return sorted(grupos.values(), key=lambda g: (-len(g.codigos), g.chave))


def resumo(grupos: list) -> str:
    juntos = [g for g in grupos if len(g.codigos) > 1]
    inseguros = [g for g in grupos if not g.bloqueio_de_sistema_seguro]
    L = [f"{len(grupos)} e-mail(s) para {sum(len(g.codigos) for g in grupos)} cadastro(s)."]
    if juntos:
        L.append(f"{len(juntos)} grupo(s) com mais de um cadastro (mesmo usuário no sistema):")
        for g in juntos:
            L.append(f"   {g.chave}  ->  sistema(s) {', '.join(g.cliente_ids)}")
    if inseguros:
        L.append(f"{len(inseguros)} com BLOQUEIO DE SISTEMA INSEGURO "
                 f"— pedir bloqueio dos seriais, não do sistema:")
        for g in inseguros:
            L.append(f"   {g.chave}: {g.bombas_de_adimplentes} bomba(s) de adimplente "
                     f"no sistema ({', '.join(g.terceiros_adimplentes[:2])}"
                     + (" ..." if len(g.terceiros_adimplentes) > 2 else "") + ")")
    return "\n".join(L)
