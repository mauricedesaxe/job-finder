(function () {
  var container = document.getElementById("latency-per-day-chart");
  var dataElement = document.getElementById("latency-per-day-data");
  if (!container || !dataElement || typeof frappe === "undefined") return;
  var data = JSON.parse(dataElement.textContent);
  var styles = getComputedStyle(document.documentElement);
  function cssColor(name, fallback) {
    return styles.getPropertyValue(name).trim() || fallback;
  }
  var colors = [
    cssColor("--acid", "#dfff00"),
    cssColor("--focus", "#315cff"),
    cssColor("--caution", "#ffd86b"),
    cssColor("--muted", "#5d5b54")
  ];
  new frappe.Chart(container, {
    type: "bar",
    height: 180,
    data: { labels: data.labels, datasets: data.datasets },
    colors: colors,
    barOptions: { spaceRatio: 0.25 },
    tooltipOptions: {
      formatTooltipX: function (label) {
        return data.details[data.labels.indexOf(label)] || label;
      },
      formatTooltipY: function (value) {
        if (value === null || value === undefined || value === 0) return "no calls";
        return Number(value).toLocaleString("en-US") + " ms";
      }
    }
  });
})();
