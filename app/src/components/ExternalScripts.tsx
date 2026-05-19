import { useEffect } from 'react';
import { scripts } from '../generated/scripts';

const CRITICAL_PATTERN = /form_embed|leadconnector|msgsndr/i;

function injectScript(s: (typeof scripts)[number], opts: { async?: boolean; defer?: boolean } = {}) {
  const el = document.createElement('script');
  if (s.external) {
    el.src = s.src;
    if (opts.async ?? true) el.async = true;
    if (opts.defer) el.defer = true;
  } else {
    el.text = s.content;
  }
  document.body.appendChild(el);
}

declare global {
  interface Window {
    __ssmScriptsInjected?: boolean;
  }
}

// HighLevel form_embed.js handles "modify-parent-url" via replaceState, which
// updates the address bar but does NOT navigate. This listener intercepts the
// same message and performs real navigation so conditional form redirects work.
function handleHighLevelRedirect(event: MessageEvent) {
  const data = event.data;
  if (!Array.isArray(data)) return;
  const action = data[0];
  if (action === 'modify-parent-url' && typeof data[1] === 'string' && data[1]) {
    window.location.href = data[1];
  }
}

export default function ExternalScripts() {
  useEffect(() => {
    window.addEventListener('message', handleHighLevelRedirect);
    return () => window.removeEventListener('message', handleHighLevelRedirect);
  }, []);

  useEffect(() => {
    // Guard against React StrictMode double-invocation in dev and against
    // any future re-mount: inject the third-party scripts at most once
    // per page load. Without this, top-level `const`/`var` declarations
    // inside inline scripts blow up with "Identifier already declared".
    if (window.__ssmScriptsInjected) return;
    window.__ssmScriptsInjected = true;

    const critical = scripts.filter((s) => s.external && CRITICAL_PATTERN.test(s.src));
    const rest = scripts.filter((s) => !(s.external && CRITICAL_PATTERN.test(s.src)));

    // Critical: load immediately so iframe forms hydrate ASAP.
    critical.forEach((s) => injectScript(s, { async: true }));

    // Non-critical: defer to idle to keep TTI low.
    const loadRest = () => rest.forEach((s) => injectScript(s, { async: true, defer: true }));
    if ('requestIdleCallback' in window) {
      (window as Window & typeof globalThis).requestIdleCallback(loadRest, { timeout: 2500 });
    } else {
      setTimeout(loadRest, 1500);
    }
  }, []);

  return null;
}
