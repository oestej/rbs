export default {
  template: `<q-btn-toggle v-model="selected" :options="options"
    @update:model-value="select" />`,
  props: {
    modelValue: String,
    panel_ids: Object,
    legend_id: String,
    legends: Object,
  },
  data() {
    return {
      selected: this.modelValue,
      options: [
        { label: "Residents", value: "residents" },
        { label: "Attendings", value: "attendings" },
      ],
    };
  },
  watch: {
    modelValue(value) {
      this.selected = value;
    },
  },
  methods: {
    select(value) {
      const selected = document.getElementById(this.panel_ids[value]);
      // An unprepared view still loads through the server; keep the current calendar meanwhile.
      if (selected?.firstElementChild) {
        for (const [name, id] of Object.entries(this.panel_ids)) {
          const panel = document.getElementById(id);
          panel?.classList.toggle("is-inactive", name !== value);
          panel?.toggleAttribute("inert", name !== value);
        }
        const legend = document.getElementById(this.legend_id);
        if (legend) legend.innerHTML = this.legends[value];
      }
      this.$emit("update:modelValue", value);
    },
  },
};
