"""
Scraper de assets para migração Vite+React — socialsellingmed.com.br/

Lê o index.html já salvo, extrai todas as URLs de assets locais, baixa preservando
estrutura de pastas em _scrape/assets/, processa CSS buscando url() aninhados,
e gera manifest.json + external_scripts.txt.

Uso:
    python download_assets.py                    # baixa tudo
    python download_assets.py --limit 5          # smoke test: 5 assets aleatórios
    python download_assets.py --limit 5 --seed 42  # smoke test determinístico
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
import urllib3
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
BASE_URL = "https://socialsellingmed.com.br"
BASE_DIR = Path(__file__).parent          # _scrape/
ASSETS_DIR = BASE_DIR / "assets"
MANIFEST_FILE = BASE_DIR / "manifest.json"
EXTERNAL_FILE = BASE_DIR / "external_scripts.txt"
INDEX_HTML = BASE_DIR / "index.html"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 30
MAX_RETRIES = 2
MAX_WORKERS = 8

# Domínios que devem permanecer externos (nunca baixar)
EXTERNAL_PATTERNS = [
    "googletagmanager.com",
    "google-analytics.com",
    "googleadservices.com",
    "doubleclick.net",
    "connect.facebook.net",
    "facebook.com",
    "hotjar.com",
    "clarity.ms",
    "fonts.googleapis.com",     # CSS de webfont → manter externo
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "js.hs-scripts.com",        # HubSpot
    "js.hsforms.net",
    "js.hscta.net",
    "js.hs-banner.com",
    "hs-analytics.net",
    "hstatic.net",
    "snap.licdn.com",
    "analytics.twitter.com",
    "static.ads-twitter.com",
    "cdn.amplitude.com",
    "cdn.segment.com",
    "sentry.io",
    # Específicos deste site (CRM / CAPI / form embed)
    "api.leadconnectorhq.com",
    "link.msgsndr.com",
    "capi-automation.s3.us-east-2.amazonaws.com",
    "wp-rocket.me",
    "api.w.org",
]

# Extensões por categoria
FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".avif"}
CSS_EXTS = {".css"}
JS_EXTS = {".js", ".mjs"}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Lock por caminho destino — evita race condition no Windows ao renomear .part
# quando múltiplos workers encontram a mesma URL normalizada com query strings diferentes
_dest_locks: dict[str, threading.Lock] = {}
_dest_locks_mutex = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class Asset(NamedTuple):
    url: str
    category: str   # css | js | img | fonts | other


def is_external(url: str) -> bool:
    """Retorna True se a URL deve permanecer externa (analytics, pixels, etc.)."""
    for pattern in EXTERNAL_PATTERNS:
        if pattern in url:
            return True
    return False


def normalize_url(url: str, base: str = BASE_URL) -> str | None:
    """Resolve URL relativa para absoluta. Retorna None para data:, javascript:, etc."""
    if not url or url.startswith(("data:", "javascript:", "#", "mailto:", "tel:")):
        return None
    # Protocol-relative
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        url = urljoin(base, url)
    # Remove fragment
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def url_to_local_path(url: str) -> tuple[Path, str]:
    """
    Converte URL → (caminho local em assets/, categoria).
    Ex: https://example.com/wp-content/themes/a/style.css
        → assets/css/wp-content/themes/a/style.css
    """
    parsed = urlparse(url)
    # Remove query string do path para não criar caminhos inválidos
    url_path = parsed.path.lstrip("/")
    suffix = Path(url_path).suffix.lower()

    if suffix in CSS_EXTS:
        cat = "css"
    elif suffix in JS_EXTS:
        cat = "js"
    elif suffix in IMAGE_EXTS:
        cat = "img"
    elif suffix in FONT_EXTS:
        cat = "fonts"
    else:
        cat = "other"

    dest = ASSETS_DIR / cat / url_path
    return dest, cat


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
    return s


def _get_dest_lock(dest: Path) -> threading.Lock:
    """Retorna (ou cria) um lock exclusivo por caminho destino."""
    key = str(dest)
    with _dest_locks_mutex:
        if key not in _dest_locks:
            _dest_locks[key] = threading.Lock()
        return _dest_locks[key]


def _atomic_write(r: requests.Response, dest: Path) -> None:
    """Salva response em dest de forma atômica via arquivo .part com lock por dest."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    lock = _get_dest_lock(dest)
    with lock:
        # Checa de novo dentro do lock — outro worker pode ter terminado
        if dest.exists() and dest.stat().st_size > 0:
            return
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)
        tmp.replace(dest)


def download_one(session: requests.Session, url: str, dest: Path) -> tuple[str, bool, str]:
    """
    Baixa url → dest. Retorna (url, sucesso, mensagem).
    Download atômico via arquivo .part com lock por caminho destino.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return url, True, "cached"

    last_err = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            with session.get(url, timeout=TIMEOUT, stream=True, verify=False) as r:
                if r.status_code == 404:
                    return url, False, "HTTP 404"
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(1)
                    continue
                _atomic_write(r, dest)
            return url, True, "ok"

        except requests.exceptions.Timeout:
            last_err = "timeout"
            time.sleep(2 ** attempt)
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(1)

    return url, False, last_err


def extract_urls_from_css_content(css_text: str, css_url: str) -> list[str]:
    """Extrai todas as url(...) de um arquivo CSS já baixado."""
    found = []
    # Captura url('...'), url("..."), url(...)
    pattern = re.compile(r'url\(\s*[\'"]?([^\'"\)]+)[\'"]?\s*\)', re.IGNORECASE)
    for match in pattern.finditer(css_text):
        raw = match.group(1).strip()
        absolute = normalize_url(raw, base=css_url)
        if absolute:
            found.append(absolute)
    return found


# ---------------------------------------------------------------------------
# Fase 1 — Extração de URLs do HTML
# ---------------------------------------------------------------------------
def extract_assets_from_html(html_text: str) -> tuple[list[Asset], list[str]]:
    """
    Varre o HTML e retorna:
      - assets: lista de Asset(url, categoria) para baixar
      - externals: lista de URLs externas que devem permanecer remotas
    """
    soup = BeautifulSoup(html_text, "lxml")
    seen_urls: set[str] = set()
    assets: list[Asset] = []
    externals: list[str] = []

    def add(raw_url: str | None) -> None:
        if not raw_url:
            return
        url = normalize_url(raw_url)
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        if is_external(url):
            externals.append(url)
            return
        # Só baixa assets do próprio domínio ou CDN do WP
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
            # CDN de terceiros que não são analíticos — decide pelo conteúdo
            # ex: fonts.gstatic.com (woff2) → baixa
            suffix = Path(parsed.path).suffix.lower()
            if suffix not in (FONT_EXTS | IMAGE_EXTS | CSS_EXTS | JS_EXTS):
                externals.append(url)
                return
        _, cat = url_to_local_path(url)
        assets.append(Asset(url=url, category=cat))

    # 1. <link rel="stylesheet"> e preload
    for tag in soup.find_all("link"):
        rel = tag.get("rel", [])
        if isinstance(rel, str):
            rel = [rel]
        href = tag.get("href", "")
        if "stylesheet" in rel or tag.get("as") in ("style", "font", "script", "image"):
            add(href)
        elif "icon" in rel or "shortcut" in rel:
            add(href)
        elif "preload" in rel:
            add(href)

    # 2. <script src="..."> e data-rocket-src (WP Rocket lazy load)
    for tag in soup.find_all("script"):
        add(tag.get("src"))
        add(tag.get("data-rocket-src"))

    # 3. <img src="..." srcset="...">
    for tag in soup.find_all("img"):
        add(tag.get("src"))
        add(tag.get("data-src"))      # lazy load
        add(tag.get("data-lazy-src")) # outro padrão lazy
        srcset = tag.get("srcset") or tag.get("data-srcset", "")
        for part in srcset.split(","):
            raw = part.strip().split()[0] if part.strip() else ""
            add(raw)

    # 4. <source srcset="..."> (picture/video)
    for tag in soup.find_all("source"):
        add(tag.get("src"))
        srcset = tag.get("srcset", "")
        for part in srcset.split(","):
            raw = part.strip().split()[0] if part.strip() else ""
            add(raw)

    # 5. style="background-image:url(...)" inline
    bg_pattern = re.compile(r'url\(\s*[\'"]?([^\'"\)]+)[\'"]?\s*\)', re.IGNORECASE)
    for tag in soup.find_all(style=True):
        for m in bg_pattern.finditer(tag["style"]):
            add(m.group(1).strip())

    # 6. url(...) dentro de blocos <style> inline
    for style_tag in soup.find_all("style"):
        for m in bg_pattern.finditer(style_tag.get_text()):
            add(m.group(1).strip())

    # 7. data-background (Elementor lazy background)
    for tag in soup.find_all(attrs={"data-background": True}):
        add(tag["data-background"])

    # 8. Elementor settings JSON no HTML (background images embutidas em atributos data)
    for tag in soup.find_all(attrs={"data-settings": True}):
        text = tag["data-settings"]
        for m in re.finditer(r'"url"\s*:\s*"(https?://[^"]+)"', text):
            add(m.group(1))

    return assets, externals


# ---------------------------------------------------------------------------
# Fase 2 — Download paralelo
# ---------------------------------------------------------------------------
def download_all(
    assets: list[Asset],
    session: requests.Session,
) -> tuple[dict[str, str], list[dict], list[str]]:
    """
    Baixa todos os assets em paralelo.
    Retorna:
      - url_map: {url_original: caminho_local_relativo}
      - errors: [{url, error}]
      - css_files_downloaded: paths locais dos CSS para segunda passagem
    """
    url_map: dict[str, str] = {}
    errors: list[dict] = []
    css_paths: list[str] = []
    total = len(assets)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_asset = {}
        for asset in assets:
            dest, _ = url_to_local_path(asset.url)
            future = pool.submit(download_one, session, asset.url, dest)
            future_to_asset[future] = asset

        for i, future in enumerate(as_completed(future_to_asset), 1):
            asset = future_to_asset[future]
            url, ok, msg = future.result()
            dest, cat = url_to_local_path(url)
            rel = str(dest.relative_to(BASE_DIR)).replace("\\", "/")
            status_icon = "OK" if ok else "ERRO"
            print(f"  [{i:>3}/{total}] {status_icon}  {cat:5}  {msg:20}  {url[-70:]}")

            if ok:
                url_map[url] = rel
                if cat == "css":
                    css_paths.append(str(dest))
            else:
                errors.append({"url": url, "error": msg})

    return url_map, errors, css_paths


# ---------------------------------------------------------------------------
# Fase 3 — Segunda passagem: url() dentro dos CSS baixados
# ---------------------------------------------------------------------------
def process_css_assets(
    css_paths: list[str],
    session: requests.Session,
    url_map: dict[str, str],
    errors: list[dict],
) -> None:
    """
    Lê cada CSS baixado, extrai url(...), baixa os assets referenciados
    (fontes, imagens de background) e adiciona ao url_map.
    """
    extra_assets: list[tuple[str, str]] = []  # (url, css_url_origin)

    for css_path in css_paths:
        p = Path(css_path)
        if not p.exists():
            continue
        # Reconstrói a URL original a partir do path local
        rel = str(p.relative_to(ASSETS_DIR / "css")).replace("\\", "/")
        css_url = BASE_URL + "/" + rel
        try:
            css_text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for url in extract_urls_from_css_content(css_text, css_url):
            if url not in url_map and not is_external(url):
                extra_assets.append((url, css_url))

    if not extra_assets:
        return

    print(f"\n  [CSS] Encontrei {len(extra_assets)} assets adicionais dentro dos CSS...")
    seen = set()
    futures = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for url, _ in extra_assets:
            if url in seen:
                continue
            seen.add(url)
            dest, _ = url_to_local_path(url)
            future = pool.submit(download_one, session, url, dest)
            futures[future] = url

        for i, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            _, ok, msg = future.result()
            dest, cat = url_to_local_path(url)
            rel = str(dest.relative_to(BASE_DIR)).replace("\\", "/")
            print(f"  [CSS {i:>3}/{len(seen)}] {'OK' if ok else 'ERRO'}  {cat:5}  {url[-70:]}")
            if ok:
                url_map[url] = rel
            else:
                errors.append({"url": url, "error": msg})


# ---------------------------------------------------------------------------
# Fase 4 — Geração dos artefatos de saída
# ---------------------------------------------------------------------------
def write_manifest(url_map: dict[str, str], externals: list[str], errors: list[dict]) -> None:
    data = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "source_html": str(INDEX_HTML),
        "assets_dir": str(ASSETS_DIR),
        "stats": {
            "total_downloaded": len(url_map),
            "total_external": len(externals),
            "total_errors": len(errors),
        },
        "url_map": url_map,
        "external_scripts": externals,
        "errors": errors,
    }
    MANIFEST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  Manifest salvo: {MANIFEST_FILE}")


def write_external_scripts(externals: list[str]) -> None:
    EXTERNAL_FILE.write_text("\n".join(sorted(set(externals))), encoding="utf-8")
    print(f"  Scripts externos: {EXTERNAL_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baixa todos os assets de uma landing page WordPress para migração Vite+React."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Smoke test: baixa apenas N assets aleatórios"
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Seed para seleção aleatória com --limit (reprodutibilidade)"
    )
    args = parser.parse_args()

    # Verifica index.html
    if not INDEX_HTML.exists():
        print(f"ERRO: {INDEX_HTML} não encontrado.")
        return 1

    print("=" * 70)
    print("  Scraper de Assets — socialsellingmed.com.br/")
    print("=" * 70)

    # Lê HTML
    html_text = INDEX_HTML.read_text(encoding="utf-8", errors="replace")
    print(f"\n[1/4] Lendo {INDEX_HTML.name} ({len(html_text):,} chars)...")

    # Extrai URLs
    assets, externals = extract_assets_from_html(html_text)
    print(f"      {len(assets)} assets para baixar, {len(externals)} URLs externas preservadas")

    # Modo --limit: seleciona amostra aleatória
    if args.limit is not None:
        rng = random.Random(args.seed)
        sample = rng.sample(assets, min(args.limit, len(assets)))
        print(f"\n  [SMOKE TEST] Baixando {len(sample)} de {len(assets)} assets (--limit {args.limit})")
        assets = sample

    print(f"\n[2/4] Baixando {len(assets)} assets ({MAX_WORKERS} workers em paralelo)...")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    session = get_session()
    url_map, errors, css_paths = download_all(assets, session)

    # Segunda passagem: url() dentro dos CSS (só no run completo)
    if args.limit is None and css_paths:
        print(f"\n[3/4] Segunda passagem: analisando {len(css_paths)} arquivo(s) CSS...")
        process_css_assets(css_paths, session, url_map, errors)
    else:
        print("\n[3/4] Segunda passagem CSS pulada (--limit ativo ou sem CSS)")

    # Relatório
    print("\n[4/4] Gerando manifest.json e external_scripts.txt...")
    write_manifest(url_map, externals, errors)
    write_external_scripts(externals)

    # Resumo por categoria
    from collections import Counter
    cat_counts: Counter = Counter()
    for url, _ in url_map.items():
        _, cat = url_to_local_path(url)
        cat_counts[cat] += 1

    print("\n" + "=" * 70)
    print("  RESUMO")
    print("=" * 70)
    print(f"  Assets baixados : {len(url_map)}")
    for cat, n in sorted(cat_counts.items()):
        print(f"    {cat:8}: {n}")
    print(f"  Scripts externos: {len(externals)}")
    print(f"  Erros           : {len(errors)}")
    if errors:
        print("\n  Erros detalhados:")
        for e in errors[:10]:
            print(f"    {e['error']:20} {e['url'][-70:]}")
        if len(errors) > 10:
            print(f"    ... e mais {len(errors) - 10} (ver manifest.json)")
    print("=" * 70)

    if args.limit is not None:
        print(f"\n  Smoke test OK. Rode sem --limit para baixar todos os {len(assets)} assets.")
    else:
        print(f"\n  Concluido. Assets em: {ASSETS_DIR}")
        print(f"  Mapeamento completo: {MANIFEST_FILE}")

    return 0 if not errors or len(url_map) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
