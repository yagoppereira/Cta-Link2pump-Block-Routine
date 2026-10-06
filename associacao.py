"""
Associa TÍTULO VENCIDO a EQUIPAMENTO, por aproximação declarada.

Não existe chave. O contrato não guarda o serial: a descrição é genérica
("LICENCIAMENTO DE SOFTWARE") e o vínculo real vive no texto livre da
observação, em formatos que convivem na mesma base.

Então a regra PONTUA sinais e devolve o grau de confiança junto, em vez de
afirmar. Um palpite rotulado como palpite serve; um palpite apresentado como
fato faz o vendedor dizer ao cliente que vai bloquear a bomba errada.

OS SINAIS, na ordem de força observada nos dados reais:

  1. CARDINALIDADE  cliente com 1 equipamento -> é aquele, sem dúvida.
                    142 clientes inadimplentes estão nesse caso.

  2. CIDADE         a observação traz "Itatiaiuçu, MG" e o nome da bomba
                    costuma trazer a mesma cidade. Casou em 99 de 510 pares
                    testados — bom quando casa, silencioso quando não.

  3. PRODUTO        "Mobile Simples", "Pedestal Duplo" aparecem na observação
                    e às vezes no nome da bomba. Sozinho não distingue dois
                    pedestais do mesmo cliente; combinado com cidade, sim.

  4. PAREAMENTO     contratos saem em par (licenciamento R$ 720 + aluguel
                    R$ 180) com a MESMA observação e códigos vizinhos
                    (6471/6472). Serve para agrupar contratos do mesmo ponto,
                    não para achar a bomba — mas reduz a lista pela metade.

O que a regra NUNCA faz: escolher entre dois equipamentos igualmente
plausíveis. Nesse caso devolve os dois e diz que não sabe.
"""

import re
import unicodedata

CONFIANCA = ("inequívoca", "alta", "média", "baixa", "nenhuma")


def _chave(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9 ]", " ", t.upper())


RUIDO = {"CTA", "MOB", "MOBILE", "PED", "ED", "PEDESTAL", "SONDA", "COMBOIO",
         "SIMPLES", "DUPLO", "TRIPLO", "CIDADE", "INSTALACAO", "INST",
         "PRODUTO", "A", "DE", "DA", "DO", "LOCAL", "UNIDADE", "FILIAL"}


def cidade_de(texto) -> str | None:
    """'CTA Pedestal Simples - Itatiaiuçu, MG - VIROU MEDIÇÃO' -> 'ITATIAIUCU'.

    O nome da cidade é o que sobra depois de tirar produto e rótulo, e antes
    da UF. Remove as palavras de ruído em LAÇO, não uma vez: "Pedestal Simples
    Itatiaiuçu" precisa perder duas antes de chegar na cidade, e a primeira
    versão parava na primeira.

    Preposições no meio ficam ("Canaã Dos Carajás"); só as do começo saem.
    """
    t = _chave(texto)
    # \b no começo: sem ele o regex começava no meio da palavra e devolvia
    # "EDESTAL DUPLO SANTA MARIA" em vez de "SANTA MARIA".
    m = re.search(r"\b([A-Z][A-Z ]{2,40}?)\s*[, ]\s*(AC|AL|AP|AM|BA|CE|DF|ES|"
                  r"GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)"
                  r"\b", t)
    if not m:
        return None

    palavras = [p for p in m.group(1).split() if p]
    while palavras and palavras[0] in RUIDO:
        palavras.pop(0)
    # sobra só ruído: não havia cidade, era descrição de produto
    if not palavras:
        return None
    return " ".join(palavras)


def produto_de(texto) -> str | None:
    t = _chave(texto)
    tipo = ("MOBILE" if re.search(r"\bMOB(ILE)?\b", t) else
            "PEDESTAL" if re.search(r"\bPED(ESTAL)?\b", t) else
            "SONDA" if "SONDA" in t else None)
    porte = next((p for p in ("TRIPLO", "DUPLO", "SIMPLES") if p in t), None)
    return " ".join(x for x in (tipo, porte) if x) or None


def associar(contratos: list, bombas: list) -> list:
    """Para cada contrato com título vencido, aponta o equipamento provável.

    contratos: [{contrato, descricao, observacao, titulos, saldo}]
    bombas:    [{serial, nome}]

    Devolve a lista de contratos com `serial_provavel`, `confianca` e
    `porque` — este último é o que permite ao vendedor discordar com base.
    """
    saida = []
    unico = len({b.get("serial") for b in bombas}) == 1

    for ct in contratos:
        obs = ct.get("observacao") or ""
        cidade = cidade_de(obs)
        produto = produto_de(obs)

        if unico and bombas:
            saida.append({**ct, "serial_provavel": bombas[0]["serial"],
                          "confianca": "inequívoca",
                          "porque": "cliente tem um equipamento só"})
            continue

        candidatos = []
        for b in bombas:
            nome = _chave(b.get("nome"))
            pontos, razoes = 0, []
            if cidade and cidade in nome:
                pontos += 3
                razoes.append(f"cidade {cidade.title()}")
            if produto:
                p_bomba = produto_de(b.get("nome"))
                if p_bomba and p_bomba == produto:
                    pontos += 1
                    razoes.append(produto.title())
            if pontos:
                candidatos.append((pontos, b["serial"], razoes))

        if not candidatos:
            saida.append({**ct, "serial_provavel": None, "confianca": "nenhuma",
                          "porque": ("sem cidade na observação do contrato"
                                     if not cidade else
                                     f"cidade {cidade.title()} não aparece em "
                                     f"nenhuma bomba do cliente")})
            continue

        candidatos.sort(reverse=True)
        melhor = candidatos[0]
        empate = [c for c in candidatos if c[0] == melhor[0]]
        if len(empate) > 1:
            saida.append({
                **ct, "serial_provavel": None, "confianca": "baixa",
                "porque": (f"{len(empate)} equipamentos empatados por "
                           f"{', '.join(melhor[2])}: "
                           f"{', '.join(str(c[1]) for c in empate)}")})
        else:
            saida.append({
                **ct, "serial_provavel": melhor[1],
                "confianca": "alta" if melhor[0] >= 3 else "média",
                "porque": " + ".join(melhor[2])})
    return saida
