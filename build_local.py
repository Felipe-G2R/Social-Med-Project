"""
Gera uma versão local navegável a partir de _scrape/.

- Copia _scrape/assets/  ->  ./assets/
- Reescreve url() dentro de cada CSS apontando para /assets/...
- Reescreve URLs absolutas do HTML (src, href, srcset, data-rocket-src, style="url(...)")
  para paths absolutos servidos pelo servidor local (/assets/...)
- Salva ./index.html pronto para servir com qualquer http server estatico
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SCRAPE_DIR = ROOT / "_scrape"
MANIFEST = SCRAPE_DIR / "manifest.json"
SRC_HTML = SCRAPE_DIR / "index.html"
SRC_ASSETS = SCRAPE_DIR / "assets"
OUT_HTML = ROOT / "index.html"
OUT_ASSETS = ROOT / "assets"

BASE_URL = "https://socialsellingmed.com.br"

URL_PAT = re.compile(r'url\(\s*([\'"]?)([^\'"\)]+)\1\s*\)', re.IGNORECASE)


def normalize_url(url: str, base: str = BASE_URL) -> str | None:
    if not url or url.startswith(("data:", "javascript:", "#", "mailto:", "tel:")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        url = urljoin(base, url)
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    url_map: dict[str, str] = manifest["url_map"]
    # URL absoluta -> path local absoluto servido (ex: "/assets/img/...")
    local_paths: dict[str, str] = {
        url: "/" + rel.replace("\\", "/") for url, rel in url_map.items()
    }
    print(f"[1/3] url_map: {len(local_paths)} entradas")

    # 1) Copia assets
    if OUT_ASSETS.exists():
        shutil.rmtree(OUT_ASSETS)
    shutil.copytree(SRC_ASSETS, OUT_ASSETS)
    print(f"[2/3] Assets copiados: {OUT_ASSETS}")

    # 2) Reescreve url() dentro dos CSS (in-place no destino)
    css_root = OUT_ASSETS / "css"
    rewritten = 0
    for css_file in css_root.rglob("*.css"):
        rel = css_file.relative_to(css_root)
        css_url = BASE_URL + "/" + str(rel).replace("\\", "/")
        text = css_file.read_text(encoding="utf-8", errors="replace")

        def repl(m: re.Match) -> str:
            quote, raw = m.group(1), m.group(2).strip()
            absolute = normalize_url(raw, base=css_url)
            if absolute and absolute in local_paths:
                return f"url({quote}{local_paths[absolute]}{quote})"
            return m.group(0)

        new_text = URL_PAT.sub(repl, text)
        if new_text != text:
            css_file.write_text(new_text, encoding="utf-8")
            rewritten += 1
    print(f"      CSS reescritos: {rewritten}")

    # 3) Reescreve HTML
    html = SRC_HTML.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")

    def rewrite_attr(tag, attr: str) -> None:
        val = tag.get(attr)
        if not val:
            return
        absolute = normalize_url(val)
        if absolute and absolute in local_paths:
            tag[attr] = local_paths[absolute]

    def rewrite_srcset(tag, attr: str) -> None:
        val = tag.get(attr)
        if not val:
            return
        new_parts = []
        for part in val.split(","):
            part = part.strip()
            if not part:
                continue
            sub = part.split()
            absolute = normalize_url(sub[0])
            if absolute and absolute in local_paths:
                sub[0] = local_paths[absolute]
            new_parts.append(" ".join(sub))
        tag[attr] = ", ".join(new_parts)

    for tag in soup.find_all("link"):
        rewrite_attr(tag, "href")
    for tag in soup.find_all("script"):
        rewrite_attr(tag, "src")
        rewrite_attr(tag, "data-rocket-src")
    for tag in soup.find_all("img"):
        for a in ("src", "data-src", "data-lazy-src"):
            rewrite_attr(tag, a)
        rewrite_srcset(tag, "srcset")
        rewrite_srcset(tag, "data-srcset")
    for tag in soup.find_all("source"):
        rewrite_attr(tag, "src")
        rewrite_srcset(tag, "srcset")
    for tag in soup.find_all(attrs={"data-background": True}):
        rewrite_attr(tag, "data-background")

    # style="background:url(...)"
    for tag in soup.find_all(style=True):
        def repl_inline(m: re.Match) -> str:
            quote, raw = m.group(1), m.group(2).strip()
            absolute = normalize_url(raw)
            if absolute and absolute in local_paths:
                return f"url({quote}{local_paths[absolute]}{quote})"
            return m.group(0)
        tag["style"] = URL_PAT.sub(repl_inline, tag["style"])

    # <style> blocks
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            def repl_style(m: re.Match) -> str:
                quote, raw = m.group(1), m.group(2).strip()
                absolute = normalize_url(raw)
                if absolute and absolute in local_paths:
                    return f"url({quote}{local_paths[absolute]}{quote})"
                return m.group(0)
            new_text = URL_PAT.sub(repl_style, style_tag.string)
            style_tag.string.replace_with(new_text)

    OUT_HTML.write_text(str(soup), encoding="utf-8")
    print(f"[3/3] HTML salvo: {OUT_HTML}")
    print("\nPronto. Rode:  python -m http.server 8765")
    print("E acesse:      http://localhost:8765/")


if __name__ == "__main__":
    main()
