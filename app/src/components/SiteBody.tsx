import { useEffect, useRef } from 'react';
import Swiper from 'swiper';
import { Autoplay, FreeMode } from 'swiper/modules';
import 'swiper/css';
import 'swiper/css/free-mode';
import { bodyHtml } from '../generated/body.html';

function decodePopupHash(hash: string): { id: string } | null {
  // Format: #elementor-action:action=popup:open&settings=BASE64({"id":"2793","toggle":false})
  // Or URL-encoded: #elementor-action%3Aaction%3Dpopup%3Aopen%26settings%3D...
  try {
    const decoded = decodeURIComponent(hash.replace(/^#/, ''));
    if (!decoded.startsWith('elementor-action:')) return null;
    const params = new URLSearchParams(decoded.replace('elementor-action:', ''));
    const action = params.get('action');
    const settingsB64 = params.get('settings');
    if (action !== 'popup:open' || !settingsB64) return null;
    const settings = JSON.parse(atob(settingsB64));
    if (typeof settings?.id === 'string' || typeof settings?.id === 'number') {
      return { id: String(settings.id) };
    }
  } catch {
    /* fall through */
  }
  return null;
}

function openElementorPopup(root: HTMLElement, popupId: string) {
  const popup = root.querySelector<HTMLElement>(
    `.elementor-location-popup[data-elementor-id="${popupId}"]`,
  );
  if (!popup) return false;

  // Hydrate iframes inside the popup whose src is "about:blank" - resolve from data-lazy-src.
  popup.querySelectorAll<HTMLIFrameElement>('iframe').forEach((f) => {
    const lazy = f.getAttribute('data-lazy-src');
    if (lazy && (f.getAttribute('src') === 'about:blank' || !f.getAttribute('src'))) {
      f.setAttribute('src', lazy);
    }
  });

  let overlay = document.getElementById('app-popup-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'app-popup-overlay';
    document.body.appendChild(overlay);
  }
  overlay.innerHTML = '';
  overlay.className = 'app-popup-overlay app-popup-overlay--open';

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'Fechar');
  closeBtn.className = 'app-popup-close';
  closeBtn.innerHTML = '&times;';
  overlay.appendChild(closeBtn);

  // Move the popup node into the overlay (preserve original location with a placeholder).
  const placeholder = document.createComment(`popup-${popupId}-placeholder`);
  popup.parentNode?.insertBefore(placeholder, popup);
  overlay.appendChild(popup);
  popup.style.display = 'block';

  const close = () => {
    overlay!.classList.remove('app-popup-overlay--open');
    popup.style.display = '';
    placeholder.parentNode?.insertBefore(popup, placeholder);
    placeholder.parentNode?.removeChild(placeholder);
    overlay!.innerHTML = '';
    document.body.classList.remove('app-popup-locked');
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };

  closeBtn.addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', onKey);
  document.body.classList.add('app-popup-locked');

  return true;
}

export default function SiteBody() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;

    const root = ref.current;

    // Native lazy-load for all <img>
    root.querySelectorAll('img').forEach((img) => {
      if (!img.hasAttribute('loading')) img.setAttribute('loading', 'lazy');
      if (!img.hasAttribute('decoding')) img.setAttribute('decoding', 'async');
    });
    const firstImg = root.querySelector('img');
    if (firstImg) {
      firstImg.setAttribute('loading', 'eager');
      firstImg.setAttribute('fetchpriority', 'high');
    }

    // Resolve WP Rocket lazy attrs on non-script elements (iframes, images).
    root.querySelectorAll<HTMLElement>('[data-rocket-src],[data-src],[data-lazy-src]').forEach((el) => {
      const src =
        el.getAttribute('data-rocket-src') ||
        el.getAttribute('data-src') ||
        el.getAttribute('data-lazy-src');
      if (src && el.tagName !== 'SCRIPT') {
        el.setAttribute('src', src);
        el.removeAttribute('data-rocket-src');
        el.removeAttribute('data-src');
        el.removeAttribute('data-lazy-src');
        el.removeAttribute('data-rocket-lazyload');
      }
      // Hydrate lazy srcset on <img>
      const lazySrcset = el.getAttribute('data-lazy-srcset');
      if (lazySrcset && el.tagName === 'IMG') {
        el.setAttribute('srcset', lazySrcset);
        el.removeAttribute('data-lazy-srcset');
      }
      const lazySizes = el.getAttribute('data-lazy-sizes');
      if (lazySizes && el.tagName === 'IMG') {
        el.setAttribute('sizes', lazySizes);
        el.removeAttribute('data-lazy-sizes');
      }
    });

    // Catch any remaining <img> with the WP Rocket SVG placeholder + data-lazy-srcset
    root.querySelectorAll<HTMLImageElement>('img[data-lazy-srcset]').forEach((img) => {
      const srcset = img.getAttribute('data-lazy-srcset');
      if (srcset) {
        img.setAttribute('srcset', srcset);
        img.removeAttribute('data-lazy-srcset');
      }
    });

    // Initialize the real Swiper.js on every nested-carousel widget. The
    // original page uses Swiper v8 with autoplay; we use v11 with the same
    // configuration to match: continuous slide (autoplay.delay=0 + high
    // speed + freeMode) + draggable. swiper-initialized class is added by
    // Swiper itself, which neutralizes the static fallback CSS.
    root.querySelectorAll<HTMLElement>('.elementor-widget-n-carousel .swiper, .e-n-carousel.swiper').forEach((el) => {
      if (el.dataset.ssmSwiper === '1') return;
      el.dataset.ssmSwiper = '1';
      // eslint-disable-next-line @typescript-eslint/no-new
      new Swiper(el, {
        modules: [Autoplay, FreeMode],
        slidesPerView: 'auto',
        spaceBetween: 16,
        loop: true,
        loopAdditionalSlides: 4,
        speed: 6000,
        autoplay: {
          delay: 0,
          disableOnInteraction: false,
          pauseOnMouseEnter: true,
        },
        freeMode: {
          enabled: true,
          momentum: false,
        },
        allowTouchMove: true,
        grabCursor: true,
        a11y: { enabled: false },
      });
    });

    // Make external links open in a new tab safely
    root.querySelectorAll<HTMLAnchorElement>('a[target="_blank"]').forEach((a) => {
      const rel = (a.getAttribute('rel') || '').split(/\s+/);
      for (const r of ['noopener', 'noreferrer']) if (!rel.includes(r)) rel.push(r);
      a.setAttribute('rel', rel.filter(Boolean).join(' '));
    });

    // Elementor popup action: #elementor-action:action=popup:open&settings=BASE64
    const onLinkClick = (e: Event) => {
      const a = (e.target as HTMLElement | null)?.closest?.('a');
      if (!a) return;
      const href = a.getAttribute('href') || '';
      if (!href.includes('elementor-action')) return;
      const decoded = decodePopupHash(href);
      if (!decoded) return;
      e.preventDefault();
      openElementorPopup(root, decoded.id);
    };
    root.addEventListener('click', onLinkClick);

    // Smooth scroll for in-page anchors (skip elementor-action and empty hrefs).
    root.querySelectorAll<HTMLAnchorElement>('a[href^="#"]').forEach((a) => {
      a.addEventListener('click', (e) => {
        const href = a.getAttribute('href') || '';
        if (href === '#' || href.includes('elementor-action')) return;
        const id = decodeURIComponent(href.slice(1));
        const tgt = document.getElementById(id) || document.getElementById(href.slice(1));
        if (tgt) {
          e.preventDefault();
          tgt.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    });

    // Open popup automatically if the URL already has the hash on first paint.
    if (window.location.hash.includes('elementor-action')) {
      const decoded = decodePopupHash(window.location.hash);
      if (decoded) openElementorPopup(root, decoded.id);
    }

    return () => { root.removeEventListener('click', onLinkClick); };
  }, []);

  return <div ref={ref} dangerouslySetInnerHTML={{ __html: bodyHtml }} />;
}
