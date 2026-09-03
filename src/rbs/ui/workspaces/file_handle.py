"""Saving a workspace to a file the user picks, and keeps.

The File System Access API turns export into save: the first save opens a
picker, the handle is remembered in IndexedDB, and every later save overwrites
that same file. Without it people accumulate dated copies in Downloads, lose
track of which is current, and quietly start treating the server as their
storage - which is the one outcome this architecture exists to prevent.

Two constraints shape the implementation:

* ``showSaveFilePicker`` requires a user gesture, and a server round-trip loses
  it. So the picker runs in a client-side ``js_handler`` on the real click, and
  only the *result* is reported back to Python.
* The content is fetched over HTTP rather than pushed through the websocket, so
  an expired proxy session shows up as a redirect the browser can see. A save
  that silently writes a login page to disk is the worst failure this design
  has, and this is what makes it detectable.
"""

from __future__ import annotations

import hashlib
import re

SAVE_SCRIPT = """
(() => {
  if (window.rbsSaveWorkspace) return;
  const DB_NAME = 'rbs-file-handles';
  const STORE = 'handles';

  const openDb = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

  async function withStore(mode, run) {
    const db = await openDb();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        const request = run(tx.objectStore(STORE));
        tx.oncomplete = () => resolve(request ? request.result : undefined);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  }

  const readHandle = (key) =>
    withStore('readonly', (store) => store.get(key)).catch(() => undefined);
  const writeHandle = (key, handle) =>
    withStore('readwrite', (store) => store.put(handle, key)).catch(() => undefined);
  const forgetHandle = (key) =>
    withStore('readwrite', (store) => store.delete(key)).catch(() => undefined);

  async function pickWritable(key, suggestedName, forcePicker) {
    if (!window.showSaveFilePicker) return null;
    let handle = forcePicker ? undefined : await readHandle(key);
    if (handle) {
      const options = { mode: 'readwrite' };
      let permission = await handle.queryPermission(options);
      if (permission !== 'granted') permission = await handle.requestPermission(options);
      if (permission !== 'granted') handle = undefined;
    }
    if (!handle) {
      handle = await window.showSaveFilePicker({
        suggestedName: suggestedName,
        types: [{
          description: 'RBS workspace',
          accept: { 'application/json': ['.rbsc'] },
        }],
      });
      await writeHandle(key, handle);
    }
    return handle;
  }

  function downloadInstead(text, suggestedName) {
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = suggestedName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  window.rbsSaveWorkspace = async (url, suggestedName, key, forcePicker) => {
    let handle = null;
    try {
      handle = await pickWritable(key, suggestedName, forcePicker);
    } catch (error) {
      if (error && error.name === 'AbortError') return 'cancelled';
      handle = null;
    }

    let response;
    try {
      response = await fetch(url, { credentials: 'same-origin', redirect: 'manual' });
    } catch (error) {
      return 'error:network';
    }
    // An expired proxy session answers with a redirect to its own login page.
    // Writing that to the user's file would look exactly like a successful save.
    if (response.type === 'opaqueredirect' || response.status === 0) return 'auth-expired';
    if (!response.ok) return 'error:http-' + response.status;
    if (response.headers.get('X-RBS-Payload') !== 'rbsc') return 'auth-expired';

    const text = await response.text();
    if (!handle) {
      downloadInstead(text, suggestedName);
      return 'downloaded:' + suggestedName;
    }
    try {
      const writable = await handle.createWritable();
      await writable.write(text);
      await writable.close();
      return 'saved:' + (handle.name || suggestedName);
    } catch (error) {
      await forgetHandle(key);
      return 'error:write';
    }
  };
})();
"""

UNSAVED_GUARD_SCRIPT = """
(() => {
  if (window.rbsSetUnsaved) return;
  window.rbsSetUnsaved = (unsaved) => {
    if (window.__rbsUnsavedHandler) {
      window.removeEventListener('beforeunload', window.__rbsUnsavedHandler);
      window.__rbsUnsavedHandler = null;
    }
    if (unsaved) {
      window.__rbsUnsavedHandler = (event) => {
        event.preventDefault();
        event.returnValue = '';
      };
      window.addEventListener('beforeunload', window.__rbsUnsavedHandler);
    }
  };
})();
"""

PAYLOAD_HEADER = "X-RBS-Payload"
PAYLOAD_MARKER = "rbsc"
WORKSPACE_DOWNLOAD_ROUTE = "/rbs/workspace/{workspace_id}/file.rbsc"


def install(ui) -> None:
    """Add the save bridge and the unsaved-changes guard to the page head."""
    ui.add_head_html(f"<script>{SAVE_SCRIPT}</script>")
    ui.add_head_html(f"<script>{UNSAVED_GUARD_SCRIPT}</script>")


def set_unsaved(ui, unsaved: bool) -> None:
    """Arm or clear the browser's leave-the-page warning.

    Fire-and-forget, and skipped entirely without a running loop: NiceGUI
    schedules the call as a background task, so attempting it outside one leaves
    an un-awaited coroutine behind. A missing warning must never break a render.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        ui.run_javascript(f"window.rbsSetUnsaved({'true' if unsaved else 'false'})")
    except Exception:  # noqa: BLE001 - a best-effort guard, never a hard failure
        pass


def handle_key(subject: str, workspace_id: int) -> str:
    """A stable per-user, per-workspace key for the remembered file handle.

    The subject is hashed rather than stored so a shared browser profile does
    not leave one person's identity readable in another's IndexedDB.
    """
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
    return f"rbs:{digest}:{workspace_id}"


def workspace_filename(name: str, academic_year: str) -> str:
    """A filename the user will recognise a year from now."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{name} {academic_year}").strip("-")
    return f"{slug or 'workspace'}.rbsc"


def save_js_handler(url: str, filename: str, key: str, *, force_picker: bool = False) -> str:
    """The click handler that runs in the browser, gesture intact."""
    force = "true" if force_picker else "false"
    return (
        "async () => { const outcome = await window.rbsSaveWorkspace("
        f"{url!r}, {filename!r}, {key!r}, {force}); emit(outcome); }}"
    )


def describe_outcome(outcome: str) -> tuple[str, str]:
    """Turn a save result into a message and a notification level."""
    if outcome.startswith("saved:"):
        return f"Saved to {outcome.split(':', 1)[1]}", "positive"
    if outcome.startswith("downloaded:"):
        return f"Downloaded {outcome.split(':', 1)[1]}", "positive"
    if outcome == "cancelled":
        return "Save cancelled - nothing was written", "info"
    if outcome == "auth-expired":
        return (
            "Your session expired before the file could be saved. Reload the "
            "page to sign in again, then save - nothing was written.",
            "negative",
        )
    if outcome == "error:write":
        return "Could not write to that file. Choose a location again.", "negative"
    if outcome == "error:network":
        return "The connection dropped before the file could be saved.", "negative"
    return f"Save failed ({outcome}). Nothing was written.", "negative"


def saved_successfully(outcome: str) -> bool:
    """Only a completed write counts as the user holding a current file."""
    return outcome.startswith(("saved:", "downloaded:"))
