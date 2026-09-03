"""Static branding and page-chrome constants for the RBS UI."""

from __future__ import annotations

import hashlib
from pathlib import Path

from rbs.ui.visual_tokens import DANGER, SUCCESS, WARNING

STATIC_DIR = Path(__file__).with_name("static")

STATIC_URL = "/rbs-static"

FAVICON_URL = (
    f"{STATIC_URL}/favicon.svg?v="
    f"{hashlib.sha256((STATIC_DIR / 'favicon.svg').read_bytes()).hexdigest()[:12]}"
)

WORDMARK_URL = (
    f"{STATIC_URL}/wordmark.svg?v="
    f"{hashlib.sha256((STATIC_DIR / 'wordmark.svg').read_bytes()).hexdigest()[:12]}"
)

RECONNECT_BRANDING_HTML = f'<style>:root {{ --rbs-reconnect-mark: url("{WORDMARK_URL}"); }}</style>'

ABOUT_COPYRIGHT_NOTICE = "Copyright © 2026 Jason Mitchell."

ABOUT_LICENSE_NOTICE = "Licensed under the Open Software License version 3.0 (OSL-3.0)."

SUCCESS_COLOR = SUCCESS

DANGER_COLOR = DANGER

WARNING_COLOR = WARNING

EMPTY_WORKSPACE_ARROW = """
<svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true" focusable="false">
  <defs>
    <marker id="rbs-empty-arrowhead" markerWidth="8" markerHeight="8"
            refX="7.2" refY="4" orient="auto" markerUnits="strokeWidth">
      <path d="M 0 0 L 8 4 L 0 8 z" fill="currentColor"></path>
    </marker>
  </defs>
  <path class="rbs-empty-workspace-arrow-path"
        fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" vector-effect="non-scaling-stroke"
        marker-end="url(#rbs-empty-arrowhead)"></path>
</svg>
"""

STYLESHEET_URLS = tuple(
    f"{STATIC_URL}/{filename}?v="
    f"{hashlib.sha256((STATIC_DIR / filename).read_bytes()).hexdigest()[:12]}"
    for filename in ("tokens.css", "app.css", "grid.css", "clinic.css")
)

LOADING_SCREEN_HTML = f"""
<div id="rbs-loading-screen" class="rbs-loading-screen" role="status"
     aria-live="polite" aria-label="Loading RBS">
  <div class="rbs-loading-card rbs-branded-dialog">
    <img class="rbs-dialog-wordmark rbs-loading-wordmark" src="{WORDMARK_URL}" alt="RBS">
    <div class="rbs-loading-message">Preparing your workspace…</div>
    <div class="rbs-spinner-status rbs-loading-status">
      <div class="rbs-loading-spinner" aria-hidden="true"></div>
      <div class="rbs-elapsed-time rbs-loading-elapsed" aria-hidden="true">0:00.0</div>
    </div>
  </div>
</div>
"""

SPINNER_ELAPSED_SCRIPT = """
(() => {
  const timerKey = '__rbsElapsedTimer';
  const labelKey = '__rbsElapsedLabel';

  const formatElapsed = (elapsedMilliseconds) => {
    const totalTenths = Math.max(0, Math.floor(elapsedMilliseconds / 100));
    const minutes = Math.floor(totalTenths / 600);
    const seconds = Math.floor((totalTenths % 600) / 10);
    const tenths = totalTenths % 10;
    return `${minutes}:${String(seconds).padStart(2, '0')}.${tenths}`;
  };

  const stopTimer = (root) => {
    if (!root || root[timerKey] === undefined) return;
    window.clearInterval(root[timerKey]);
    delete root[timerKey];
    delete root[labelKey];
  };

  const startTimer = (root, label, isActive) => {
    if (!root || !label) return;
    if (root[timerKey] !== undefined && root[labelKey] === label) return;
    stopTimer(root);

    const startedAt = window.performance.now();
    const update = () => {
      if (!root.isConnected || !label.isConnected || !isActive()) {
        stopTimer(root);
        return;
      }
      const value = formatElapsed(window.performance.now() - startedAt);
      if (label.childNodes.length === 1 && label.firstChild?.nodeType === Node.TEXT_NODE) {
        label.firstChild.nodeValue = value;
      } else {
        label.textContent = value;
      }
    };

    root[labelKey] = label;
    root[timerKey] = window.setInterval(update, 100);
    update();
  };

  const syncTimers = () => {
    const loading = document.getElementById('rbs-loading-screen');
    if (loading && !loading.classList.contains('is-ready')) {
      startTimer(
        loading,
        loading.querySelector('.rbs-loading-elapsed'),
        () => !loading.classList.contains('is-ready'),
      );
    } else {
      stopTimer(loading);
    }

    const popup = document.querySelector('#popup.nicegui-error-popup');
    if (!popup) return;
    if (popup.getAttribute('aria-hidden') !== 'false') {
      stopTimer(popup);
      return;
    }

    const detail = popup.querySelector(':scope > span:last-child');
    if (!detail) return;
    let status = detail.querySelector('.rbs-reconnect-status');
    if (!status) {
      status = document.createElement('span');
      status.className = 'rbs-spinner-status rbs-reconnect-status';
      const spinner = document.createElement('span');
      spinner.className = 'rbs-reconnect-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      status.append(spinner);
      detail.append(status);
    }
    let label = status.querySelector('.rbs-reconnect-elapsed');
    if (!label) {
      label = document.createElement('span');
      label.className = 'rbs-elapsed-time rbs-reconnect-elapsed';
      label.setAttribute('aria-hidden', 'true');
      label.textContent = '0:00.0';
      status.append(label);
    }
    startTimer(
      popup,
      label,
      () => popup.getAttribute('aria-hidden') === 'false',
    );
  };

  let frame = 0;
  const scheduleSync = () => {
    window.cancelAnimationFrame(frame);
    frame = window.requestAnimationFrame(syncTimers);
  };
  const start = () => {
    new MutationObserver(scheduleSync).observe(document.body, {
      attributes: true,
      attributeFilter: ['aria-hidden', 'class'],
      childList: true,
      subtree: true,
    });
    syncTimers();
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once: true});
  } else {
    start();
  }
})()
"""


def dialog_wordmark():
    """Render the shared wordmark used by branded RBS dialogs."""
    from nicegui import ui

    return ui.image(WORDMARK_URL).props("fit=contain").classes("rbs-dialog-wordmark")


DISMISS_LOADING_SCREEN_SCRIPT = """
(() => {
  const screen = document.getElementById('rbs-loading-screen');
  if (!screen) return true;
  screen.classList.add('is-ready');
  window.setTimeout(() => screen.remove(), 200);
  return true;
})()
"""

BLOCK_LABEL_FIT_SCRIPT = """
(() => {
  let frame = 0;
  const layoutEmptyWorkspaceArrow = () => {
    const arrow = document.querySelector('.rbs-empty-workspace-arrow');
    const copy = document.querySelector('.rbs-empty-workspace-copy');
    const controls = document.querySelector('.rbs-empty-workspace-primary-actions')
      || document.querySelector('.rbs-empty-workspace-controls');
    const svg = arrow?.querySelector('svg');
    const path = arrow?.querySelector('.rbs-empty-workspace-arrow-path');
    if (!arrow || !copy || !controls || !svg || !path) return;

    const copyRect = copy.getBoundingClientRect();
    const controlsRect = controls.getBoundingClientRect();
    const startX = copyRect.right + 16;
    const startY = copyRect.top + copyRect.height / 2;
    const endX = controlsRect.left + controlsRect.width / 2;
    const endY = controlsRect.bottom + 12;
    const horizontalRoom = endX - startX;
    const verticalRoom = startY - endY;

    if (horizontalRoom < 64 || verticalRoom < 140) {
      arrow.classList.remove('is-laid-out');
      path.removeAttribute('d');
      return;
    }

    const firstControlX = startX + horizontalRoom * 0.36;
    const secondControlY = endY + verticalRoom * 0.30;
    svg.setAttribute('viewBox', `0 0 ${window.innerWidth} ${window.innerHeight}`);
    path.setAttribute(
      'd',
      `M ${startX.toFixed(1)} ${startY.toFixed(1)} ` +
      `C ${firstControlX.toFixed(1)} ${startY.toFixed(1)}, ` +
      `${endX.toFixed(1)} ${secondControlY.toFixed(1)}, ` +
      `${endX.toFixed(1)} ${endY.toFixed(1)}`,
    );
    arrow.classList.add('is-laid-out');
  };
  const fitLabels = () => {
    document.querySelectorAll('.rbs-block-name').forEach((label) => {
      label.classList.remove('is-code');
      label.classList.toggle('is-code', label.scrollWidth > label.clientWidth + 1);
    });
    layoutEmptyWorkspaceArrow();
  };
  const scheduleFit = () => {
    window.cancelAnimationFrame(frame);
    frame = window.requestAnimationFrame(fitLabels);
  };
  const start = () => {
    new MutationObserver(scheduleFit).observe(document.body, {
      childList: true,
      subtree: true,
    });
    window.addEventListener('resize', scheduleFit);
    if (document.fonts) document.fonts.ready.then(scheduleFit);
    scheduleFit();
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once: true});
  } else {
    start();
  }
})()
"""
