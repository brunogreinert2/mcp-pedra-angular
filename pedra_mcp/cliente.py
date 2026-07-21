"""
cliente.py — Pedra Angular MCP

Fala com o site real (pedraangular.app.br), que já expõe publicamente:
  - /livros/catalogo.json    (lista plana: id, titulo, autor, arquivo)
  - /livros/<arquivo>        (o .md cru de cada obra, com YAML na frente)

Nenhuma infraestrutura nova precisa existir — este módulo só lê o que
o app já publica. Tudo é cacheado localmente em disco, então depois da
primeira consulta, tudo funciona OFFLINE também.
"""
from __future__ import annotations
import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

BASE_URL = "https://pedraangular.app.br/livros/"
CACHE_DIR = Path.home() / ".cache" / "pedra_angular_mcp"
CATALOGO_CACHE = CACHE_DIR / "catalogo.json"
ARQUIVOS_CACHE = CACHE_DIR / "arquivos"
TTL_CATALOGO_SEGUNDOS = 6 * 3600  # recarrega o catálogo a cada 6h; obras individuais são imutáveis o bastante para cache indefinido


def _garantir_pastas():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ARQUIVOS_CACHE.mkdir(parents=True, exist_ok=True)


def _buscar_url(url: str, timeout: int = 15) -> str:
    """Busca uma URL. Levanta erro claro se a rede não estiver disponível —
    nunca inventa conteúdo no lugar de um fetch que falhou."""
    req = urllib.request.Request(url, headers={"User-Agent": "pedra-angular-mcp/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


@dataclass
class Obra:
    id: str
    titulo: str
    autor: str
    arquivo: str  # caminho relativo, ex: FILOSOFIA/Moralistas/Plutarco/.../arquivo.md

    @classmethod
    def de_catalogo(cls, o: dict) -> "Obra":
        """
        Constrói uma Obra a partir de uma entrada bruta do catalogo.json,
        ignorando qualquer campo extra que o catálogo real tenha e que esta
        classe não conheça (ex.: 'sistema_referencia'), e preenchendo 'autor'
        com um placeholder se estiver ausente. Um `Obra(**o)` direto quebra a
        construção INTEIRA do catálogo assim que um campo novo aparece —
        aqui, um campo desconhecido só é ignorado, nunca derruba tudo.
        """
        return cls(
            id=o["id"],
            titulo=o.get("titulo", o["id"]),
            autor=o.get("autor", "—"),
            arquivo=o["arquivo"],
        )

    @property
    def caminho_partes(self) -> list[str]:
        return self.arquivo.split("/")


class ClientePedraAngular:
    def __init__(self, base_url: str = BASE_URL, usar_rede: bool = True):
        self.base_url = base_url
        self.usar_rede = usar_rede
        _garantir_pastas()
        self._catalogo: Optional[list[Obra]] = None

    # ---------- catálogo ----------
    def catalogo(self, forcar_atualizacao: bool = False) -> list[Obra]:
        if self._catalogo is not None and not forcar_atualizacao:
            return self._catalogo

        usar_cache = (
            not forcar_atualizacao
            and CATALOGO_CACHE.exists()
            and (time.time() - CATALOGO_CACHE.stat().st_mtime) < TTL_CATALOGO_SEGUNDOS
        )
        if usar_cache:
            dados = json.loads(CATALOGO_CACHE.read_text(encoding="utf-8"))
        elif self.usar_rede:
            texto = _buscar_url(self.base_url + "catalogo.json")
            dados = json.loads(texto)
            CATALOGO_CACHE.write_text(texto, encoding="utf-8")
        elif CATALOGO_CACHE.exists():
            dados = json.loads(CATALOGO_CACHE.read_text(encoding="utf-8"))
        else:
            raise RuntimeError(
                "Sem rede e sem cache local do catálogo. "
                "Rode uma vez com usar_rede=True antes de usar offline."
            )

        self._catalogo = [Obra.de_catalogo(o) for o in dados["livros"]]
        return self._catalogo

    # ---------- conteúdo de uma obra ----------
    def _caminho_cache_arquivo(self, arquivo: str) -> Path:
        return ARQUIVOS_CACHE / arquivo

    def conteudo_bruto(self, arquivo: str) -> str:
        """Devolve o .md cru (YAML + corpo) de uma obra, cacheado em disco."""
        destino = self._caminho_cache_arquivo(arquivo)
        if destino.exists():
            return destino.read_text(encoding="utf-8")
        if not self.usar_rede:
            raise RuntimeError(f"Arquivo não está em cache e rede está desligada: {arquivo}")
        texto = _buscar_url(self.base_url + arquivo)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(texto, encoding="utf-8")
        return texto

    def semear_cache_local(self, arquivo: str, conteudo: str):
        """Permite popular o cache manualmente (usado nos testes deste protótipo,
        e útil se você já tem os .md localmente e quer evitar rede de todo)."""
        destino = self._caminho_cache_arquivo(arquivo)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(conteudo, encoding="utf-8")

    def semear_catalogo_local(self, dados_json: dict):
        CATALOGO_CACHE.write_text(json.dumps(dados_json, ensure_ascii=False), encoding="utf-8")
        self._catalogo = [Obra.de_catalogo(o) for o in dados_json["livros"]]


# ---------- parsing: separar YAML do corpo ----------
_RX_FRONTMATTER = re.compile(r"^---\s*\n(.*?\n)---\s*\n(.*)$", re.DOTALL)


def separar_frontmatter(md: str) -> tuple[dict, str]:
    m = _RX_FRONTMATTER.match(md)
    if not m:
        return {}, md
    import yaml
    meta = yaml.safe_load(m.group(1)) or {}
    corpo = m.group(2)
    return meta, corpo


# ---------- árvore de navegação (a partir do catálogo, que é uma lista plana) ----------
def construir_arvore(obras: list[Obra]) -> dict:
    """
    Constrói uma árvore a partir do caminho do 'arquivo' de cada obra.
    Ex.: FILOSOFIA/Moralistas/Plutarco/Advice_About_Keeping_Well/arquivo.md
    vira: {"FILOSOFIA": {"Moralistas": {"Plutarco": {"Advice_About_Keeping_Well": {"_obra": <Obra>}}}}}
    """
    raiz: dict = {}
    for obra in obras:
        partes = obra.caminho_partes
        no = raiz
        for parte in partes[:-1]:  # pastas
            no = no.setdefault(parte, {})
        # a última parte é o arquivo .md em si -> a pasta que o contém já é a "obra"
        # (o padrão do catálogo é sempre pasta_da_obra/arquivo.md)
        no["_obras"] = no.get("_obras", [])
        no["_obras"].append(obra)
    return raiz


def listar_nivel(arvore: dict, caminho: list[str]) -> dict:
    """Navega a árvore até 'caminho' e devolve o que existe ali:
    subpastas (para continuar navegando) e obras (folhas, prontas pra ler)."""
    no = arvore
    for parte in caminho:
        if parte not in no:
            raise KeyError(f"Caminho não encontrado: {'/'.join(caminho)} (faltou '{parte}')")
        no = no[parte]
    subpastas = sorted(k for k in no.keys() if k != "_obras")
    obras = no.get("_obras", [])
    return {"subpastas": subpastas, "obras": [{"id": o.id, "titulo": o.titulo, "autor": o.autor} for o in obras]}


# ---------- busca lexical simples (nível 1 — full-text, não semântico) ----------
def normalizar(t: str) -> str:
    return re.sub(r"[^\wà-ÿ]+", " ", t.lower())


def buscar_lexical(pergunta: str, obras: list[Obra], cliente: ClientePedraAngular, top: int = 5) -> list[dict]:
    termos = set(normalizar(pergunta).split()) - {
        "o", "a", "de", "que", "do", "da", "em", "para", "com", "sobre", "é", "os", "as"
    }
    pontuados = []
    for obra in obras:
        try:
            bruto = cliente.conteudo_bruto(obra.arquivo)
        except RuntimeError:
            continue  # obra não cacheada e sem rede -> pula, não trava a busca inteira
        _, corpo = separar_frontmatter(bruto)
        texto_norm = normalizar(obra.titulo + " " + obra.autor + " " + corpo)
        pontos = sum(texto_norm.count(t) for t in termos if len(t) > 2)
        if pontos > 0:
            pontuados.append((pontos, obra))
    pontuados.sort(key=lambda x: -x[0])
    return [{"id": o.id, "titulo": o.titulo, "autor": o.autor, "pontos": p} for p, o in pontuados[:top]]


# ---------- citação natural ("Gn 1:1") -> id do catálogo + trecho exato ----------
# Tabela canônica de abreviações da Bíblia em português — mesma lista que
# qualquer edição impressa usa (Gn=1 ... Ap=66). Não é invenção nossa: é o
# padrão que o próprio placeholder do app ("Gn 1:1") já pressupõe.
_LIVROS_BIBLIA = [
    "Gn","Êx","Lv","Nm","Dt","Js","Jz","Rt","1Sm","2Sm","1Rs","2Rs","1Cr","2Cr",
    "Ed","Ne","Et","Jó","Sl","Pv","Ec","Ct","Is","Jr","Lm","Ez","Dn","Os","Jl",
    "Am","Ob","Jn","Mq","Na","Hc","Sf","Ag","Zc","Ml","Mt","Mc","Lc","Jo","At",
    "Rm","1Co","2Co","Gl","Ef","Fp","Cl","1Ts","2Ts","1Tm","2Tm","Tt","Fm","Hb",
    "Tg","1Pe","2Pe","1Jo","2Jo","3Jo","Jd","Ap",
]
_ABREV_PARA_NUMERO = {abrev.lower(): i + 1 for i, abrev in enumerate(_LIVROS_BIBLIA)}

_RX_CITACAO_BIBLICA = re.compile(
    r"^\s*(\d?\s?[A-Za-zÀ-ÿ]+)\.?\s+(\d+)\s*[:.]\s*(\d+)\s*$"
)


def resolver_citacao(texto: str, obras: list[Obra]) -> Optional[dict]:
    """
    Tenta interpretar 'texto' como uma citação bíblica no padrão 'Gn 1:1'
    (o mesmo que o app reconhece). Se conseguir, devolve:
        {"obra": Obra, "coordenada": "1:1", "ancora": "^gn-1-1"}
    Não resolve citações de obras clássicas (essas não têm 'abrev' — usa-se
    ler_trecho_exato com o id do catálogo, ou buscar_no_corpus, para elas).
    """
    m = _RX_CITACAO_BIBLICA.match(texto.strip())
    if not m:
        return None
    abrev_bruto, cap, versiculo = m.groups()
    abrev_norm = abrev_bruto.replace(" ", "").lower()
    numero = _ABREV_PARA_NUMERO.get(abrev_norm)
    if numero is None:
        return None

    candidatos = [o for o in obras if o.id.startswith(f"biblia-{numero:02d}-")]
    if not candidatos:
        return None
    # se houver mais de uma tradução, todas são candidatas; quem chama decide
    # (normalmente a IA já sabe qual tradução o usuário está usando)
    ancora = f"^{abrev_norm}-{cap}-{versiculo}"
    return {"candidatos": candidatos, "coordenada": f"{cap}:{versiculo}", "ancora": ancora}


def extrair_trecho_por_ancora(corpo: str, ancora: str, contexto_linhas: int = 0) -> Optional[str]:
    """Acha a linha terminando em '^ancora' e devolve só ela (+ contexto, se pedido).
    O '^ancora' em si é removido do texto devolvido — é uma referência para
    máquina (igual ao block-reference do Obsidian), não parte da leitura."""
    linhas = corpo.split("\n")
    rx_remover_ancora = re.compile(r"\s\^[a-zA-Z0-9\-_]+\s*$", re.MULTILINE)
    for i, linha in enumerate(linhas):
        if linha.rstrip().endswith(ancora):
            ini = max(0, i - contexto_linhas)
            fim = min(len(linhas), i + contexto_linhas + 1)
            bruto = "\n".join(linhas[ini:fim]).strip()
            return rx_remover_ancora.sub("", bruto)
    return None
