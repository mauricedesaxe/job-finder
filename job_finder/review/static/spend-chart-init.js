(function () {
  var container = document.getElementById("spend-per-day-chart");
  var dataElement = document.getElementById("spend-per-day-data");
  if (!container || !dataElement || typeof frappe === "undefined") return;
  var data = JSON.parse(dataElement.textContent);
  var acid = getComputedStyle(document.documentElement).getPropertyValue("--acid").trim();
  new frappe.Chart(container, {
    type: "bar",
    height: 180,
    data: { labels: data.labels, datasets: [{ values: data.values }] },
    colors: [acid || "#c8f542"],
    barOptions: { spaceRatio: 0.25 },
    tooltipOptions: {
      formatTooltipX: function (label) {
        return data.details[data.labels.indexOf(label)] || label;
      },
      formatTooltipY: function (value) {
        var i = data.values.indexOf(value);
        return i >= 0 ? data.costs[i] : "$" + Number(value).toFixed(4);
      }
    }
  });
})();
