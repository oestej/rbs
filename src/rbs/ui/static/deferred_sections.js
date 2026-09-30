// This is an interaction quiet period, not a visual transition duration.
const QUIET_PERIOD_MS = 200;

export default {
  template: '<span hidden aria-hidden="true"></span>',
  props: { pending: Boolean, generation: Number },
  data() {
    return { running: false, inFlight: false, pointerDown: false, timer: null, idle: null, frame: null };
  },
  mounted() { this.start(); },
  activated() { this.start(); },
  deactivated() { this.stop(); },
  beforeUnmount() { this.stop(); },
  watch: {
    pending() { this.schedule(); },
    generation() {
      this.inFlight = false;
      this.schedule();
    },
  },
  methods: {
    start() {
      if (this.running) return;
      this.running = true;
      for (const event of ['keydown', 'input', 'focusin', 'focusout', 'scroll', 'visibilitychange']) {
        document.addEventListener(event, this.schedule, { capture: true, passive: true });
      }
      document.addEventListener('pointerdown', this.press, { capture: true, passive: true });
      document.addEventListener('pointerup', this.release, { capture: true, passive: true });
      document.addEventListener('pointercancel', this.release, { capture: true, passive: true });
      window.addEventListener('blur', this.release);
      this.schedule();
    },
    stop() {
      this.running = false;
      this.inFlight = false;
      this.pointerDown = false;
      this.cancelScheduled();
      for (const event of ['keydown', 'input', 'focusin', 'focusout', 'scroll', 'visibilitychange']) {
        document.removeEventListener(event, this.schedule, true);
      }
      document.removeEventListener('pointerdown', this.press, true);
      document.removeEventListener('pointerup', this.release, true);
      document.removeEventListener('pointercancel', this.release, true);
      window.removeEventListener('blur', this.release);
    },
    press() {
      this.pointerDown = true;
      this.schedule();
    },
    release() {
      this.pointerDown = false;
      this.schedule();
    },
    cancelScheduled() {
      if (this.timer !== null) window.clearTimeout(this.timer);
      if (this.idle !== null) window.cancelIdleCallback(this.idle);
      if (this.frame !== null) window.cancelAnimationFrame(this.frame);
      this.timer = this.idle = this.frame = null;
    },
    busy() {
      if (!this.running || document.hidden || this.pointerDown || !this.$el.isConnected) return true;
      const page = this.$el.closest('.rbs-page-shell') || this.$el.parentElement;
      if (!page?.getClientRects().length) return true;
      const editor = document.activeElement;
      if (editor?.matches('input, textarea, select, [role="textbox"], [role="combobox"]')
          || editor?.isContentEditable) return true;
      return [...document.querySelectorAll(
        '.q-dialog, .q-menu, [role="dialog"], [role="menu"], #rbs-loading-screen:not(.is-ready)',
      )].some((element) => element.getClientRects().length > 0);
    },
    schedule() {
      this.cancelScheduled();
      if (!this.running || !this.pending || this.inFlight) return;
      // A timer lets the visible section paint, and each interaction restarts
      // the quiet period. Focused editors and open dialogs keep it paused.
      this.timer = window.setTimeout(() => {
        this.timer = null;
        if (this.busy()) {
          this.schedule();
          return;
        }
        const ready = (deadline) => {
          this.idle = null;
          if (this.busy() || (deadline && deadline.timeRemaining() <= 0)) {
            this.schedule();
            return;
          }
          this.inFlight = true;
          this.$emit('idle', this.generation);
        };
        if (window.requestIdleCallback) {
          this.idle = window.requestIdleCallback(ready);
        } else {
          // WebKit versions without idle callbacks still yield a frame and a
          // separate task before asking the server to build one section.
          this.frame = window.requestAnimationFrame(() => {
            this.frame = null;
            this.timer = window.setTimeout(() => {
              this.timer = null;
              ready();
            }, 0);
          });
        }
      }, QUIET_PERIOD_MS);
    },
  },
};
