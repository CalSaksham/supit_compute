const CALENDLY_URL = 'https://calendly.com/madhavsh-wharton/supit-compute-product-demo-madhav-shar-a?hide_gdpr_banner=1&primary_color=2f6b4a';
let loading;

function loadCalendly() {
  if (window.Calendly) return Promise.resolve();
  loading ||= new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = 'https://assets.calendly.com/assets/external/widget.css';
    document.head.append(css);
    const js = document.createElement('script');
    js.src = 'https://assets.calendly.com/assets/external/widget.js';
    js.async = true;
    js.onload = resolve;
    js.onerror = reject;
    document.head.append(js);
  });
  return loading;
}

document.querySelectorAll('[data-book]').forEach(a => {
  a.href = CALENDLY_URL;
  a.target = '_blank';
  a.rel = 'noopener';
  a.addEventListener('pointerenter', () => loadCalendly().catch(() => {}), {once: true});
});

document.addEventListener('click', e => {
  const a = e.target.closest('[data-book]');
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  loadCalendly()
    .then(() => window.Calendly.initPopupWidget({url: CALENDLY_URL}))
    .catch(() => window.open(CALENDLY_URL, '_blank', 'noopener'));
});
