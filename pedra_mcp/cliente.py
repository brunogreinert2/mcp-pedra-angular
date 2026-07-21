"""
cliente.py — Pedra Angular MCP

Fala com o site real (pedraangular.app.br), que já expõe publicamente:
  - /livros/catalogo.json    (lista plana: id, titulo, autor, arquivo)
  - /livros/<arquivo>        (o .md cru de cada obra, com YAML na frente)

Nenhuma infraestrutura nova precisa existir — este módulo só lê o que
o app já publica. Tudo é cacheado localmente em disco, então depois da
primeira consulta, tudo funciona OFFLINE também.

NOVIDADES DESTA VERSÃO (motor de citações v2):
  - Citações bíblicas aceitam nome completo, com ou sem acento, e as
    abreviações tradicionais: 'Gn 1:1', 'genesis 1,1', 'Gênesis 1.1',
    '1 corintios 13:4', 'primeira coríntios 13,4', 'II Timóteo 1:7'...
  - Separadores flexíveis entre capítulo e versículo: ':', '.' ou ','.
  - Intervalos: 'Gn 1:1-3' devolve os três versículos.
  - Capítulo inteiro: 'Salmos 23' (sem versículo) devolve o capítulo.
  - id_obra aproximado: id errado/incompleto sugere os mais próximos
    em vez de responder só "nenhuma obra".
  - Fallback clássico: citação não-bíblica ('Leviathan cap. 13') tenta
    casar com títulos do catálogo antes de desistir.
"""
from __future__ import annotations
import json
import re
import time
import unicodedata
import urllib.request
import urllib.error
from difflib import get_close_matches
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
    req = urllib.request.Request(url, headers={"User-Agent": "pedra-angular-mcp/0.2"})
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
        self._catalogo_timestamp: Optional[float] = None

    # ---------- catálogo ----------
    def catalogo(self, forcar_atualizacao: bool = False) -> list[Obra]:
        # Antes só checava "já tenho em memória?" -- ignorava o TTL depois da
        # primeira vez, então obra nova no site só aparecia reiniciando o app
        # inteiro. Agora reconsulta o TTL sempre, mesmo com cache em memória.
        if (
            self._catalogo is not None
            and self._catalogo_timestamp is not None
            and not forcar_atualizacao
            and (time.time() - self._catalogo_timestamp) < TTL_CATALOGO_SEGUNDOS
        ):
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
        self._catalogo_timestamp = time.time()
        return self._catalogo

    # ---------- conteúdo de uma obra ----------
    def _caminho_cache_arquivo(self, arquivo: str) -> Path:
        return ARQUIVOS_CACHE / arquivo

    def conteudo_bruto(self, arquivo: str, forcar_atualizacao: bool = False) -> str:
        """Devolve o .md cru (YAML + corpo) de uma obra, cacheado em disco.
        Cache sem prazo de validade, de propósito (texto publicado costuma
        ser estável) -- mas isso é ERRADO durante curadoria ativa, quando
        você está editando YAML/conteúdo. Use forcar_atualizacao=True (ou a
        ferramenta atualizar_obra) para ignorar o cache dessa obra específica."""
        destino = self._caminho_cache_arquivo(arquivo)
        if destino.exists() and not forcar_atualizacao:
            return destino.read_text(encoding="utf-8")
        if not self.usar_rede:
            if destino.exists():
                return destino.read_text(encoding="utf-8")  # sem rede -> melhor servir o cache velho que nada
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
        self._catalogo_timestamp = time.time()


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


# ======================================================================
#  MOTOR DE CITAÇÕES v2
# ======================================================================

def _sem_acentos(t: str) -> str:
    """'Gênesis' -> 'Genesis'. Base de toda a tolerância a acentuação."""
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


# Tabela canônica: (abreviação tradicional, nome completo, aliases extras).
# A abreviação tradicional é a que gera a ÂNCORA no texto (^gn-1-1) — o
# mesmo padrão do placeholder do app. Os aliases só apontam para ela.
_NOMES_BIBLIA: list[tuple[str, str, list[str]]] = [
    ("Gn", "Gênesis", ["Gen"]),
    ("Êx", "Êxodo", ["Ex", "Exo", "Exod"]),
    ("Lv", "Levítico", ["Lev"]),
    ("Nm", "Números", ["Num"]),
    ("Dt", "Deuteronômio", ["Deut", "Deu"]),
    ("Js", "Josué", ["Jos"]),
    ("Jz", "Juízes", ["Juiz", "Juizes"]),
    ("Rt", "Rute", ["Rut"]),
    ("1Sm", "1 Samuel", ["1Sa", "1Sam", "1Samuel"]),
    ("2Sm", "2 Samuel", ["2Sa", "2Sam", "2Samuel"]),
    ("1Rs", "1 Reis", ["1Re", "1Reis"]),
    ("2Rs", "2 Reis", ["2Re", "2Reis"]),
    ("1Cr", "1 Crônicas", ["1Cro", "1Cron"]),
    ("2Cr", "2 Crônicas", ["2Cro", "2Cron"]),
    ("Ed", "Esdras", ["Esd"]),
    ("Ne", "Neemias", ["Nee"]),
    ("Et", "Ester", ["Est"]),
    ("Jó", "Jó", ["Job"]),
    ("Sl", "Salmos", ["Sal", "Salmo", "Ps"]),
    ("Pv", "Provérbios", ["Pro", "Prov", "Proverbio", "Proverbios"]),
    ("Ec", "Eclesiastes", ["Ecl", "Qohelet", "Coelet"]),
    ("Ct", "Cânticos", ["Cant", "Cantares", "Cantares de Salomão",
                        "Cântico dos Cânticos", "Cantico dos Canticos"]),
    ("Is", "Isaías", ["Isa"]),
    ("Jr", "Jeremias", ["Jer"]),
    ("Lm", "Lamentações", ["Lam", "Lamentações de Jeremias"]),
    ("Ez", "Ezequiel", ["Eze", "Ezq"]),
    ("Dn", "Daniel", ["Dan"]),
    ("Os", "Oséias", ["Oseias", "Ose"]),
    ("Jl", "Joel", []),
    ("Am", "Amós", ["Amo"]),
    ("Ob", "Obadias", ["Oba", "Abdias"]),
    ("Jn", "Jonas", ["Jon"]),
    ("Mq", "Miquéias", ["Miq", "Miqueias"]),
    ("Na", "Naum", []),
    ("Hc", "Habacuque", ["Hab", "Habacuc"]),
    ("Sf", "Sofonias", ["Sof"]),
    ("Ag", "Ageu", ["Age"]),
    ("Zc", "Zacarias", ["Zac"]),
    ("Ml", "Malaquias", ["Mal"]),
    ("Mt", "Mateus", ["Mat", "Matheus"]),
    ("Mc", "Marcos", ["Mar", "Marc"]),
    ("Lc", "Lucas", ["Luc"]),
    ("Jo", "João", ["Joh", "Joao"]),
    ("At", "Atos", ["Act", "Atos dos Apóstolos", "Atos dos Apostolos"]),
    ("Rm", "Romanos", ["Rom"]),
    ("1Co", "1 Coríntios", ["1Cor", "1Corintios"]),
    ("2Co", "2 Coríntios", ["2Cor", "2Corintios"]),
    ("Gl", "Gálatas", ["Gal"]),
    ("Ef", "Efésios", ["Efe", "Efes"]),
    ("Fp", "Filipenses", ["Fil", "Flp", "Filip"]),
    ("Cl", "Colossenses", ["Col"]),
    ("1Ts", "1 Tessalonicenses", ["1Tes", "1Tess"]),
    ("2Ts", "2 Tessalonicenses", ["2Tes", "2Tess"]),
    ("1Tm", "1 Timóteo", ["1Tim", "1Timoteo"]),
    ("2Tm", "2 Timóteo", ["2Tim", "2Timoteo"]),
    ("Tt", "Tito", ["Tit"]),
    ("Fm", "Filemom", ["Filemon", "Flm"]),
    ("Hb", "Hebreus", ["Heb"]),
    ("Tg", "Tiago", ["Tia", "Thiago"]),
    ("1Pe", "1 Pedro", ["1Pd", "1Ped"]),
    ("2Pe", "2 Pedro", ["2Pd", "2Ped"]),
    ("1Jo", "1 João", ["1Joao"]),
    ("2Jo", "2 João", ["2Joao"]),
    ("3Jo", "3 João", ["3Joao"]),
    ("Jd", "Judas", ["Jud"]),
    ("Ap", "Apocalipse", ["Apo", "Apoc", "Revelação", "Revelacao"]),
]

# Dois mapas: um sensível a acento (consultado primeiro) e um sem acento
# (fallback). O motivo de serem DOIS: 'Jó' sem acento vira 'Jo', que é a
# abreviação de João — só o acento distingue. Consultando o mapa acentuado
# primeiro, 'jó 1:1' resolve para Jó; e no mapa sem acento, 'jo'/'joao'
# ficam explicitamente com João (quem quiser Jó sem teclado acentuado
# escreve 'Job 1:1').
_ALIAS_ACENTUADO: dict[str, int] = {}
_ALIAS_SEM_ACENTO: dict[str, int] = {}


def _chave_alias(texto: str) -> str:
    return re.sub(r"[\s\.]+", "", texto.strip().lower())


def _registrar_alias(alias: str, numero: int):
    k = _chave_alias(alias)
    _ALIAS_ACENTUADO.setdefault(k, numero)
    _ALIAS_SEM_ACENTO.setdefault(_sem_acentos(k), numero)


for _i, (_abrev, _nome, _extras) in enumerate(_NOMES_BIBLIA):
    _n = _i + 1
    for _a in [_abrev, _nome] + _extras:
        _registrar_alias(_a, _n)

# Desempates explícitos no mapa sem acento (colisões conhecidas):
_ALIAS_SEM_ACENTO["jo"] = 43   # 'Jo' sem acento = João (Jó pede acento, ou 'Job')

# Compatibilidade com código antigo que importa estes nomes:
_LIVROS_BIBLIA = [t[0] for t in _NOMES_BIBLIA]
_ABREV_PARA_NUMERO = {abrev.lower(): i + 1 for i, abrev in enumerate(_LIVROS_BIBLIA)}


def _normalizar_prefixo_numerado(livro: str) -> str:
    """'primeira coríntios' -> '1 coríntios'; 'II Timóteo' -> '2 Timóteo';
    '1ª João' -> '1 João'. Só mexe no PREFIXO, nunca no nome do livro."""
    t = livro.strip()
    t = re.sub(r"^(primeir[ao])\b\.?", "1", t, flags=re.IGNORECASE)
    t = re.sub(r"^(segund[ao])\b\.?", "2", t, flags=re.IGNORECASE)
    t = re.sub(r"^(terceir[ao])\b\.?", "3", t, flags=re.IGNORECASE)
    t = re.sub(r"^(iii)\s+", "3 ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(ii)\s+", "2 ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(i)\s+", "1 ", t, flags=re.IGNORECASE)
    t = re.sub(r"^([123])\s*[ªºoa]\b\.?", r"\1", t, flags=re.IGNORECASE)
    return t


def _numero_do_livro(livro: str) -> Optional[int]:
    """Resolve qualquer grafia razoável de um livro bíblico para seu número
    canônico (Gn=1 ... Ap=66). None se não for um livro bíblico."""
    k = _chave_alias(_normalizar_prefixo_numerado(livro))
    n = _ALIAS_ACENTUADO.get(k)
    if n is not None:
        return n
    return _ALIAS_SEM_ACENTO.get(_sem_acentos(k))


# livro (possivelmente com dígito/ordinal na frente e espaços no meio),
# capítulo, e opcionalmente [separador flexível] versículo [- fim].
_RX_CITACAO_BIBLICA = re.compile(
    r"^\s*"
    r"((?:[0-3][ªºoa]?\.?)?\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\.ªº]*?)"   # livro (com ordinal opcional: 1ª, 2º, 1a...)
    r"\s+(\d{1,3})"                                   # capítulo
    r"(?:\s*[:.,]\s*(\d{1,3})"                        # : . ou ,  + versículo
    r"(?:\s*[-–—]\s*(\d{1,3}))?"                      # -fim (intervalo)
    r")?\s*$"
)


def resolver_citacao(texto: str, obras: list[Obra]) -> Optional[dict]:
    """
    Interpreta 'texto' como citação bíblica em QUALQUER grafia razoável:
      'Gn 1:1' | 'genesis 1,1' | 'Gênesis 1.1' | 'salmos 23' (capítulo)
      '1 corintios 13:4-7' (intervalo) | 'primeira coríntios 13,4' ...
    Devolve:
      {"candidatos": [Obra...], "numero": 1, "abrev": "Gn",
       "cap": 1, "v_ini": 1|None, "v_fim": None|int, "coordenada": "1:1"}
    ou None se não for citação bíblica (aí o chamador tenta o fallback
    de obras clássicas).
    """
    m = _RX_CITACAO_BIBLICA.match(texto.strip())
    if not m:
        return None
    livro_bruto, cap_s, v_ini_s, v_fim_s = m.groups()
    numero = _numero_do_livro(livro_bruto)
    if numero is None:
        return None

    candidatos = [o for o in obras if o.id.startswith(f"biblia-{numero:02d}-")]
    if not candidatos:
        return None

    cap = int(cap_s)
    v_ini = int(v_ini_s) if v_ini_s else None
    v_fim = int(v_fim_s) if v_fim_s else None
    if v_ini is not None and v_fim is not None and v_fim < v_ini:
        v_ini, v_fim = v_fim, v_ini  # 'Gn 1:3-1' -> trata como 1-3

    abrev = _NOMES_BIBLIA[numero - 1][0]
    if v_ini is None:
        coordenada = f"{cap}"
    elif v_fim is not None:
        coordenada = f"{cap}:{v_ini}-{v_fim}"
    else:
        coordenada = f"{cap}:{v_ini}"

    # 'ancora' mantida por compatibilidade com chamadores antigos
    ancora = f"^{abrev.lower()}-{cap}-{v_ini if v_ini is not None else 1}"
    return {
        "candidatos": candidatos, "numero": numero, "abrev": abrev,
        "cap": cap, "v_ini": v_ini, "v_fim": v_fim,
        "coordenada": coordenada, "ancora": ancora,
    }


# âncora no fim da linha: ^gn-1-1  (a abreviação pode vir acentuada ou não,
# dependendo de como o arquivo foi gerado — aceitamos as duas)
_RX_ANCORA_FIM = re.compile(r"\^([0-9A-Za-zÀ-ÿ]+)-(\d+)-(\d+)\s*$")


def extrair_citacao_biblica(
    corpo: str, abrev: str, cap: int,
    v_ini: Optional[int] = None, v_fim: Optional[int] = None,
) -> Optional[str]:
    """Extrai do corpo os versículos pedidos, guiado pelas âncoras ^ab-cap-v.
    v_ini=None -> capítulo inteiro. v_fim=None -> um versículo só.
    Tolera âncora acentuada ('^êx-3-14') e sem acento ('^ex-3-14')."""
    chaves = {abrev.lower(), _sem_acentos(abrev.lower())}
    fim = v_fim if v_fim is not None else v_ini
    achados: list[tuple[int, str]] = []
    for linha in corpo.split("\n"):
        m = _RX_ANCORA_FIM.search(linha.rstrip())
        if not m:
            continue
        ab = m.group(1).lower()
        if ab not in chaves and _sem_acentos(ab) not in chaves:
            continue
        c, v = int(m.group(2)), int(m.group(3))
        if c != cap:
            continue
        if v_ini is not None and not (v_ini <= v <= fim):
            continue
        achados.append((v, linha[: m.start()].rstrip()))
    if not achados:
        return None
    achados.sort()
    return "\n".join(t for _, t in achados)


def extrair_trecho_por_ancora(corpo: str, ancora: str, contexto_linhas: int = 0) -> Optional[str]:
    """(mantida por compatibilidade) Acha a linha terminando em '^ancora' e
    devolve só ela (+ contexto, se pedido). O '^ancora' em si é removido do
    texto devolvido — é referência para máquina, não parte da leitura."""
    linhas = corpo.split("\n")
    rx_remover_ancora = re.compile(r"\s\^[a-zA-Z0-9\-_]+\s*$", re.MULTILINE)
    for i, linha in enumerate(linhas):
        if linha.rstrip().endswith(ancora):
            ini = max(0, i - contexto_linhas)
            fim = min(len(linhas), i + contexto_linhas + 1)
            bruto = "\n".join(linhas[ini:fim]).strip()
            return rx_remover_ancora.sub("", bruto)
    return None


# ---------- resolução aproximada de id_obra ----------
def resolver_id_aproximado(consulta: str, obras: list[Obra], n: int = 5) -> tuple[Optional[Obra], list[Obra]]:
    """
    Devolve (obra_exata_ou_unica, sugestões).
      - id exato                       -> (obra, [])
      - id 'quase' (com prefixo de pasta, substring única no id ou título)
                                       -> (obra, [])  [resolvido sozinho]
      - várias possibilidades          -> (None, [candidatas...])
      - nada parecido                  -> (None, [])
    """
    alvo_cru = consulta.strip()
    exata = next((o for o in obras if o.id == alvo_cru), None)
    if exata:
        return exata, []

    # 'Livro/hobbes-leviathan-latin-1668' -> tenta também só o último segmento
    ultimo_segmento = alvo_cru.split("/")[-1].strip()
    if ultimo_segmento != alvo_cru:
        exata = next((o for o in obras if o.id == ultimo_segmento), None)
        if exata:
            return exata, []

    alvo = _sem_acentos(ultimo_segmento.lower())
    if not alvo:
        return None, []

    contem = [
        o for o in obras
        if alvo in _sem_acentos(o.id.lower())
        or alvo in _sem_acentos((o.titulo + " " + o.autor).lower())
    ]
    if len(contem) == 1:
        return contem[0], []
    if 1 < len(contem) <= max(n, 8):
        return None, contem[:n]

    universo: dict[str, Obra] = {}
    for o in obras:
        universo.setdefault(_sem_acentos(o.id.lower()), o)
        universo.setdefault(_sem_acentos(o.titulo.lower()), o)
    proximos = get_close_matches(alvo, list(universo.keys()), n=n, cutoff=0.6)
    sugestoes: list[Obra] = []
    for p in proximos:
        o = universo[p]
        if o not in sugestoes:
            sugestoes.append(o)
    return None, sugestoes


def _limpar_citacao_classica(citacao: str) -> str:
    """'Leviathan cap. 13' -> 'Leviathan'; 'Sofista 216a' -> 'Sofista'.
    Remove coordenadas do fim para sobrar só o provável título."""
    t = citacao.strip()
    t = re.sub(r"\b(cap[íi]tulo|cap|livro|liv|parte|se[cç][aã]o)\b\.?\s*[ivxlcdm\d]*\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[\d:.,;\-–—]+\s*[a-e]?\s*$", "", t)
    return t.strip()


# ---------- lógica compartilhada da ferramenta ler_trecho_exato ----------
# Os DOIS servidores (stdio e HTTP) chamam esta função — a lógica vive num
# lugar só; os servidores viram cascas finas de transporte.
def ferramenta_ler_trecho(cliente: ClientePedraAngular, id_obra: str = "", citacao: str = "") -> str:
    obras = cliente.catalogo()

    # ---- caminho 1: citação natural ----
    if citacao and not id_obra:
        resolvido = resolver_citacao(citacao, obras)
        if resolvido is not None:
            candidatos = resolvido["candidatos"]
            if len(candidatos) > 1:
                linhas = [f"'{citacao}' bate com mais de uma tradução — escolha uma (id_obra):"]
                for c in candidatos:
                    linhas.append(f"  - [{c.id}] {c.titulo}")
                return "\n".join(linhas)
            obra = candidatos[0]
            bruto = cliente.conteudo_bruto(obra.arquivo)
            meta, corpo = separar_frontmatter(bruto)
            trecho = extrair_citacao_biblica(
                corpo, resolvido["abrev"], resolvido["cap"],
                resolvido["v_ini"], resolvido["v_fim"],
            )
            if trecho is None:
                return (f"Obra encontrada ({obra.titulo}), mas a coordenada "
                        f"{resolvido['abrev']} {resolvido['coordenada']} não achou "
                        f"correspondência no texto (capítulo/versículo existe nessa tradução?).")
            return (f"OBRA: {meta.get('title', obra.titulo)}  [{resolvido['abrev']} {resolvido['coordenada']}]\n"
                    f"TRADUÇÃO: {meta.get('translation') or meta.get('translator')}\n"
                    f"FONTE: {meta.get('source')}\n"
                    f"LICENÇA: {meta.get('license', '(ver source)')}\n---\n" + trecho)

        # não é citação bíblica -> fallback: tenta casar com título de obra clássica
        palpite = _limpar_citacao_classica(citacao)
        obra_unica, sugestoes = resolver_id_aproximado(palpite, obras) if palpite else (None, [])
        if obra_unica is not None:
            return ferramenta_ler_trecho(cliente, id_obra=obra_unica.id)
        if sugestoes:
            linhas = [
                f"Não reconheci '{citacao}' como citação bíblica, mas parece "
                f"referir-se a uma destas obras (use ler_trecho_exato com o id):"
            ]
            for o in sugestoes:
                linhas.append(f"  - [{o.id}] {o.titulo} — {o.autor}")
            return "\n".join(linhas)
        return (f"Não reconheci '{citacao}' como citação bíblica (ex.: 'Gn 1:1', "
                f"'gênesis 1,1', 'salmos 23') nem casei com título de obra do catálogo. "
                f"Para obras clássicas, use id_obra (via listar_filhos ou buscar_no_corpus).")

    # ---- caminho 2: por id (agora com resolução aproximada) ----
    if not id_obra:
        return "Informe id_obra ou citacao."
    obra, sugestoes = resolver_id_aproximado(id_obra, obras)
    if obra is None:
        if sugestoes:
            linhas = [f"Nenhuma obra com id exato '{id_obra}'. Você quis dizer:"]
            for o in sugestoes:
                linhas.append(f"  - [{o.id}] {o.titulo} — {o.autor}")
            return "\n".join(linhas)
        return f"Nenhuma obra com id='{id_obra}' no catálogo (nem nada parecido)."
    aviso = "" if obra.id == id_obra.strip() else f"(id aproximado: pedi '{id_obra}', usei '{obra.id}')\n"
    try:
        bruto = cliente.conteudo_bruto(obra.arquivo)
    except Exception as e:
        return f"Erro ao buscar o arquivo: {e}"
    meta, corpo = separar_frontmatter(bruto)
    return (aviso +
            f"OBRA: {meta.get('title', obra.titulo)}\n"
            f"AUTOR: {meta.get('author', obra.autor)}\n"
            f"TRADUÇÃO: {meta.get('translation') or meta.get('translator')}\n"
            f"FONTE: {meta.get('source')}\n"
            f"LICENÇA: {meta.get('license', '(ver source)')}\n---\n" + corpo.strip())
