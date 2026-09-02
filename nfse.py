"""
Ponte para o número da NFS-e.

O CIGAM tem o campo NF_SERVICO no relatório de títulos (ex.: 202400000016587
para a fatura 20261095, 6111 para a 6284). Ele NÃO existe em nenhuma tabela do
warehouse — conferi em silver.lancamentos_enriquecidos e em
bronze.cigam__notas_fiscais, e o landing não é legível com a permissão atual.

Até que o campo seja ingerido, esta ponte permite exibir a NFS-e no e-mail:
cole o export do CIGAM numa aba com as colunas FATURA e NF_SERVICO, e o
template usa o número de lá.

POR QUE UMA PONTE E NÃO UM PALPITE
A primeira versão do template mostrava o campo `nf` na coluna NFS-e. Ele é
igual ao `fatura`, então o e-mail saía com o documento repetido em todas as
linhas parecendo número de nota. Coluna vazia é honesta; coluna preenchida com
o dado errado, não.

A aba é OPCIONAL: sem ela, a coluna NFS-e simplesmente não aparece.
"""

import re

ABA_NFSE = "NFSe_Titulos"


def _fatura(valor) -> str | None:
    """'20261095/' e '6284/1' -> '20261095' e '6284'. A fatura do export vem
    com a parcela colada; a do DW, sem."""
    d = re.sub(r"\s+", "", str(valor or "")).split("/")[0].upper()
    return d or None


def carregar(planilha, io_sheets, aba: str = ABA_NFSE) -> dict:
    """Devolve {fatura: nfse}. Aba ausente ou vazia devolve {} — sem erro."""
    linhas = io_sheets.ler_aba(planilha, aba, obrigatoria=False)
    if not linhas:
        return {}

    mapa = {}
    for l in linhas:
        fat = _fatura(l.get("fatura") or l.get("FATURA") or l.get("doc"))
        nfse = str(l.get("nf_servico") or l.get("NF_SERVICO")
                   or l.get("nfse") or "").strip()
        # NF_SERVICO = 0 significa "não tem nota de serviço", não zero.
        if fat and nfse and nfse not in ("0", "0.0"):
            mapa[fat] = nfse
    print(f"  NFS-e: {len(mapa)} fatura(s) mapeada(s) da aba '{aba}'")
    return mapa


def aplicar(titulos: list, mapa: dict) -> list:
    """Preenche `nfse` nos títulos que têm correspondência."""
    if not mapa:
        return titulos
    for t in titulos:
        num = mapa.get(_fatura(t.get("doc")))
        if num:
            t["nfse"] = num
    return titulos
