// Runs before first paint so the page never flashes the wrong theme or announces the wrong language.
// Theme: the stored choice is "light", "dark" or absent (follow the system). Language: "zh", "en" or absent
// (follow the browser).
(function () {
  var theme = null, lang = null;
  try { theme = localStorage.getItem("farsight-theme"); lang = localStorage.getItem("farsight-lang"); } catch (error) { /* storage may be blocked */ }
  var dark = theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  if (lang !== "zh" && lang !== "en") lang = (navigator.language || "zh").toLowerCase().indexOf("zh") === 0 ? "zh" : "en";
  document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
})();
