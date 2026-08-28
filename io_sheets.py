"""
Acesso ao Google Sheets. Só I/O — nenhuma regra de negócio mora aqui.

DOIS PONTOS QUE JÁ CAUSARAM PERDA SILENCIOSA DE DADO:

  1. resize=True no set_with_dataframe. worksheet.clear() apaga o CONTEÚDO
     mas não redimensiona a grade. Se a aba nasceu com 1.000 linhas e o
     DataFrame tem 4.200, o que não cabe é descartado sem erro. Ordenado por
     nome, some sempre o fim do alfabeto.

  2. append_log grava UMA linha por vez, via append_row. Runtime do Colab cai;
     log em memória gravado no fim significa reexecutar e mandar tudo de novo.

O retry de 429/500/502/503 é o mesmo padrão do script original.
"""

import re
import time

import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from gspread_dataframe import set_with_dataframe


def com_retry(func, *args, max_tentativas: int = 5, espera_inicial: float = 2.0, **kwargs):
    """Backoff exponencial para erro transitório do Sheets. 403 e 404 sobem na
    hora: permissão e planilha errada não melhoram esperando."""
    tentativa = 0
    while True:
        try:
            return func(*args, **kwargs)
        except APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            tentativa += 1
            if status not in (429, 500, 502, 503) or tentativa >= max_tentativas:
                raise
            espera = espera_inicial * (2 ** (tentativa - 1))
            print(f"  Sheets devolveu {status} ({tentativa}/{max_tentativas}). "
                  f"Aguardando {espera:.0f}s.")
            time.sleep(espera)


def abrir(gc, spreadsheet_id: str):
    return com_retry(gc.open_by_key, spreadsheet_id)


def ler_aba(planilha, nome: str, obrigatoria: bool = True) -> list:
    """Devolve list[dict]. Cabeçalho normalizado para minúsculo sem espaço."""
    try:
        ws = com_retry(planilha.worksheet, nome)
    except WorksheetNotFound:
        if obrigatoria:
            raise RuntimeError(
                f"Aba '{nome}' não existe em '{planilha.title}'. "
                f"Confira se o ID da planilha é o certo antes de criar a aba."
            )
        return []

    valores = com_retry(ws.get_all_values)
    if len(valores) < 2:
        return []

    cab = [re.sub(r"\s+", "_", str(c).strip()).lower() for c in valores[0]]
    linhas = []
    for row in valores[1:]:
        if not any(str(c).strip() for c in row):
            continue
        row = list(row) + [""] * (len(cab) - len(row))
        linhas.append(dict(zip(cab, row)))
    return linhas


def escrever_aba(planilha, nome: str, df, limpar: bool = True):
    """Escreve um DataFrame. resize=True é o que impede o truncamento."""
    import pandas as pd  # noqa: F401

    n_linhas = max(100, len(df) + 20)
    n_colunas = max(26, len(df.columns) + 2)

    try:
        ws = com_retry(planilha.worksheet, nome)
        if limpar:
            com_retry(ws.clear)
    except WorksheetNotFound:
        ws = com_retry(planilha.add_worksheet, title=nome,
                       rows=str(n_linhas), cols=str(n_colunas))
        print(f"  aba '{nome}' criada")

    com_retry(set_with_dataframe, ws, df.fillna(""),
              include_index=False, include_column_header=True,
              resize=True)   # <- não remova
    com_retry(ws.freeze, rows=1)
    return ws


def append_log(planilha, nome: str, linha: dict):
    """Acrescenta UMA linha. Cria a aba e o cabeçalho na primeira chamada.

    Nunca limpa: o log é histórico e é o que permite retomar uma campanha
    interrompida e provar o que foi enviado meses depois.
    """
    colunas = list(linha.keys())
    try:
        ws = com_retry(planilha.worksheet, nome)
        cab = com_retry(ws.row_values, 1)
        if not cab:
            com_retry(ws.append_row, colunas, value_input_option="RAW")
            cab = colunas
    except WorksheetNotFound:
        ws = com_retry(planilha.add_worksheet, title=nome, rows="1000",
                       cols=str(max(26, len(colunas) + 2)))
        com_retry(ws.append_row, colunas, value_input_option="RAW")
        cab = colunas

    # Segue a ordem do cabeçalho existente: se alguém acrescentar coluna
    # depois, as linhas antigas continuam alinhadas.
    com_retry(ws.append_row, [str(linha.get(c, "")) for c in cab],
              value_input_option="RAW")


def chaves_ja_enviadas(planilha, nome_log: str, id_campanha: str) -> set:
    """Códigos que já receberam e-mail NESTA campanha. É o que faz uma
    reexecução continuar em vez de duplicar."""
    return {
        l.get("codigo_cliente")
        for l in ler_aba(planilha, nome_log, obrigatoria=False)
        if l.get("id_campanha") == id_campanha and l.get("status") == "ENVIADO"
    }
