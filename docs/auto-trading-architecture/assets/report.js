(function () {
  if (!window.mermaid) {
    return;
  }
  mermaid.initialize({
    startOnLoad: true,
    theme: 'neutral',
    securityLevel: 'loose',
    flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: true },
    sequence: { useMaxWidth: true, wrap: true },
    themeVariables: {
      fontFamily: '"Microsoft YaHei","PingFang SC","Segoe UI",system-ui,sans-serif',
      fontSize: '14px',
      primaryColor: '#e8f1f7',
      primaryBorderColor: '#0f5c8c',
      primaryTextColor: '#15212c',
      lineColor: '#5d6b7a',
      secondaryColor: '#fbeee8',
      tertiaryColor: '#ffffff'
    }
  });
})();
