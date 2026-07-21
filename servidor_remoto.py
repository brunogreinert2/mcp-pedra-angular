#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
servidor_remoto.py — MCP Pedra Angular, versão REMOTA (HTTP)

Mesmas quatro ferramentas do servidor.py local, mas acessível por URL.
Qualquer pessoa conecta colando UMA URL no campo "Adicionar conector
personalizado" do Claude (Desktop, web ou Code) — sem pip, sem editar
JSON, sem terminal.

RODAR LOCALMENTE (teste):
    python servidor_remoto.py
    -> http://localhost:8000/mcp

HOSPEDAR (Render, Railway, Fly, ou qualquer host que rode Python):
    - requirements.txt: mcp, pyyaml
    - comando de start:  python servidor_remoto.py
    - a porta vem da variável de ambiente PORT (padrão dos hosts)
    -> a URL pública fica algo como https://mcp-pedraangular.onrender.com/mcp
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp.server.fastmcp import FastMCP
from pedra_mcp.cliente import (
    ClientePedraAngular, construir_arvore, listar_nivel,
    separar_frontmatter, buscar_lexical, resolver_citacao,
    extrair_trecho_por_ancora, BASE_URL,
)

BASE = os.environ.get("PEDRA_ANGULAR_BASE_URL", BASE_URL)
cliente = ClientePedraAngular(base_url=BASE, usar_rede=True)

_arvore_cache = None
_arvore_ts = None


def _arvore():
    global _arvore_cache, _arvore_ts
    cliente.catalogo()
    if _arvore_cache is None or _arvore_ts != cliente._catalogo_timestamp:
        _arvore_cache = construir_arvore(cliente.catalogo())
        _arvore_ts = cliente._catalogo_timestamp
    return _arvore_cache


mcp = FastMCP(
    "pedra-angular",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
)


@mcp.custom_route("/", methods=["GET"])
async def saude(request):
    """Página simples pra confirmar visualmente, no navegador, que o
    servidor está de pé. O endpoint de verdade (/mcp) só responde a POST
    no protocolo MCP -- por isso ele sozinho dá 'Not Found' no navegador,
    o que é esperado, não um sinal de erro."""
    from starlette.responses import PlainTextResponse
    try:
        n = len(cliente.catalogo())
        status = f"{n} obras carregadas no catálogo."
    except Exception as e:
        status = f"catálogo ainda não carregado ou com erro: {e}"
    return PlainTextResponse(
        "Pedra Angular MCP -- servidor no ar.\n"
        f"{status}\n"
        "O endpoint /mcp só responde ao protocolo MCP (via Claude), "
        "não a um navegador comum -- 'Not Found' ali é esperado."
    )


@mcp.tool()
def listar_filhos(caminho: list[str] = []) -> str:
    """Navega a árvore do corpus por título, sem carregar texto — barato,
    funciona mesmo com autores de centenas de obras (ex.: Plutarco).
    Devolve subpastas (para continuar navegando) e obras (folhas).
    Caminho vazio = raiz. Ex.: ['FILOSOFIA','Moralistas','Plutarco']."""
    try:
        r = listar_nivel(_arvore(), caminho)
    except KeyError as e:
        return f"Caminho não encontrado: {e}"
    linhas = [f"Em {'/'.join(caminho) or '(raiz)'}:"]
    if r["subpastas"]:
        linhas.append("Subpastas: " + ", ".join(r["subpastas"]))
    if r["obras"]:
        linhas.append("Obras aqui:")
        for o in r["obras"]:
            linhas.append(f"  - [{o['id']}] {o['titulo']} — {o['autor']}")
    return "\n".join(linhas)


@mcp.tool()
def ler_trecho_exato(id_obra: str = "", citacao: str = "") -> str:
    """Lê um trecho exato — pelo id do catálogo OU por citação natural
    no padrão 'Gn 1:1'. Devolve o texto E o metadado (tradutor, fonte,
    licença) para nunca citar sem saber de qual tradução/edição veio.
    Se a citação bater com mais de uma tradução, lista as opções."""
    if citacao and not id_obra:
        resolvido = resolver_citacao(citacao, cliente.catalogo())
        if resolvido is None:
            return (f"Não reconheci '{citacao}' como citação bíblica (padrão 'Gn 1:1'). "
                    f"Para obras clássicas, use id_obra (via listar_filhos ou buscar_no_corpus).")
        candidatos = resolvido["candidatos"]
        if len(candidatos) > 1:
            linhas = [f"'{citacao}' bate com mais de uma tradução — escolha uma (id_obra):"]
            for c in candidatos:
                linhas.append(f"  - [{c.id}] {c.titulo}")
            return "\n".join(linhas)
        obra = candidatos[0]
        bruto = cliente.conteudo_bruto(obra.arquivo)
        meta, corpo = separar_frontmatter(bruto)
        trecho = extrair_trecho_por_ancora(corpo, resolvido["ancora"])
        if trecho is None:
            return f"Obra encontrada ({obra.titulo}), mas '{resolvido['ancora']}' não achou correspondência no texto."
        return (f"OBRA: {meta.get('title', obra.titulo)}  [{resolvido['coordenada']}]\n"
                f"TRADUÇÃO: {meta.get('translation') or meta.get('translator')}\n"
                f"FONTE: {meta.get('source')}\n"
                f"LICENÇA: {meta.get('license', '(ver source)')}\n---\n" + trecho)

    if not id_obra:
        return "Informe id_obra ou citacao."
    obra = next((o for o in cliente.catalogo() if o.id == id_obra), None)
    if obra is None:
        return f"Nenhuma obra com id='{id_obra}' no catálogo."
    try:
        bruto = cliente.conteudo_bruto(obra.arquivo)
    except Exception as e:
        return f"Erro ao buscar o arquivo: {e}"
    meta, corpo = separar_frontmatter(bruto)
    return (f"OBRA: {meta.get('title', obra.titulo)}\n"
            f"AUTOR: {meta.get('author', obra.autor)}\n"
            f"TRADUÇÃO: {meta.get('translation') or meta.get('translator')}\n"
            f"FONTE: {meta.get('source')}\n"
            f"LICENÇA: {meta.get('license', '(ver source)')}\n---\n" + corpo.strip())


@mcp.tool()
def buscar_no_corpus(pergunta: str, top: int = 5) -> str:
    """Busca lexical (por palavra, NÃO por significado) em todo o corpus.
    Útil quando não se sabe autor/obra de antemão. Para temas cujo vocabulário
    difere do texto original, prefira listar_filhos e navegar até a obra."""
    resultados = buscar_lexical(pergunta, cliente.catalogo(), cliente, top=top)
    if not resultados:
        return "Nenhum resultado (busca é lexical, não semântica)."
    linhas = [f"Resultados para '{pergunta}' (busca lexical):"]
    for r in resultados:
        linhas.append(f"  [{r['pontos']} pts] {r['autor']} — {r['titulo']} (id: {r['id']})")
    return "\n".join(linhas)


@mcp.tool()
def atualizar_catalogo() -> str:
    """Força buscar agora a versão mais recente do catálogo do site, sem
    esperar o prazo normal de revalidação. Use quando o usuário perguntar
    se há obras novas publicadas."""
    antes = len(cliente.catalogo())
    cliente.catalogo(forcar_atualizacao=True)
    depois = len(cliente.catalogo())
    diff = depois - antes
    msg = f"Catálogo atualizado. Antes: {antes} obras. Agora: {depois} obras."
    if diff > 0:
        msg += f" ({diff} nova(s)!)"
    elif diff < 0:
        msg += f" ({-diff} a menos — removida ou renomeada?)"
    else:
        msg += " Sem mudança na contagem."
    return msg


@mcp.tool()
def atualizar_obra(id_obra: str) -> str:
    """Força reler AGORA uma obra específica do site, ignorando o cache (que
    por padrão guarda o conteúdo de uma obra indefinidamente, assumindo que
    texto publicado é estável). Use quando o usuário disser que editou uma
    obra específica e a ferramenta parece não estar vendo a mudança."""
    obra = next((o for o in cliente.catalogo() if o.id == id_obra), None)
    if obra is None:
        return f"Nenhuma obra com id='{id_obra}' no catálogo."
    try:
        cliente.conteudo_bruto(obra.arquivo, forcar_atualizacao=True)
    except Exception as e:
        return f"Erro ao atualizar: {e}"
    return f"'{obra.titulo}' relido do site agora, cache antigo descartado."


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
