(function () {
  var container = document.getElementById("spend-per-day-chart");
  var dataElement = document.getElementById("spend-per-day-data");
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
  var costsByValue = {};
  data.datasets.forEach(function (dataset) {
    dataset.values.forEach(function (value, i) {
      costsByValue[value] = dataset.costs[i];
    });
  });
  new frappe.Chart(container, {
    type: "bar",
    height: 180,
    data: { labels: data.labels, datasets: data.datasets },
    colors: colors,
    barOptions: { spaceRatio: 0.25, stacked: true },
    tooltipOptions: {
      formatTooltipX: function (label) {
        return data.details[data.labels.indexOf(label)] || label;
      },
      formatTooltipY: function (value) {
        return costsByValue[value] || "$" + Number(value).toFixed(4);
      }
    }
  });
})();
