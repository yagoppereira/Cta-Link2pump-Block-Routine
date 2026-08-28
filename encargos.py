"""
Cálculo de juros e multa para o aviso de bloqueio.

Porte 1:1 da fórmula que hoje roda na planilha:

    =IF(AND(K=TRUE; J=TRUE);
        IF(E<=4;
           ROUND(ROUND(G*0,01/30; 2)*E; 2);
           ROUND(ROUND(G*0,02; 2) + ROUND(ROUND(G*0,01/30; 2)*E; 2); 2));
        0)

    G = valor base do título (coluna OCULTA — não é a coluna TOTAL exibida,
        que já vem atualizada: TOTAL_exibido = G + encargos)
    E = dias corridos = MAX(0; (data_pagamento OU hoje) - vencimento)
    J = COBRA JUROS (checkbox)      K = COBRA TITULO (checkbox)

DECISÕES DE PORTE (não mude sem revalidar a paridade):
  1. O juro DIÁRIO é arredondado a centavo ANTES de multiplicar pelos dias.
     Não é saldo * 1% * dias/30. A ordem importa e muda o resultado.
  2. ROUND() do Sheets é meio-pra-cima. round() do Python é bancário
     (round(0.125, 2) == 0.12). Por isso Decimal + ROUND_HALF_UP.
  3. Entrada em Decimal a partir de str. Nunca float — 0.1 + 0.2 != 0.3.

ATENCAO — o arredondamento diario NAO e neutro. O juro de um dia arredondado a
centavo, multiplicado por N dias, desvia do nominal de 1% a.m. Na linha de
paridade abaixo (317,43 / 210 dias) a fórmula cobra 23,10 contra 22,22 nominais,
ou seja +3,96% (taxa efetiva 1,04% a.m.). Se o texto do e-mail declarar a taxa,
o número ao lado não vai corresponder a ela.

PENDENTE: J e K sao checkbox manuais na planilha. Para automatizar, precisam
virar regra derivada de algum campo (tipo de cobranca, contrato, cliente).
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

CENTAVO = Decimal("0.01")

TAXA_JUROS_MES = Decimal("0.01")   # 1% a.m.
DIAS_BASE_MES = Decimal("30")      # base 30, não o nº real de dias do mês
TAXA_MULTA = Decimal("0.02")       # 2%
CARENCIA_MULTA_DIAS = 4            # multa só a partir do 5º dia de atraso

Numerico = Union[str, int, Decimal]


def _dec(valor: Numerico) -> Decimal:
    """Converte para Decimal sem passar por float."""
    if isinstance(valor, Decimal):
        return valor
    return Decimal(str(valor))


def _round2(valor: Decimal) -> Decimal:
    """Equivalente ao ROUND(x; 2) do Google Sheets (meio-pra-cima)."""
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def calcular_encargos(saldo: Numerico, dias_atraso: int,
                      cobra_juros: bool = True, cobra_titulo: bool = True) -> dict:
    """Devolve os encargos de UM título, com memória de cálculo.

    saldo:        valor base do título (o G da planilha, NÃO o TOTAL atualizado)
    dias_atraso:  dias corridos entre o vencimento e a data-base do envio
    cobra_juros:  coluna J
    cobra_titulo: coluna K
    """
    saldo = _dec(saldo)

    if not (cobra_juros and cobra_titulo) or dias_atraso <= 0 or saldo <= 0:
        zero = Decimal("0.00")
        return {
            "saldo": _round2(saldo),
            "dias_atraso": dias_atraso,
            "juros_dia": zero,
            "juros": zero,
            "multa": zero,
            "encargos": zero,
            "total": _round2(saldo),
            "aplicou_multa": False,
        }

    juros_dia = _round2(saldo * TAXA_JUROS_MES / DIAS_BASE_MES)
    juros = _round2(juros_dia * Decimal(dias_atraso))

    aplicou_multa = dias_atraso > CARENCIA_MULTA_DIAS
    multa = _round2(saldo * TAXA_MULTA) if aplicou_multa else Decimal("0.00")

    encargos = _round2(multa + juros)

    return {
        "saldo": _round2(saldo),
        "dias_atraso": dias_atraso,
        "juros_dia": juros_dia,
        "juros": juros,
        "multa": multa,
        "encargos": encargos,
        "total": _round2(saldo + encargos),
        "aplicou_multa": aplicou_multa,
    }


def conferir_paridade(casos: list) -> bool:
    """Valida o porte contra valores reais da planilha ANTES de usar em envio.

    casos: lista de (saldo, dias_atraso, encargos_esperados) — copie 20+ linhas
           da planilha atual, incluindo pelo menos um caso com dias <= 4,
           um caso de saldo alto e um de saldo baixo.

    Retorna True se todos baterem no centavo. Imprime as divergências.
    """
    divergencias = []
    for saldo, dias, esperado in casos:
        obtido = calcular_encargos(saldo, dias)["encargos"]
        if obtido != _dec(esperado):
            divergencias.append((saldo, dias, _dec(esperado), obtido))

    if divergencias:
        print(f"❌ {len(divergencias)} de {len(casos)} casos divergiram:")
        for saldo, dias, esperado, obtido in divergencias:
            delta = obtido - esperado
            print(f"   saldo={saldo} dias={dias} | planilha={esperado} "
                  f"python={obtido} | delta={delta:+}")
        return False

    print(f"✅ Paridade confirmada em {len(casos)} casos.")
    return True


if __name__ == "__main__":
    # Substitua por linhas reais da planilha antes de confiar no módulo.
    exemplos = [
        # Caso REAL, conferido contra o extrato da LGA em 28/08/2026:
        # G=317,43 · 210 dias · juros 23,10 + multa 6,35 · TOTAL exibido 346,88
        ("317.43", 210, "29.45"),
        # Casos sintéticos — substitua por linhas reais da planilha.
        ("1500.00", 10, "35.00"),
        ("1000.00", 4, "1.32"),   # ramo sem multa
        ("14.00", 365, "0.28"),   # juro diário arredonda pra 0,00
    ]
    conferir_paridade(exemplos)
