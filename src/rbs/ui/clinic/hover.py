"""Delegated hover/focus cards for the HTML clinic calendar."""

CLINIC_HOVER_SCRIPT = """
(() => {
  let active = null, card = null, timer = null, titled = null, title = '';
  function close() {
    clearTimeout(timer);
    if (active) active.removeAttribute('aria-describedby');
    if (titled) titled.setAttribute('title', title);
    if (card) card.remove();
    active = card = titled = null;
  }
  function show(target) {
    clearTimeout(timer);
    if (active === target) return;
    close();
    const source = target.querySelector('.rbs-clinic-detail-source');
    if (!source) return;
    active = target;
    titled = target.closest('[title]');
    if (titled) { title = titled.title; titled.removeAttribute('title'); }
    card = document.createElement('div');
    card.className = 'rbs-clinic-detail-card';
    const appearance = getComputedStyle(target);
    for (const property of ['--rbs-clinic-site-color', '--rbs-clinic-site-tint']) {
      card.style.setProperty(property, appearance.getPropertyValue(property));
    }
    if (target.classList.contains('admin')) card.classList.add('is-admin');
    card.id = 'rbs-clinic-active-detail';
    card.setAttribute('role', 'tooltip');
    for (const child of source.childNodes) card.append(child.cloneNode(true));
    document.body.append(card);
    target.setAttribute('aria-describedby', card.id);
    const rect = target.getBoundingClientRect();
    const width = card.offsetWidth, height = card.offsetHeight;
    card.style.left = Math.max(8, Math.min(rect.left, innerWidth - width - 8)) + 'px';
    const below = rect.bottom + 4;
    card.style.top = Math.max(8, Math.min(
      below + height <= innerHeight - 8 ? below : rect.top - height - 4,
      innerHeight - height - 8
    )) + 'px';
    card.addEventListener('pointerenter', () => clearTimeout(timer));
    card.addEventListener('pointerleave', () => { timer = setTimeout(close, 120); });
  }
  document.addEventListener('pointerover', event => {
    const target = event.target.closest('.rbs-clinic-person');
    if (target) show(target);
  });
  document.addEventListener('pointerout', event => {
    if (active && active.contains(event.target) && !active.contains(event.relatedTarget)) {
      timer = setTimeout(close, 120);
    }
  });
  document.addEventListener('focusin', event => {
    if (event.target.matches('.rbs-clinic-person')) show(event.target);
  });
  document.addEventListener('focusout', event => {
    if (event.target === active) close();
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  document.addEventListener('scroll', event => {
    if (card && !card.contains(event.target)) close();
  }, true);
  window.addEventListener('resize', close);
  new MutationObserver(() => { if (active && !active.isConnected) close(); })
    .observe(document.documentElement, {childList: true, subtree: true});
})();
"""
