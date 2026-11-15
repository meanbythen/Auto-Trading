(function () {
  if (typeof mermaid === "undefined") return;
  var css = getComputedStyle(document.documentElement);
  var accent = css.getPropertyValue("--accent").trim();
  var accent2 = css.getPropertyValue("--accent2").trim();
  var ink = css.getPropertyValue("--ink").trim();
  var muted = css.getPropertyValue("--muted").trim();
  var rule = css.getPropertyValue("--rule").trim();
  var bg2 = css.getPropertyValue("--bg2").trim();

  mermaid.initialize({
    startOnLoad: true,
    theme: "base",
    securityLevel: "loose",
    fontFamily: '"Instrument Sans", "Microsoft YaHei", "PingFang SC", sans-serif',
    themeVariables: {
      primaryColor: bg2,
      primaryTextColor: ink,
      primaryBorderColor: accent,
      secondaryColor: "#EEF2F7",
      secondaryTextColor: ink,
      secondaryBorderColor: muted,
      tertiaryColor: "#FBF3E6",
      tertiaryTextColor: ink,
      tertiaryBorderColor: accent2,
      lineColor: muted,
      textColor: ink,
      mainBkg: bg2,
      nodeBorder: accent,
      clusterBkg: "#F2F4F7",
      clusterBorder: rule,
      edgeLabelBackground: bg2,
      actorBkg: bg2,
      actorBorder: accent,
      actorTextColor: ink,
      signalColor: muted,
      signalTextColor: ink,
      noteBkgColor: "#FBF3E6",
      noteTextColor: ink,
      noteBorderColor: accent2,
      fontSize: "14px"
    },
    flowchart: { htmlLabels: true, curve: "basis", padding: 12 },
    sequence: { actorMargin: 46, messageMargin: 34, useMaxWidth: true },
    mindmap: { padding: 12, useMaxWidth: true }
  });
})();
