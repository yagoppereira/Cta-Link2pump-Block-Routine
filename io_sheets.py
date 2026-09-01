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


def _texto_celula(v):
    """Valor -> texto para a célula. Data sem hora sai como AAAA-MM-DD em vez
    de '2026-08-31 00:00:00', que polui a planilha."""
    import datetime as _dt
    import pandas as pd

    if v is None or v is pd.NaT:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    if isinstance(v, _dt.datetime):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) \
               else v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, _dt.date):
        return v.isoformat()
    return str(v)


def _para_sheets(df):
    """Prepara o DataFrame para o Sheets sem quebrar tipos exóticos.

    `df.fillna("")` parece inofensivo e não é: colunas DATE vindas do BigQuery
    chegam como `dbdate` (extension array), e enfiar string vazia nelas estoura
    com `ValueError: Bad date string: ''`. O db_dtypes está certo em recusar —
    string vazia não é data. O erro é nosso.

    Monta um DataFrame NOVO em vez de reatribuir colunas: assim nenhum
    extension array sobrevive à conversão e não há chance de o pandas tentar
    recastar o valor de volta para o tipo original.

    Numérico e booleano passam intactos, para o Sheets receber número como
    número. O resto vira texto via map, que não depende do dtype e por isso
    funciona igual para dbdate, datetime, Decimal ou object.
    """
    import pandas as pd

    from decimal import Decimal

    def _e_numerica(serie):
        """NUMERIC do BigQuery chega como Decimal dentro de coluna `object`, e
        is_numeric_dtype devolve False. Se isso passar para o ramo de texto,
        str(Decimal('48300.300000000')) vira "48300.300000000" e o Sheets em
        pt-BR lê o ponto como separador de MILHAR: R$ 48.300,30 aparece como
        48.300.300.000.000. Inflado em 10^9, sem erro nenhum."""
        if pd.api.types.is_numeric_dtype(serie) or pd.api.types.is_bool_dtype(serie):
            return True
        naonulos = [v for v in serie.tolist() if v is not None and not (
            isinstance(v, float) and pd.isna(v))]
        return bool(naonulos) and all(isinstance(v, Decimal) for v in naonulos)

    colunas = {}
    for col in df.columns:
        s = df[col]
        if _e_numerica(s):
            colunas[col] = [None if v is None or (isinstance(v, float) and pd.isna(v))
                            or v is pd.NA else float(v)
                            for v in s.astype(object).tolist()]
        else:
            colunas[col] = [_texto_celula(v) for v in s.tolist()]
    return pd.DataFrame(colunas, columns=list(df.columns))


def ler_aba_procurando_cabecalho(planilha, nome: str, obrigatorias: list,
                                 max_linhas: int = 10) -> list:
    """Como ler_aba, mas o cabeçalho não precisa estar na linha 1.

    A aba ACORDOS tem 'TOTAL EM ACORDO: R$ ...' na primeira linha e o cabeçalho
    real na segunda. Assumir linha 1 fazia a leitura devolver zero registros —
    sem erro, o que é pior: o cruzamento simplesmente não acontecia e o disparo
    seguiria notificando quem está pagando acordo.

    obrigatorias: nomes de coluna que identificam o cabeçalho de verdade.
    """
    ws = com_retry(planilha.worksheet, nome)
    valores = com_retry(ws.get_all_values)
    alvo = {re.sub(r"\s+", "_", a.strip()).lower() for a in obrigatorias}

    for i, linha in enumerate(valores[:max_linhas]):
        cab = [re.sub(r"\s+", "_", str(c).strip()).lower() for c in linha]
        if alvo.issubset(set(cab)):
            saida = []
            for row in valores[i + 1:]:
                if not any(str(c).strip() for c in row):
                    continue
                row = list(row) + [""] * (len(cab) - len(row))
                saida.append(dict(zip(cab, row)))
            if i:
                print(f"  ('{nome}': cabeçalho na linha {i + 1})")
            return saida

    raise RuntimeError(
        f"Não achei o cabeçalho da aba '{nome}' nas primeiras {max_linhas} "
        f"linhas. Esperava as colunas {sorted(alvo)}. "
        f"Primeira linha lida: {valores[0][:8] if valores else '(aba vazia)'}"
    )


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

    com_retry(set_with_dataframe, ws, _para_sheets(df),
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
