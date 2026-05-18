// Processes scraped HTML into clean body markup + bundled CSS for Vite.
// - Extracts <body> content
// - Rewrites absolute URLs (socialsellingmed.com.br/wp-content/...) to /assets/...
// - Concatenates all local CSS files into a single bundle with rewritten url()
// - Copies _scrape/assets/ to public/assets/
// - Extracts inline <script> tags from body into a generated TS module
// - Captures key meta/SEO hints

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(__dirname, '..');                 // socialsellingmed/app/
const PROJECT_ROOT = path.resolve(APP_ROOT, '..');              // socialsellingmed/
const SCRAPE = path.join(PROJECT_ROOT, '_scrape');              // socialsellingmed/_scrape/
const PUBLIC = path.join(APP_ROOT, 'public');
const GEN = path.join(APP_ROOT, 'src', 'generated');

const SITE_ORIGIN = 'https://socialsellingmed.com.br';

if (!fs.existsSync(SCRAPE)) {
  console.error('[process-html] _scrape/ not found at', SCRAPE);
  process.exit(1);
}

fs.mkdirSync(GEN, { recursive: true });
fs.mkdirSync(path.join(PUBLIC, 'assets'), { recursive: true });
fs.mkdirSync(path.join(APP_ROOT, 'src', 'styles'), { recursive: true });

const manifest = JSON.parse(fs.readFileSync(path.join(SCRAPE, 'manifest.json'), 'utf8'));
const html = fs.readFileSync(path.join(SCRAPE, 'index.html'), 'utf8');

// 1) Build URL -> local path map (absolute URLs and relative paths)
const urlMap = new Map();
for (const [origUrl, localPath] of Object.entries(manifest.url_map)) {
  const publicPath = '/' + localPath.replace(/\\/g, '/');
  urlMap.set(origUrl, publicPath);
  try {
    const u = new URL(origUrl);
    urlMap.set(`${u.origin}${u.pathname}`, publicPath);
  } catch {}
}

// 2) Copy assets to public/
function copyDir(src, dst) {
  if (!fs.existsSync(src)) return;
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}
console.log('[process-html] Copying assets to public/assets/...');
copyDir(path.join(SCRAPE, 'assets'), path.join(PUBLIC, 'assets'));

// 3) Rewrite URL helper
function rewriteUrl(url) {
  if (!url) return url;
  url = url.trim().replace(/^['"]|['"]$/g, '');
  if (urlMap.has(url)) return urlMap.get(url);
  const noQuery = url.split('?')[0].split('#')[0];
  if (urlMap.has(noQuery)) return urlMap.get(noQuery);
  if (url.startsWith(SITE_ORIGIN)) {
    const pathOnly = url.substring(SITE_ORIGIN.length).split('?')[0].split('#')[0];
    for (const [k, v] of urlMap.entries()) {
      if (k.endsWith(pathOnly)) return v;
    }
  }
  return url;
}

function rewriteContent(content) {
  return content
    .replace(/(href|src|data-src|data-rocket-src|data-lazy-src|data-original|poster)=(["'])([^"']+)\2/gi, (_, attr, q, val) => {
      if (val.startsWith('data:') || val.startsWith('mailto:') || val.startsWith('tel:') || val.startsWith('#')) {
        return `${attr}=${q}${val}${q}`;
      }
      return `${attr}=${q}${rewriteUrl(val)}${q}`;
    })
    .replace(/(srcset|data-lazy-srcset)=(["'])([^"']+)\2/gi, (_, attr, q, val) => {
      const rewritten = val.split(',').map(part => {
        const m = part.trim().match(/^(\S+)(\s+\S+)?$/);
        if (!m) return part;
        return rewriteUrl(m[1]) + (m[2] || '');
      }).join(', ');
      return `${attr}=${q}${rewritten}${q}`;
    })
    .replace(/url\((['"]?)([^)'"]+)\1\)/gi, (_, q, val) => `url(${q}${rewriteUrl(val)}${q})`);
}

// 4) Extract body content
function extractBody(src) {
  const bodyMatch = src.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  if (!bodyMatch) throw new Error('No <body> found');
  const bodyAttrs = src.match(/<body([^>]*)>/i)[1];
  let body = bodyMatch[1];

  // Drop <noscript> blocks - GTM noscript is already in app/index.html shell
  body = body.replace(/<noscript>[\s\S]*?<\/noscript>/gi, '');

  // Extract <script> tags from body
  const scripts = [];
  body = body.replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (_full, attrs, content) => {
    // WP Rocket deferred scripts: type="rocketlazyloadscript" + data-rocket-src
    const isLazy = /type\s*=\s*["']?(?:[^"']*\/)?rocketlazyloadscript/i.test(attrs);
    const rocketSrc = attrs.match(/data-rocket-src=["']([^"']+)["']/i)?.[1];
    if (isLazy && rocketSrc) {
      const cleaned = attrs
        .replace(/type\s*=\s*["'][^"']*rocketlazyloadscript[^"']*["']/i, '')
        .replace(/data-rocket-src=["'][^"']+["']/i, `src="${rocketSrc}"`)
        .trim();
      scripts.push({ attrs: cleaned, content });
      return '';
    }
    scripts.push({ attrs: attrs.trim(), content });
    return '';
  });

  return { bodyAttrs: bodyAttrs.trim(), body: rewriteContent(body), scripts };
}

// 5) Build CSS bundle from all local CSS files
function buildCssBundle() {
  const cssEntries = Object.entries(manifest.url_map).filter(([, p]) => p.startsWith('assets/css/'));
  let bundle = '/* Bundled CSS - generated by scripts/process-html.mjs */\n';
  for (const [, relPath] of cssEntries) {
    const abs = path.join(SCRAPE, relPath);
    if (!fs.existsSync(abs)) continue;
    let css = fs.readFileSync(abs, 'utf8');
    const cssDir = path.posix.dirname('/' + relPath.replace(/\\/g, '/'));
    css = css.replace(/url\((['"]?)([^)'"]+)\1\)/gi, (_, q, val) => {
      if (val.startsWith('data:') || val.startsWith('http')) return `url(${q}${rewriteUrl(val)}${q})`;
      const resolved = path.posix.normalize(cssDir + '/' + val.split('?')[0].split('#')[0]);
      const candidate = path.join(PUBLIC, resolved);
      if (fs.existsSync(candidate)) return `url(${q}${resolved}${q})`;
      const fname = path.posix.basename(val.split('?')[0]);
      for (const [, mPath] of Object.entries(manifest.url_map)) {
        if (mPath.endsWith(fname)) return `url(${q}/${mPath.replace(/\\/g, '/')}${q})`;
      }
      return `url(${q}${val}${q})`;
    });
    bundle += `\n/* === ${relPath} === */\n` + css + '\n';
  }
  const styleRe = /<style[^>]*>([\s\S]*?)<\/style>/gi;
  for (const m of html.matchAll(styleRe)) {
    bundle += '\n/* === inline <style> === */\n' + rewriteContent(m[1]) + '\n';
  }
  return bundle;
}

const { bodyAttrs, body, scripts } = extractBody(html);
const cssBundle = buildCssBundle();

// 6) Filter scripts - keep ONLY what's needed for the static clone to work.
// Everything WordPress / Elementor / WP Rocket related is dropped because
// their dependencies (wp.i18n, jquery, elementor frontend, etc.) are not
// loaded. Keep only:
//   - form_embed.js (LeadConnector inline iframe forms)
function filterScripts(scripts) {
  const out = [];
  for (const s of scripts) {
    // Drop non-JavaScript script blocks (JSON-LD, speculationrules, etc.)
    if (/type\s*=\s*["'](?:application\/ld\+json|application\/json|speculationrules)["']/i.test(s.attrs)) continue;

    const srcM = s.attrs.match(/src=["']([^"']+)["']/i);
    const dataRocketSrcM = s.attrs.match(/data-rocket-src=["']([^"']+)["']/i);
    const src = srcM?.[1] || dataRocketSrcM?.[1];
    if (src) {
      // Keep form_embed.js - required for LeadConnector inline iframe forms.
      const isForm = /form_embed|leadconnector|msgsndr/i.test(src);
      // Skip WP/Elementor/jQuery/WPRocket noise
      const isNoise = /jquery|elementor|happy-elementor|pro-elements|wp-rocket|wp-includes|wp-content\/(plugins|themes)|webpack-pro|frontend-modules|hello-frontend|gtm4wp|gtag\/js|googletagmanager|swiper/i.test(src);
      if (isNoise && !isForm) continue;
      out.push({ external: true, src: rewriteUrl(src), attrs: s.attrs });
    } else {
      const content = s.content.trim();
      if (!content) continue;
      // Drop inline scripts that depend on globals we don't ship, or that
      // declare top-level identifiers (which break under React StrictMode
      // double-invocation when re-injected).
      const inlineNoise = [
        /\bwp\.i18n\b/,                          // depends on wp global
        /\bwp\.\w+\(/,                            // any wp.* call
        /RocketBrowserCompatibilityChecker/,
        /RocketPreloadLinksConfig/,
        /RocketLazyLoadScripts/,
        /\bHappyLocalize\b/,
        /elementorFrontendConfig/,
        /ElementorProFrontendConfig/,
        /lazyLoadOptions/,
        /lazyloadRunObserver/,
        /wp-emoji/,
        /gtm4wp_datalayer/,
        /\bgtm\.start\b/,
        /dataLayer_content/,
        /\bconst\s+utms\b/,
        /\bvar\s+utms\b/,
        /getParameterByName/,                     // UTM helper - we don't have forms here
      ];
      if (inlineNoise.some((re) => re.test(content))) continue;
      // Drop huge minified blobs (Rocket bootstrap, etc.)
      if (content.length > 4000) continue;
      out.push({ external: false, content, attrs: s.attrs });
    }
  }
  return out;
}

const cleanScripts = filterScripts(scripts);

// 7) Write outputs
fs.writeFileSync(path.join(GEN, 'body.html.ts'), `// Auto-generated by scripts/process-html.mjs - do not edit.
export const bodyHtml = ${JSON.stringify(body)};
export const bodyAttrs = ${JSON.stringify(bodyAttrs)};
`);

fs.writeFileSync(path.join(GEN, 'scripts.ts'), `// Auto-generated by scripts/process-html.mjs - do not edit.
export interface ExternalScript { external: true; src: string; attrs: string }
export interface InlineScript { external: false; content: string; attrs: string }
export type SiteScript = ExternalScript | InlineScript;
export const scripts: SiteScript[] = ${JSON.stringify(cleanScripts, null, 2)};
`);

fs.writeFileSync(path.join(APP_ROOT, 'src', 'styles', 'site.css'), cssBundle);

console.log('[process-html] Done.');
console.log(`  body length : ${body.length} chars`);
console.log(`  scripts kept: ${cleanScripts.length} (external: ${cleanScripts.filter(s => s.external).length})`);
console.log(`  css bundle  : ${(cssBundle.length / 1024).toFixed(1)} KB`);
